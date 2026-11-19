"""Private, atomic inventory and configuration."""
import contextlib
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path

from cloudflare import Error

STATE = Path("/var/lib/boring")
CONFIG = Path("/etc/boring/config.json")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, content):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".boring-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def private_text(path):
    with path.open() as stream:
        mode = os.fstat(stream.fileno())
        if (not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077
                or mode.st_uid != os.geteuid()):
            raise Error(f"Expected a private file owned by the current user: {path}", 2)
        return stream.read()


def read(path):
    return json.loads(private_text(path))


class Inventory:
    def __init__(self, root=STATE):
        self.root = root

    @contextlib.contextmanager
    def lock(self):
        with (self.root / ".lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def records(self):
        return [self.load(path.stem) for path in sorted(self.root.glob("*.json"))]

    def load(self, ident):
        record = read(self.root / f"{ident}.json")
        if record["schema_version"] != 1 or record["id"] != ident:
            raise Error("Invalid inventory schema or identifier")
        return record

    def save(self, record):
        atomic(self.root / f"{record['id']}.json", json.dumps(record, indent=2) + "\n")

    def remove(self, record):
        (self.root / f"{record['id']}.json").unlink()
        sync_directory(self.root)

    def credential(self, ident):
        return self.root / f"{ident}.token"
