#!/usr/bin/env bash
set -euo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
hermes_home=${1:-/home/Gloamere/.hermes}
launcher=${2:-/home/Gloamere/hermes/.hermes/bin/hermes}
[[ -f "$hermes_home/config.yaml" && -x "$launcher" ]] || {
    echo 'Hermes home is missing its configuration or launcher'; exit 1;
}
profile_user=$(stat -c '%U' "$hermes_home")
profile_group=$(stat -c '%G' "$hermes_home")
install -d -m 0755 /usr/local/lib/hermes-ops
install -m 0644 "$source_dir/status_query.py" "$source_dir/common.py" "$source_dir/metrics.py" /usr/local/lib/hermes-ops/
plugin_dir="$hermes_home/plugins/gloamere-status"
install -d -m 0700 -o "$profile_user" -g "$profile_group" "$plugin_dir"
install -m 0600 -o "$profile_user" -g "$profile_group" "$source_dir/gloamere-status/"{__init__.py,plugin.yaml} "$plugin_dir/"

# Use the Hermes writer for config mutations; preserve any existing menu priorities.
mapfile -t menu_settings < <(/usr/bin/python3 - "$hermes_home/config.yaml" <<'PY'
import json, sys, yaml
from pathlib import Path
config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
menu = config.get("platforms", {}).get("telegram", {}).get("extra", {}).get("command_menu", {})
priority = ["server", "backups"] + [name for name in menu.get("priority", []) if name not in ("server", "backups")]
print(min(100, max(62, int(menu.get("max_commands", 60)))))
print(json.dumps(priority))
PY
)
[[ ${#menu_settings[@]} -eq 2 ]] || { echo 'Cannot read Telegram menu settings'; exit 1; }
run_hermes() {
    runuser -u "$profile_user" -- env HERMES_HOME="$hermes_home" "$launcher" "$@"
}
run_hermes plugins enable gloamere-status
run_hermes config set platforms.telegram.extra.command_menu.max_commands "${menu_settings[0]}"
run_hermes config set platforms.telegram.extra.command_menu.priority "${menu_settings[1]}"
run_hermes config set platforms.telegram.extra.command_menu.priority_mode prepend
echo 'Installed /server and /backups. Restart the Hermes gateway when its current turn is idle.'
