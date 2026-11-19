#!/usr/bin/env bash
set -euo pipefail
if [[ $# != 1 || ${1:-} != --yes || $EUID != 0 || ! -d /run/systemd/system || $(< /proc/1/comm) != systemd ]]; then
    echo 'Run sudo ./uninstall.sh --yes to remove local Boring software and stop connectors.' >&2
    exit 2
fi
if [[ ! -d /var/lib/boring || $(readlink -- /usr/local/bin/boring || true) != /usr/local/lib/boring/boring.sh ]]; then
    echo 'Expected a Boring installation in /usr/local/lib/boring.' >&2
    exit 5
fi
systemctl disable --now boring-expiry.timer
systemctl stop boring-expiry.service
umask 077
exec 9>>/var/lib/boring/.lock
flock -x 9
shopt -s nullglob
for record in /var/lib/boring/*.json; do
    id=${record##*/}
    id=${id%.json}
    [[ $id =~ ^[0-9a-f]{32}$ ]] || continue
    systemctl disable --now "boring@$id.service"
done
rm -f -- /etc/systemd/system/boring@.service /etc/systemd/system/boring-expiry.service \
    /etc/systemd/system/boring-expiry.timer /usr/local/bin/boring \
    /usr/local/lib/boring/boring.sh /usr/local/lib/boring/lib/{classic,cli,cloudflare,platform_systemd,storage}.py
systemctl daemon-reload
echo 'Local services removed. Inventory, configuration and credentials retained for recovery.'
