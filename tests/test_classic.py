import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import cli
from classic import Classic, can_run, expiration, origin_url
from cloudflare import Cloudflare, Error, NoRedirect
from storage import Inventory, private_text

ACCOUNT = "a" * 32
ZONE = "b" * 32
TUNNEL = "12345678-1234-4123-8123-123456789abc"
DNS = "d" * 32
CONFIG = dict(account_id=ACCOUNT, zone_id=ZONE, zone="example.com")
CONNECTOR_TOKEN = "fake-connector-token"
ROOT = Path(__file__).resolve().parents[1]


class FakeCloudflare:
    def __init__(self):
        self.tunnel = None
        self.config = None
        self.dns = {}
        self.routes = []
        self.connectors = []
        self.writes = []
        self.fail_before = None
        self.fail_after = None

    def list(self, path, **filters):
        if path.endswith(("/teamnet/routes", "/zerotrust/routes/hostname")):
            return self.routes
        if path.endswith("/cfd_tunnel"):
            rows = [self.tunnel] if self.tunnel else []
            keys = {"uuid": "id", "name": "name"}
            for key, field in keys.items():
                if key in filters:
                    rows = [r for r in rows if r[field] == filters[key]]
            return rows
        if path.endswith("/dns_records"):
            rows = list(self.dns.values())
            for key, value in filters.items():
                field, relation = key.split(".")
                if relation == "exact":
                    rows = [r for r in rows if r.get(field) == value]
                elif relation == "startswith":
                    rows = [r for r in rows if r.get(field, "").startswith(value)]
            return rows
        raise AssertionError(path)

    def get(self, path, **kwargs):
        if path == "/zones/" + ZONE:
            return {"name": "example.com", "account": {"id": ACCOUNT}}
        if path.endswith("/token"):
            return CONNECTOR_TOKEN
        if path.endswith("/connections"):
            return self.connectors
        if path.endswith("/configurations"):
            return {"config": self.config}
        if "/dns_records/" in path:
            return self.dns.get(path.rsplit("/", 1)[1])
        raise AssertionError(path)

    def write(self, method, path, body=None):
        event = (method, path.rsplit("/", 1)[1])
        self.writes.append(event)
        if event == self.fail_before:
            self.fail_before = None
            raise Error("Injected failure before mutation")
        result = None
        if event == ("POST", "cfd_tunnel"):
            assert self.tunnel is None
            self.tunnel = dict(body, id=TUNNEL, account_tag=ACCOUNT, status="inactive")
            result = self.tunnel
        elif event == ("PUT", "configurations"):
            self.config = body["config"]
        elif event == ("POST", "dns_records"):
            assert DNS not in self.dns
            self.dns[DNS] = dict(body, id=DNS)
            result = self.dns[DNS]
        elif event == ("DELETE", DNS):
            del self.dns[DNS]
        elif event == ("DELETE", TUNNEL):
            self.tunnel = None
        else:
            raise AssertionError(event)
        if event == self.fail_after:
            self.fail_after = None
            raise Error("Injected response loss after mutation")
        return result


class FakeSystemd:
    def __init__(self):
        self.units = {}
        self.fail_stop = False

    def set_running(self, unit, running):
        if not running and self.fail_stop:
            raise Error("Injected stop failure", 4)
        self.units[unit] = "active" if running else "inactive"

    def restart(self, unit):
        self.set_running(unit, True)

    def state(self, unit):
        return self.units.get(unit, "inactive")


class ClassicTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.inventory = Inventory(self.root)
        self.cf = FakeCloudflare()
        self.system = FakeSystemd()
        self.now = 1000
        self.app = Classic(self.inventory, self.system, self.cf, lambda: self.now,
                           lambda _: {"healthy": True})

    def create(self, expires=1060, start=False):
        with self.inventory.lock():
            return self.app.create(CONFIG, "grafana", "grafana.example.com",
                                   "http://127.0.0.1:3000", expires, start)

    def record(self):
        return self.inventory.records()[0]

    def test_create_and_retry_preserve_identity_and_private_credentials(self):
        record = self.create()
        count = len(self.cf.writes)
        self.assertEqual(self.create(), record)
        self.assertEqual(len(self.cf.writes), count)
        path = self.inventory.credential(record["id"])
        self.assertEqual(private_text(path).strip(), CONNECTOR_TOKEN)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(CONNECTOR_TOKEN, json.dumps(record))
        self.assertEqual(self.cf.config["ingress"][-1], {"service": "http_status:404"})

    def test_invalid_create_arguments_never_call_api(self):
        for host, url in [("outside.test", "http://localhost"), ("ok.example.com", "file:///etc/passwd"),
                          ("ok.example.com", "https://alice:secret@localhost"),
                          ("ok.example.com", "http://localhost:65536")]:
            with self.subTest(host=host, url=url), self.assertRaises(Error):
                self.app.create(CONFIG, "test", host, url, None, False)
        self.assertEqual(self.cf.writes, [])
        self.assertEqual(self.inventory.records(), [])

    def test_duplicate_dns_is_not_modified(self):
        self.cf.dns[DNS] = {"id": DNS, "name": "grafana.example.com", "type": "A"}
        with self.assertRaisesRegex(Error, "already has DNS"):
            self.create()
        self.assertEqual(self.cf.writes, [])

    def test_tunnel_post_response_loss_requires_verified_uuid(self):
        self.cf.fail_after = ("POST", "cfd_tunnel")
        with self.assertRaises(Error):
            self.create()
        with self.assertRaisesRegex(Error, "uncertain"):
            self.create()
        self.assertEqual(self.app.status(self.record())["recovery_candidates"], [TUNNEL])
        with self.assertRaises(Error):
            self.app.delete(self.record())
        with self.assertRaises(Error):
            self.app.resume(self.record(), retry=True)
        self.app.resume(self.record(), adopt=TUNNEL)
        self.assertEqual(self.cf.writes.count(("POST", "cfd_tunnel")), 1)

    def test_explicit_retry_after_failed_post_without_remote_candidate(self):
        self.cf.fail_before = ("POST", "cfd_tunnel")
        with self.assertRaises(Error):
            self.create()
        record = self.app.resume(self.record(), retry=True)
        self.assertEqual(record["state"], "ready")

    def test_dns_response_loss_reconciles_by_owner(self):
        self.cf.fail_after = ("POST", "dns_records")
        with self.assertRaises(Error):
            self.create()
        self.assertEqual(self.create()["dns_id"], DNS)
        self.assertEqual(self.cf.writes.count(("POST", "dns_records")), 1)

    def test_put_failure_resumes_from_saved_tunnel_uuid(self):
        self.cf.fail_before = ("PUT", "configurations")
        with self.assertRaises(Error):
            self.create()
        self.assertEqual(self.record()["tunnel_id"], TUNNEL)
        self.assertEqual(self.create()["state"], "ready")
        self.assertEqual(self.cf.writes.count(("POST", "cfd_tunnel")), 1)

    def test_start_stop_and_restart_are_independent_of_remote_health(self):
        record = self.create()
        self.app.lifecycle(record, "start")
        self.app.lifecycle(record, "restart")
        status = self.app.status(record)
        self.assertEqual(status["service_state"], "active")
        self.assertTrue(status["tunnel_exists"])
        self.assertFalse(status["connector_connected"])
        self.cf.connectors = [{"conns": [{"id": "test"}]}]
        self.assertTrue(self.app.status(record)["connector_connected"])
        self.app.lifecycle(record, "stop")
        self.assertEqual(self.system.state(record["unit"]), "inactive")

    def test_expiry_stop_retry_and_boot_gate_from_disk(self):
        record = self.create(start=True)
        self.assertTrue(can_run(self.record(), self.now))
        self.now = 1060
        self.assertFalse(can_run(Inventory(self.root).records()[0], self.now))
        with self.assertRaises(Error):
            self.app.lifecycle(self.record(), "start")
        self.system.fail_stop = True
        self.assertEqual(self.app.expire_due()["failed"], ["grafana"])
        self.assertEqual(self.record()["state"], "expired")
        self.system.fail_stop = False
        self.assertEqual(self.app.expire_due()["expired"], ["grafana"])
        self.assertEqual(self.app.expire_due()["expired"], [])
        self.assertEqual(self.system.state(record["unit"]), "inactive")
        renewed = self.app.lifecycle(self.record(), "renew", 1200)
        self.assertFalse(can_run(renewed, self.now))
        self.assertTrue(can_run(self.app.lifecycle(renewed, "start"), self.now))
        self.assertIn(DNS, self.cf.dns)

    def test_orphan_status_and_safe_dns_cleanup(self):
        record = self.create()
        self.cf.tunnel = None
        status = self.app.status(record)
        self.assertTrue(status["dns_orphaned"])
        self.assertFalse(status["tunnel_exists"])
        self.app.delete(record)
        self.assertEqual(self.cf.dns, {})
        self.assertEqual(self.inventory.records(), [])
        self.assertFalse(self.inventory.credential(record["id"]).exists())

    def test_delete_rejects_renamed_record_and_foreign_dependencies(self):
        record = self.create(start=True)
        self.cf.dns[DNS]["name"] = "admin.example.com"
        with self.assertRaisesRegex(Error, "changed"):
            self.app.delete(record)
        self.cf.dns[DNS]["name"] = record["hostname"]
        self.cf.routes = [{"id": "foreign"}]
        with self.assertRaisesRegex(Error, "routes depend"):
            self.app.delete(record)
        self.cf.routes = []
        self.cf.dns["foreign"] = dict(self.cf.dns[DNS], name="alias.example.com", id="foreign")
        with self.assertRaisesRegex(Error, "Another DNS record"):
            self.app.delete(record)
        self.assertEqual(self.system.state(record["unit"]), "active")
        self.assertFalse(any(method == "DELETE" for method, _ in self.cf.writes))

    def test_delete_recovers_after_losing_dns_and_tunnel_responses(self):
        record = self.create(start=True)
        self.cf.fail_after = ("DELETE", DNS)
        with self.assertRaises(Error):
            self.app.delete(record)
        self.assertEqual(self.record()["state"], "deleting")
        self.assertFalse(can_run(self.record(), self.now))
        self.cf.fail_after = ("DELETE", TUNNEL)
        with self.assertRaises(Error):
            self.app.delete(self.record())
        self.app.delete(self.record())
        self.assertEqual(self.cf.writes.count(("DELETE", DNS)), 1)
        self.assertEqual(self.cf.writes.count(("DELETE", TUNNEL)), 1)
        self.assertEqual(self.inventory.records(), [])

    def test_audit_candidates_are_observational(self):
        self.create()
        self.assertEqual(self.app.audit(CONFIG)["unregistered_tunnel_candidates"], [])
        self.inventory.remove(self.record())
        self.assertEqual(self.app.audit(CONFIG)["unregistered_dns_candidates"][0]["id"], DNS)

    def test_expiration_and_ipv6(self):
        self.assertEqual(expiration("24h", None, 1000), 87400)
        self.assertEqual(expiration(None, "1970-01-01T01:20:00+01:00", 1000), 1200)
        with self.assertRaises(Error):
            expiration(None, "2030-01-01", 1000)
        self.assertEqual(origin_url("https://[::1]:8443/"), "https://[::1]:8443")


