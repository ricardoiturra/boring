"""Classic lifecycle. Caller holds the inventory lock for each transaction."""
import datetime as dt
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cloudflare import Error, NoRedirect
from storage import atomic

NAME = r"[a-z0-9][a-z0-9-]{0,47}"
HEX_ID = r"[0-9a-f]{32}"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


def hostname(value):
    value = value.lower().removesuffix(".")
    if len(value) > 253 or not re.fullmatch(rf"{LABEL}(?:\.{LABEL})+", value):
        raise Error("Expected a fully qualified ASCII hostname", 2)
    return value


def origin_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
        if (url.scheme not in {"http", "https"} or not url.hostname
                or port == 0 or url.netloc.endswith(":") or url.username is not None
                or url.path not in {"", "/"} or url.query or url.fragment
                or re.search(r"[\s\x00-\x1f\x7f\\%?#]", value)):
            raise ValueError
        if ":" not in url.hostname:
            if not re.fullmatch(rf"{LABEL}(?:\.{LABEL})*\.?", url.hostname):
                raise ValueError
        return f"{url.scheme}://{url.netloc}"
    except ValueError:
        raise Error("Expected an HTTP(S) origin without credentials, path or query", 2) from None


def expiration(relative, absolute, now):
    if relative is None and absolute is None:
        return None
    try:
        if relative is not None:
            match = re.fullmatch(r"([1-9][0-9]{0,8})([smhd])", relative)
            if not match:
                raise ValueError
            when = now + int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        else:
            parsed = dt.datetime.fromisoformat(absolute)
            if parsed.tzinfo is None:
                raise ValueError
            when = int(parsed.timestamp())
        dt.datetime.fromtimestamp(when, dt.timezone.utc)
        if when <= now:
            raise ValueError
        return when
    except (ValueError, OverflowError, OSError):
        raise Error("Expected a future timestamp with offset or a duration such as 24h", 2) from None


def due(record, now):
    return record["expires_at"] is not None and record["expires_at"] <= now


def can_run(record, now):
    return record["state"] == "ready" and record["desired_running"] and not due(record, now)


def owned_dns(record, remote):
    return all(remote.get(key) == value for key, value in dns_body(record).items() if key != "ttl")


def dns_body(record):
    return {"name": record["hostname"], "type": "CNAME", "proxied": True, "ttl": 1,
            "content": record["tunnel_id"] + ".cfargotunnel.com",
            "comment": "boring:" + record["id"]}


def ingress(record):
    return [{"hostname": record["hostname"], "service": record["origin_url"]},
            {"service": "http_status:404"}]


