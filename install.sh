#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 0 || $EUID != 0 || ! -d /run/systemd/system || $(< /proc/1/comm) != systemd ]]; then
    echo 'Run sudo ./install.sh on Debian with systemd.' >&2
    exit 2
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
apt-get install -y --no-install-recommends python3 ca-certificates
if [[ ! -x /usr/bin/cloudflared ]]; then
    apt-get install -y --no-install-recommends cloudflared
fi
help_output=$(/usr/bin/cloudflared tunnel run --help)
if [[ $help_output != *--token-file* ]]; then
    echo 'Install an official cloudflared build with --token-file support.' >&2
    exit 4
fi
if [[ -e /usr/local/bin/boring || -L /usr/local/bin/boring ]]; then
    if [[ $(readlink -- /usr/local/bin/boring || true) != /usr/local/lib/boring/boring.sh ]]; then
        echo 'An unrelated executable already uses /usr/local/bin/boring.' >&2
        exit 5
    fi
fi
install -d -m 0755 /usr/local/lib/boring/lib
install -d -m 0700 /etc/boring /var/lib/boring
install -m 0755 boring.sh /usr/local/lib/boring/boring.sh
install -m 0644 lib/*.py /usr/local/lib/boring/lib/
ln -sfn /usr/local/lib/boring/boring.sh /usr/local/bin/boring
install -m 0644 adapters/systemd/boring* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now boring-expiry.timer
