"""Retention decisions for dated encrypted backup releases."""
from __future__ import annotations

import re
from datetime import datetime

TAG_PATTERN = re.compile(r"hermes-backup-(\d{4}-\d{2}-\d{2})T(\d{6})")


def plan_retention(releases: list[dict], now: datetime, *, daily_days: int,
                   monthly_months: int, max_bytes: int) -> tuple[list[dict], list[dict]]:
    """Keep daily and monthly snapshots within a total quota, always keeping the newest."""
    if daily_days < 1 or monthly_months < 0 or max_bytes < 1:
        raise ValueError("Invalid retention limits")
    owned = []
    for release in releases:
        match = TAG_PATTERN.fullmatch(release["tag_name"])
        if match:
            created = datetime.strptime("".join(match.groups()), "%Y-%m-%d%H%M%S").replace(tzinfo=now.tzinfo)
            owned.append((created, release))
    owned.sort(key=lambda item: item[0], reverse=True)
    if not owned:
        return [], []
    kept = []
    seen_days = set()
    seen_months = set()
    for created, release in owned:
        month_age = (now.year - created.year) * 12 + now.month - created.month
        daily = 0 <= (now.date() - created.date()).days < daily_days and created.date() not in seen_days
        monthly = 0 <= month_age < monthly_months and (created.year, created.month) not in seen_months
        if daily or monthly or not kept:
            kept.append(release)
        seen_days.add(created.date())
        seen_months.add((created.year, created.month))
    size = lambda release: sum(asset["size"] for asset in release.get("assets", []))
    if size(kept[0]) > max_bytes:
        raise ValueError("Newest backup exceeds the entire retention quota")
    total = sum(size(release) for release in kept)
    while total > max_bytes and len(kept) > 1:
        total -= size(kept.pop())
    tags = {release["tag_name"] for release in kept}
    removed = [release for _, release in owned if release["tag_name"] not in tags]
    return kept, removed