def probe_origin(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(urllib.request.Request(url, method="HEAD"), timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, ValueError):
        return {"reachable": False, "http_status": None, "healthy": None}
    return {"reachable": True, "http_status": status,
            "healthy": True if 200 <= status < 300 else False if status >= 500 else None}


class Classic:
    def __init__(self, inventory, system, api, clock, probe=probe_origin):
        self.inventory = inventory
        self.system = system
        self.api = api
        self.clock = clock
        self.probe = probe

    @staticmethod
    def tunnels(record):
        return f"/accounts/{record['account_id']}/cfd_tunnel"

    @staticmethod
    def dns(record):
        return f"/zones/{record['zone_id']}/dns_records"

    def tunnel_path(self, record):
        return self.tunnels(record) + "/" + record["tunnel_id"]

    def dns_named(self, record):
        return list(self.api.list(self.dns(record), **{"name.exact": record["hostname"]}))

    def remote_tunnel(self, record):
        if record["tunnel_id"] is None:
            return None
        for remote in self.api.list(self.tunnels(record), uuid=record["tunnel_id"], is_deleted="false"):
            if remote["id"] == record["tunnel_id"] and not remote.get("deleted_at"):
                if (remote.get("account_tag") != record["account_id"]
                        or remote.get("config_src") != "cloudflare"
                        or remote.get("name") != record["remote_name"]):
                    raise Error("Remote tunnel identity changed; inspect before proceeding", 5)
                return remote
        return None

    def create(self, config, name, host, url, expires, start):
        if not re.fullmatch(NAME, name):
            raise Error("Invalid tunnel name", 2)
        host = hostname(host)
        url = origin_url(url)
        if not host.endswith("." + config["zone"]):
            raise Error("Expected a hostname below the configured zone", 2)
        same = next((r for r in self.inventory.records()
                     if r["name"] == name or r["hostname"] == host), None)
        if same:
            if any(same[k] != v for k, v in dict(name=name, hostname=host, origin_url=url,
                   account_id=config["account_id"], zone_id=config["zone_id"]).items()):
                raise Error("Local name or hostname already registered with different settings", 5)
            if same["state"] == "creating":
                return self.resume(same)
            if same["state"] in {"ready", "expired"}:
                if can_run(same, self.clock()):
                    self.system.set_running(same["unit"], True)
                return same
            raise Error("Finish the pending deletion first", 5)
        zone = self.api.get(f"/zones/{config['zone_id']}")
        if zone["name"] != config["zone"] or zone["account"]["id"] != config["account_id"]:
            raise Error("Configured zone does not belong to the configured account/name", 5)
        ident = uuid.uuid4().hex
        record = dict(schema_version=1, id=ident, name=name, remote_name=f"boring-{ident}-{name[:20]}",
                      account_id=config["account_id"], zone_id=config["zone_id"], zone=config["zone"],
                      hostname=host, origin_url=url, tunnel_id=None, dns_id=None,
                      unit=f"boring@{ident}.service", created_at=int(self.clock()), expires_at=expires,
                      state="creating", desired_running=start, protection={"mode": "classic"},
                      managed={"config": False, "dns_attempted": False, "tunnel_attempted": False})
        if self.dns_named(record):
            raise Error("Hostname already has DNS records", 5)
        self.inventory.save(record)
        return self.resume(record)

    def candidates(self, record):
        return [r["id"] for r in self.api.list(self.tunnels(record), name=record["remote_name"],
                                               is_deleted="false")
                if r.get("name") == record["remote_name"] and not r.get("deleted_at")
                and r.get("config_src") == "cloudflare"
                and r.get("account_tag") == record["account_id"]]

    def resume(self, record, adopt=None, retry=False):
        if record["state"] != "creating":
            raise Error("Tunnel has no pending creation", 5)
        managed = record["managed"]
        if (adopt or retry) and (record["tunnel_id"] or not managed["tunnel_attempted"]):
            raise Error("Tunnel creation is not awaiting reconciliation", 5)
        if not record["tunnel_id"]:
            if managed["tunnel_attempted"]:
                matches = self.candidates(record)
                if adopt and matches == [adopt] and re.fullmatch(UUID, adopt):
                    record["tunnel_id"] = adopt
                    self.inventory.save(record)
                elif retry and not matches:
                    pass
                else:
                    raise Error("Tunnel creation outcome uncertain; inspect and reconcile with recover", 5)
            if not record["tunnel_id"]:
                managed["tunnel_attempted"] = True
                self.inventory.save(record)
                result = self.api.write("POST", self.tunnels(record),
                                        {"name": record["remote_name"], "config_src": "cloudflare"})
                ident = result["id"]
                if not re.fullmatch(UUID, ident):
                    raise Error("Cloudflare returned an invalid tunnel identifier")
                record["tunnel_id"] = ident
                self.inventory.save(record)
        if not managed["config"]:
            self.api.write("PUT", self.tunnel_path(record) + "/configurations",
                           {"config": {"ingress": ingress(record)}})
            managed["config"] = True
            self.inventory.save(record)
        if not record["dns_id"]:
            rows = self.dns_named(record)
            if rows:
                if not managed["dns_attempted"] or len(rows) != 1 or not owned_dns(record, rows[0]):
                    raise Error("DNS conflict; no existing record will be overwritten", 5)
                result = rows[0]
            else:
                managed["dns_attempted"] = True
                self.inventory.save(record)
                result = self.api.write("POST", self.dns(record), dns_body(record))
            if not re.fullmatch(HEX_ID, result["id"]):
                raise Error("Cloudflare returned an invalid DNS identifier")
            record["dns_id"] = result["id"]
            self.inventory.save(record)
        token = self.api.get(self.tunnel_path(record) + "/token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", token):
            raise Error("Unexpected connector token response")
        atomic(self.inventory.credential(record["id"]), token + "\n")
        if due(record, self.clock()):
            record.update(state="expired", desired_running=False)
        else:
            record["state"] = "ready"
        self.inventory.save(record)
        if record["desired_running"]:
            self.system.set_running(record["unit"], True)
        return record

    def lifecycle(self, record, action, expires=None):
        state = record["state"]
        if state not in {"ready", "expired"}:
            raise Error("Resolve the pending operation first", 5)
        if action in {"start", "restart"} and (state == "expired" or due(record, self.clock())):
            raise Error("Renew this expired tunnel before starting", 5)
        if action == "renew":
            if state == "expired" or due(record, self.clock()):
                record["desired_running"] = False
            record.update(expires_at=expires, state="ready")
            self.inventory.save(record)
            if not record["desired_running"]:
                self.system.set_running(record["unit"], False)
            return record
        if action == "expire":
            record.update(state="expired", desired_running=False)
            if not due(record, self.clock()):
                record["expires_at"] = int(self.clock())
        else:
            record["desired_running"] = action in {"start", "restart"}
        self.inventory.save(record)
        if action == "restart":
            self.system.restart(record["unit"])
        else:
            self.system.set_running(record["unit"], record["desired_running"])
        return record

    def expire_due(self):
        done, failed = [], []
        for record in self.inventory.records():
            if due(record, self.clock()) and record["state"] in {"ready", "expired"}:
                try:
                    if record["state"] != "expired" or self.system.state(record["unit"]) not in {"inactive", "failed"}:
                        self.lifecycle(record, "expire")
                        done.append(record["name"])
                except Error:
                    failed.append(record["name"])
        return {"expired": done, "failed": failed}

    def audit(self, config):
        records = self.inventory.records()
        tunnel_ids = {r["tunnel_id"] for r in records if r["account_id"] == config["account_id"]}
        dns_ids = {r["dns_id"] for r in records if r["zone_id"] == config["zone_id"]}
        tunnels = self.api.list(self.tunnels(config), include_prefix="boring-", is_deleted="false")
        dns = self.api.list(self.dns(config), **{"comment.startswith": "boring:"})
        return {
            "unregistered_tunnel_candidates": [
                {"id": row["id"], "name": row["name"]} for row in tunnels
                if row["id"] not in tunnel_ids and row["name"].startswith("boring-")
            ],
            "unregistered_dns_candidates": [
                {"id": row["id"], "name": row["name"]} for row in dns
                if row["id"] not in dns_ids and (row.get("comment") or "").startswith("boring:")
            ],
        }

    def status(self, record):
        remote = self.remote_tunnel(record)
        rows = self.dns_named(record)
        dns = [r for r in rows if record["tunnel_id"] and owned_dns(record, r)]
        registered_dns = any(row["id"] == record["dns_id"] for row in dns)
        status = remote.get("status") if remote else None
        connectors = self.api.get(self.tunnel_path(record) + "/connections") if remote else []
        return {"record": record, "service_state": self.system.state(record["unit"]),
                "tunnel_exists": remote is not None, "tunnel_status": status,
                "connector_connected": bool(any(c.get("conns") for c in connectors))
                if remote else False,
                "dns_configured": registered_dns, "dns_orphaned": bool(dns) and not remote,
                "origin": self.probe(record["origin_url"]),
                "public_access": "not_verified", "expired": due(record, self.clock()),
                "recovery_candidates": self.candidates(record)
                if record["state"] == "creating" and not record["tunnel_id"] else []}

    def delete(self, record):
        if not record["tunnel_id"] and record["managed"]["tunnel_attempted"]:
            raise Error("Reconcile the pending tunnel creation before deletion", 5)
        remote = self.remote_tunnel(record)
        dns_id = record["dns_id"]
        host_records = self.dns_named(record)
        owned = [r for r in host_records if record["tunnel_id"] and owned_dns(record, r)]
        if dns_id and any(r["id"] == dns_id and not owned_dns(record, r) for r in host_records):
            raise Error("Managed DNS record changed", 5)
        if not dns_id and record["managed"]["dns_attempted"] and owned:
            if len(owned) != 1 or not re.fullmatch(HEX_ID, owned[0]["id"]):
                raise Error("Cannot reconcile the DNS record safely", 5)
            dns_id = owned[0]["id"]
        dns_record = None
        if dns_id:
            # Read by ID too: a renamed record is invisible in the hostname query.
            dns_record = self.api.get(self.dns(record) + "/" + dns_id, missing=True)
            if dns_record and not owned_dns(record, dns_record):
                raise Error("Managed DNS record changed", 5)
        if remote:
            path = self.tunnel_path(record)
            target = dns_body(record)["content"]
            aliases = self.api.list(self.dns(record), **{"content.exact": target})
            if any(row["id"] != dns_id and row.get("content") == target for row in aliases):
                raise Error("Another DNS record depends on this tunnel", 5)
            for endpoint in ("teamnet/routes", "zerotrust/routes/hostname"):
                routes = self.api.list(f"/accounts/{record['account_id']}/{endpoint}",
                                       tunnel_id=record["tunnel_id"], is_deleted="false")
                if next(iter(routes), None) is not None:
                    raise Error("Private IP or hostname routes depend on this tunnel", 5)
            config = self.api.get(path + "/configurations")["config"] or {}
            rules = config.get("ingress") or []
            flattened = [{k: v for k, v in row.items() if v not in ({}, "", None)} for row in rules]
            expected = [ingress(record)] if record["managed"]["config"] else [[], ingress(record)]
            if flattened not in expected or (config.get("warp-routing") or {}).get("enabled"):
                raise Error("Remote ingress or private routing changed", 5)
        record.update(state="deleting", desired_running=False, dns_id=dns_id)
        self.inventory.save(record)
        self.system.set_running(record["unit"], False)
        if remote:
            connectors = self.api.get(self.tunnel_path(record) + "/connections")
            if any(connector.get("conns") for connector in connectors):
                raise Error("Cloudflare still reports connectors; wait or stop other replicas", 5)
        if dns_record:
            self.api.write("DELETE", self.dns(record) + "/" + dns_id)
        if remote:
            self.api.write("DELETE", self.tunnel_path(record))
        self.inventory.credential(record["id"]).unlink(missing_ok=True)
        self.inventory.remove(record)
        return {"deleted": record["name"], "id": record["id"]}
