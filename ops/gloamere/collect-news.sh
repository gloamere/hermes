#!/usr/bin/env bash
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
exec timeout --kill-after=5s 65s /usr/bin/python3 "$script_dir/news_digest.py"
