#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -m 0755 /usr/local/lib/hermes-ops
install -m 0644 "$source_dir"/*.py "$source_dir/VERSION" /usr/local/lib/hermes-ops/
install -m 0755 "$source_dir/hermes-backup" /usr/local/sbin/hermes-backup
install -m 0644 "$source_dir"/systemd/*.service "$source_dir"/systemd/*.timer /etc/systemd/system/
for unit in hermes-gateway mihomo hermes-backup netdata; do
    install -d -m 0755 "/etc/systemd/system/$unit.service.d"
    install -m 0644 "$source_dir/systemd/alerts.conf" "/etc/systemd/system/$unit.service.d/alerts.conf"
done
install -m 0644 "$source_dir"/netdata/*.conf /etc/netdata/
touch /etc/netdata/.opt-out-from-anonymous-statistics
install -m 0644 "$source_dir/logrotate.conf" /etc/logrotate.d/hermes-ops
install -d -m 0700 /etc/hermes-ops /var/lib/hermes-ops
systemctl daemon-reload
systemctl restart netdata
echo 'Installed. Configure private JSON files, validate a backup/report, then enable timers.'