class BoundaryTests(unittest.TestCase):
    def test_cli_entry_and_structured_errors(self):
        for args, code, key in [(["version", "--json"], 0, "version"),
                                (["create", "--json"], 2, "error"),
                                (["--json", "help"], 0, "help")]:
            result = subprocess.run([str(ROOT / "boring.sh"), *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, code)
            self.assertIn(key, json.loads(result.stdout))

    def test_cli_create_status_delete_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = Inventory(root / "state")
            inventory.root.mkdir()
            config = root / "etc" / "config.json"
            config.parent.mkdir()
            token = config.parent / "api.token"
            token.write_text("fake-api-token")
            token.chmod(0o600)
            fake_cf = FakeCloudflare()
            fake_system = FakeSystemd()
            parser = cli.parser()

            def invoke(*args):
                return cli.dispatch(parser.parse_args(args), True, parser)

            with (patch.object(cli, "Inventory", return_value=inventory),
                  patch.object(cli, "CONFIG", config),
                  patch.object(cli, "Cloudflare", return_value=fake_cf),
                  patch.object(cli, "Systemd", return_value=fake_system),
                  patch.object(cli, "Classic", side_effect=lambda *args:
                               Classic(*args, probe=lambda _: {"healthy": True})),
                  patch.object(cli, "report_event")):
                with patch.object(cli, "os") as cli_os:
                    cli_os.geteuid.return_value = 0
                    invoke("config", "--account-id", ACCOUNT, "--zone-id", ZONE,
                           "--zone", "example.com", "--api-token-file", str(token))
                    create, code = invoke("create", "--name", "grafana",
                                          "--hostname", "grafana.example.com",
                                          "--url", "http://127.0.0.1:1")
                    self.assertEqual(code, 0)
                    self.assertEqual(invoke("list")[0][0]["id"], create["id"])
                    status, _ = invoke("status", "--name", "grafana")
                    self.assertEqual(status["record"]["id"], create["id"])
                    self.assertTrue(status["dns_configured"])
                    with self.assertRaisesRegex(Error, "--yes"):
                        invoke("delete", "--name", "grafana")
                    self.assertEqual(invoke("delete", "--name", "grafana", "--yes")[1], 0)
                    self.assertEqual(invoke("list")[0], [])

    def test_api_errors_do_not_echo_provider_body_or_authorization(self):
        api = Cloudflare("mock-secret")
        response = urllib.error.HTTPError("https://api.cloudflare.com", 403, "mock-secret",
                                          {}, io.BytesIO(b'{"error":"mock-secret"}'))
        with patch.object(api.opener, "open", side_effect=response):
            with self.assertRaisesRegex(Error, "HTTP 403") as error:
                api.write("POST", "/example", {"secret": "mock-secret"})
        self.assertNotIn("mock-secret", str(error.exception))
        self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_dns_not_found_requires_provider_code(self):
        api = Cloudflare("mock-secret")
        for cloudflare_code in (81044, 1000):
            response = urllib.error.HTTPError("https://api.cloudflare.com", 404, "", {},
                io.BytesIO(json.dumps({"errors": [{"code": cloudflare_code}]}).encode()))
            with patch.object(api.opener, "open", side_effect=response):
                if cloudflare_code == 81044:
                    self.assertIsNone(api.get("/example", missing=True))
                else:
                    with self.assertRaises(Error):
                        api.get("/example", missing=True)

    def test_list_pagination(self):
        api = Cloudflare("mock-secret")
        with patch.object(api, "request", side_effect=[
            {"result": [1], "result_info": {"total_pages": 2}},
            {"result": [2], "result_info": {"total_pages": 2}},
        ]) as request:
            self.assertEqual(list(api.list("/example", **{"name.exact": "hello.example.com"})), [1, 2])
            self.assertIn("page=2", request.call_args.args[1])

    def test_reject_shared_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("fake-secret")
            os.chmod(path, 0o644)
            with self.assertRaises(Error):
                private_text(path)


if __name__ == "__main__":
    unittest.main()
