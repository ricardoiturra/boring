"""Systemd adapter for independent connectors."""
import subprocess

from cloudflare import Error


class Systemd:
    @staticmethod
    def run(*args, check=True):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=100)
        except (OSError, subprocess.TimeoutExpired):
            raise Error(f"Cannot run {args[0]}", 4) from None
        if check and result.returncode:
            raise Error(f"{args[0]} {args[1]} failed (exit {result.returncode})", 4)
        return result

    def set_running(self, unit, running):
        if running:
            self.run("systemctl", "enable", "--now", unit)
            if self.state(unit) != "active":
                raise Error(f"Connector process did not start: {unit}", 4)
        else:
            # Disable first so an interrupted stop cannot relaunch at reboot.
            disable = self.run("systemctl", "disable", unit, check=False)
            self.run("systemctl", "stop", unit)
            if disable.returncode:
                raise Error(f"Cannot disable {unit}", 4)

    def state(self, unit):
        result = self.run("systemctl", "is-active", unit, check=False)
        return result.stdout.strip() or "unknown"

    def restart(self, unit):
        self.set_running(unit, True)
        self.run("systemctl", "restart", unit)
        if self.state(unit) != "active":
            raise Error(f"Connector process did not restart: {unit}", 4)

    def logs(self, unit, lines):
        return self.run("journalctl", "--no-pager", "-u", unit, "-n", str(lines)).stdout.splitlines()
