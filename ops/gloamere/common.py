"""Private configuration and Telegram delivery shared by host operations."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def load_config(path: str) -> dict:
    """Load configuration without embedding credentials in source code."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    """Publish complete state atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 120) -> bytes:
    """Run a checked command; avoid including credential-bearing output in errors."""
    result = subprocess.run(command, cwd=cwd, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} failed, exit {result.returncode}")
    return result.stdout


def telegram_token(env_path: str) -> str:
    """Read only the bot credential from Hermes's existing dotenv file."""
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().removeprefix("export ") == "TELEGRAM_BOT_TOKEN":
            parts = shlex.split(value, comments=True)
            token = parts[0] if len(parts) == 1 else ""
            if re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
                return token
    raise RuntimeError("Telegram credential is missing or invalid")


def notify(config: dict, text: str) -> int:
    """Send to the configured owner via the host proxy, without logging bot URLs."""
    token = telegram_token(config["telegram_env"])
    payload = json.dumps({"chat_id": config["chat_id"], "text": text[:4000],
                          "disable_web_page_preview": True}).encode()
    proxy = config.get("proxy", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy} if proxy else {}))
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", payload,
                                     {"Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=40) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Telegram delivery failed ({type(error).__name__})") from None
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the notification")
    return result["result"]["message_id"]


def use_proxy(config: dict) -> None:
    """Apply the configured proxy only to external operations."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[name] = config["proxy"]
