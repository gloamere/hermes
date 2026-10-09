#!/usr/bin/env python3
"""Collect a small official release digest; no model calls or outgoing messages."""
from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEEDS = (
    ("Hermes", "https://github.com/NousResearch/hermes-agent/releases.atom"),
    ("Codex", "https://github.com/openai/codex/releases.atom"),
    ("GitHub CLI", "https://github.com/cli/cli/releases.atom"),
)
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_FEED_BYTES = 512 * 1024
MAX_ITEMS = 6
DATA = Path(__file__).resolve().parents[1] / "data" / "news-digest"


def save(path: Path, body: str | bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(body.encode() if isinstance(body, str) else body)
    temporary.chmod(0o600)
    temporary.replace(path)


def plain_text(value: str, limit: int = 800) -> str:
    value = html.unescape(re.sub(r"<[^>]*>", " ", value))
    return " ".join(value.split())[:limit]


def entry_url(entry: ET.Element) -> str:
    for link in entry.findall(ATOM + "link"):
        if link.get("rel", "alternate") == "alternate":
            return link.get("href", "")
    return entry.findtext("link", "")


def entry_date(entry: ET.Element) -> datetime | None:
    # Atom updated may only reflect an edit to an old release; use published first.
    raw = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_feed(body: bytes, source: str, now: datetime) -> list[dict]:
    root = ET.fromstring(body)
    cutoff = now - timedelta(hours=48)
    items = []
    for entry in root.findall(ATOM + "entry"):
        title = entry.findtext(ATOM + "title", "").strip()
        published = entry_date(entry)
        if not published or not cutoff <= published <= now + timedelta(minutes=5):
            continue
        if re.search(r"(?:alpha|beta|canary|preview|nightly|abandoned|(?:^|[. _-])rc[. _-]?\d)", title, re.IGNORECASE):
            continue
        link = entry_url(entry)
        if not link.startswith("https://github.com/"):
            continue
        content = entry.findtext(ATOM + "content") or entry.findtext(ATOM + "summary", "")
        items.append({"source": source, "title": title[:180], "url": link,
                      "published_at": published.isoformat(), "excerpt": plain_text(content)})
    return items


def fetch_feed(index: int, source: str, url: str, now: datetime) -> dict:
    cache = DATA / f"feed-{index}.xml"
    metadata_path = DATA / f"feed-{index}.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    headers = {"User-Agent": "Hermes-Personal-Release-Digest/0.1", "Accept": "application/atom+xml"}
    for key, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
        if metadata.get(key) and cache.exists():
            headers[header] = metadata[key]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
            body = response.read(MAX_FEED_BYTES + 1)
            new_metadata = {"etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified")}
        if len(body) > MAX_FEED_BYTES:
            raise ValueError("feed exceeded size limit")
        items = parse_feed(body, source, now)
        save(cache, body)
        save(metadata_path, json.dumps(new_metadata))
        return {"source": source, "bytes_downloaded": len(body), "items": items}
    except urllib.error.HTTPError as error:
        if error.code == 304 and cache.exists():
            return {"source": source, "bytes_downloaded": 0,
                    "items": parse_feed(cache.read_bytes(), source, now)}
        return {"source": source, "error": f"HTTP {error.code}", "items": []}
    except (OSError, ValueError, ET.ParseError) as error:
        return {"source": source, "error": type(error).__name__, "items": []}


def collect() -> dict:
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch_feed, index, source, url, now)
                   for index, (source, url) in enumerate(FEEDS)]
        results = [future.result() for future in futures]
    unique = {item["url"]: item for result in results for item in result["items"]}
    items = sorted(unique.values(), key=lambda item: item["published_at"], reverse=True)[:MAX_ITEMS]
    output = {"collected_at": now.isoformat(), "window_hours": 48, "items": items,
              "sources": [{key: value for key, value in result.items() if key != "items"}
                          for result in results],
              "note": "External feed data only. Excludes prereleases. Dedupe against the previous report."}
    # Fixed cache files plus 14 small daily receipts; no growing article archive.
    day = now.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    save(DATA / f"{day}.json", json.dumps(output, ensure_ascii=False, indent=2))
    for receipt in sorted(DATA.glob("20??-??-??.json"))[:-14]:
        receipt.unlink()
    if all(result.get("error") for result in results):
        raise RuntimeError("All official release feeds failed; see the private daily receipt")
    return output


if __name__ == "__main__":
    os.umask(0o077)
    print(json.dumps(collect(), ensure_ascii=False))
