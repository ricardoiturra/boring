"""Stable CLI and process boundaries."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import syslog
import time

from classic import Classic, HEX_ID, can_run, expiration, hostname
from cloudflare import Cloudflare, Error
from platform_systemd import Systemd
from storage import CONFIG, Inventory, atomic, private_text, read

VERSION = "0.1.0"


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Error("Invalid arguments. Run boring help or boring COMMAND --help.", 2)


def lifetime(parser, required=False):
    options = parser.add_mutually_exclusive_group(required=required)
    options.add_argument("--expires-in")
    options.add_argument("--expires-at")
    if required:
        options.add_argument("--persistent", action="store_true")


def parser():
    cli = Parser(prog="boring", description="Manage named Cloudflare Tunnels.",
                 epilog="Add --json for structured output. Times in JSON are Unix seconds.")
    commands = cli.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "help": "Show CLI help", "version": "Show installed version",
        "list": "List local inventory", "doctor": "Check local prerequisites",
        "audit": "List unregistered remote candidates",
        "config": "Read or replace local configuration",
        "create": "Provision a tunnel", "inspect": "Inspect local and remote state",
        "status": "Inspect local and remote state", "start": "Start local connector",
        "stop": "Stop local connector", "restart": "Restart local connector",
        "expire": "Expire and stop the connector",
        "renew": "Change or clear expiration", "logs": "Read connector journal",
        "delete": "Remove owned resources", "recover": "Resume an interrupted operation",
        "_expire-due": None, "_may-start": None,
    }.items():
        cmd = commands.add_parser(name, description=help_text)
        if name in {"create", "inspect", "status", "start", "stop", "restart", "expire",
                    "renew", "logs", "delete", "recover"}:
            cmd.add_argument("--name", required=True)
        if name == "create":
            cmd.add_argument("--hostname", required=True)
            cmd.add_argument("--url", required=True)
            cmd.add_argument("--start", action="store_true")
            lifetime(cmd)
        if name == "renew":
            lifetime(cmd, required=True)
        if name == "config":
            for option in ("account-id", "zone-id", "zone", "api-token-file"):
                cmd.add_argument("--" + option)
        if name in {"delete", "recover"}:
            cmd.add_argument("--yes", action="store_true")
        if name == "recover":
            choice = cmd.add_mutually_exclusive_group()
            choice.add_argument("--adopt-tunnel")
            choice.add_argument("--retry-create", action="store_true")
        if name == "logs":
            cmd.add_argument("--lines", type=int, default=50)
        if name == "_may-start":
            cmd.add_argument("--id", required=True)
    return cli


def configure(args):
    fields = {key: getattr(args, key) for key in ("account_id", "zone_id", "zone", "api_token_file")}
    if not any(value is not None for value in fields.values()):
        return read(CONFIG)
    if not all(fields.values()) or not all(re.fullmatch(HEX_ID, fields[key])
                                           for key in ("account_id", "zone_id")):
        raise Error("Supply valid account ID, zone ID, zone name and API token file", 2)
    fields["zone"] = hostname(fields["zone"])
    path = Path(fields["api_token_file"]).resolve(strict=True)
    if path == CONFIG or path.is_relative_to(Inventory().root):
        raise Error("API token file must be separate from configuration and inventory", 2)
    Cloudflare(private_text(path).strip())
    fields["api_token_file"] = str(path)
    atomic(CONFIG, json.dumps(fields, indent=2) + "\n")
    return fields


def connector_check():
    binary = "/usr/bin/cloudflared"
    if not os.access(binary, os.X_OK):
        return False
    result = subprocess.run([binary, "tunnel", "run", "--help"],
                            capture_output=True, text=True, timeout=10)
    return result.returncode == 0 and "--token-file" in result.stdout


def doctor():
    system = Systemd()
    checks = {"cloudflared_token_file": connector_check(),
              "systemd_running": (Path("/run/systemd/system").is_dir()
                                  and Path("/proc/1/comm").read_text().strip() == "systemd"),
              "journalctl": bool(shutil.which("journalctl")),
              "inventory_directory": Inventory().root.is_dir(),
              "configuration_present": CONFIG.is_file()}
    if checks["systemd_running"]:
        checks["expiry_timer_active"] = system.state("boring-expiry.timer") == "active"
        checks["connector_template"] = system.run(
            "systemctl", "show", "--property=LoadState", "--value",
            "boring@" + "0" * 32 + ".service", check=False).stdout.strip() == "loaded"
    return checks


def confirm(record, args, structured):
    if args.yes:
        return
    if structured or not sys.stdin.isatty():
        raise Error("Deletion requires --yes for noninteractive use", 5)
    expected = record["name"]
    print(f"Delete owned resources for {expected} ({record['hostname']})?"
          " Check cross-zone aliases and load balancers first."
          f" Type {expected} to confirm: ", file=sys.stderr, end="", flush=True)
    if input() != expected:
        raise Error("Deletion cancelled", 5)


def report_event(command, name=None):
    syslog.syslog(syslog.LOG_NOTICE, f"command={command} tunnel={name or '-'} success")


def dispatch(args, structured, cli):
    action = args.command
    if action == "help":
        return {"help": cli.format_help()}, 0
    if action == "version":
        return {"version": VERSION}, 0
    if action == "doctor":
        checks = doctor()
        return checks, 0 if all(checks.values()) else 4

    if os.geteuid() != 0:
        raise Error("Run inventory and lifecycle commands as root", 4)
    inventory = Inventory()
    if action == "_may-start":
        if not re.fullmatch(HEX_ID, args.id):
            return {}, 1
        if not (inventory.root / f"{args.id}.json").is_file():
            return {}, 1
        record = inventory.load(args.id)
        return {}, 0 if can_run(record, time.time()) else 1
    if not inventory.root.is_dir() or not CONFIG.parent.is_dir():
        raise Error("Install Boring before managing inventory", 4)
    if action == "delete" and not args.yes and (structured or not sys.stdin.isatty()):
        raise Error("Deletion requires --yes for noninteractive use", 5)

    with inventory.lock():
        if action == "config":
            return configure(args), 0
        if action == "list":
            return inventory.records(), 0

        live = action in {"create", "status", "inspect", "delete", "recover", "audit"}
        config = read(CONFIG) if live else None
        api = Cloudflare(private_text(Path(config["api_token_file"])).strip()) if live else None
        app = Classic(inventory, Systemd(), api, time.time)
        if action == "audit":
            return app.audit(config), 0
        if action == "_expire-due":
            result = app.expire_due()
            for name in result["expired"]:
                report_event("expire", name)
            return result, 4 if result["failed"] else 0
        if action == "create":
            expires = expiration(args.expires_in, args.expires_at, time.time())
            result = app.create(config, args.name, args.hostname, args.url, expires, args.start)
        else:
            record = next((r for r in inventory.records() if r["name"] == args.name), None)
            if record is None:
                raise Error("Tunnel is absent from local inventory", 5)
            if live and record["account_id"] != config["account_id"]:
                raise Error("Restore configuration for the recorded account first", 5)
            if action in {"status", "inspect"}:
                return app.status(record), 0
            if action == "logs":
                if not 1 <= args.lines <= 10000:
                    raise Error("Log line count must be between 1 and 10000", 2)
                lines = app.system.logs(record["unit"], args.lines)
                path = inventory.credential(record["id"])
                if path.exists():
                    token = private_text(path).strip()
                    if token:
                        lines = [line.replace(token, "[redacted]") for line in lines]
                return {"lines": lines}, 0
            if action == "delete" or (action == "recover" and record["state"] == "deleting"):
                if action == "recover" and (args.adopt_tunnel or args.retry_create):
                    raise Error("Cannot use creation recovery flags during deletion", 2)
                confirm(record, args, structured)
                result = app.delete(record)
            elif action == "recover":
                if (args.adopt_tunnel or args.retry_create) and not args.yes:
                    raise Error("Reconciliation requires --yes", 5)
                result = app.resume(record, args.adopt_tunnel, args.retry_create)
            else:
                expires = (expiration(args.expires_in, args.expires_at, time.time())
                           if action == "renew" else None)
                result = app.lifecycle(record, action, expires)
        report_event(action, args.name)
        return result, 0


def main():
    os.umask(0o077)
    syslog.openlog("boring")
    argv = sys.argv[1:]
    structured = "--json" in argv
    action = None
    try:
        cli = parser()
        args = cli.parse_args([arg for arg in argv if arg != "--json"] or ["help"])
        action = args.command
        result, code = dispatch(args, structured, cli)
    except Error as exc:
        result, code = {"error": str(exc), "code": exc.code}, exc.code
    except FileNotFoundError:
        result, code = {"error": "Required configuration, credential or local file is missing", "code": 4}, 4
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        result, code = {"error": "Cannot process local state or Cloudflare response", "code": 4}, 4
    except KeyboardInterrupt:
        result, code = {"error": "Interrupted", "code": 130}, 130
    if action == "_may-start" and not code:
        return code
    if action == "_may-start" and not result:
        return code
    stream = sys.stderr if code and not structured else sys.stdout
    if structured:
        print(json.dumps(result, sort_keys=True), file=stream)
    elif "error" in result:
        print(result["error"], file=stream)
    elif action == "help":
        print(result["help"], end="")
    elif action == "version":
        print(result["version"])
    elif action == "logs":
        print("\n".join(result["lines"]))
    else:
        print(json.dumps(result, indent=2, sort_keys=True), file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())
