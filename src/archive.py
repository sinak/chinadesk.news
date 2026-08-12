"""Rolling multi-day archive.

The problem this solves: `dist/` is gitignored and CI checks out clean, so every
build starts with no memory. That is why the original hourly `a/YYYY/MM/DD-HH/`
snapshots only ever contained one entry in production.

The approach: treat the published site as the store. At build start we read back
the day files the last successful build published, merge today's items into
today's bucket, and write all of them out again. No repo writes, no CI changes,
no database. If the site isn't reachable yet — first deploy, or a local run —
history is simply empty and the build proceeds with today only.

The failure mode is deliberately soft. Losing history is bad; refusing to publish
because history couldn't be fetched would be worse, since the front page is the
thing people actually read.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAYS = 7
# Only the fields a rendered page needs. Bodies are the bulk of an item and are
# never displayed, so leaving them out keeps each day file to a few tens of KB.
KEEP_FIELDS = (
    "uid", "title_en", "summary", "detail", "section", "score", "cluster_id",
    "source_name", "published", "url", "link", "link_direct", "proxied",
    "origin", "nature", "china_focus",
)


def day_key(iso: str) -> str:
    """UTC calendar day an item belongs to."""
    try:
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def recent_days(now: datetime, n: int = DAYS) -> list[str]:
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def as_bullets(v) -> list[str]:
    """Coerce a detail field to a bullet list.

    `detail` used to be a string and is now a list. Day files accumulate across
    builds, so a single file can hold both shapes — and a string handed to the
    template's `{% for point in it.detail %}` iterates CHARACTERS, rendering a
    527-char paragraph as 527 single-letter bullets. Normalise on read and on
    write so neither old data nor a stray model response can do that again.
    """
    if isinstance(v, list):
        return [str(b).strip() for b in v if str(b).strip()]
    text = (v or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    return parts if len(parts) > 1 else [text]


def _thin(item) -> dict:
    d = item.dict() if hasattr(item, "dict") else dict(item)
    out = {k: d.get(k) for k in KEEP_FIELDS if k in d}
    out["detail"] = as_bullets(out.get("detail"))
    for extra in ("link", "link_direct", "proxied"):
        if hasattr(item, extra):
            out[extra] = getattr(item, extra)
    return out


def load(out_dir: Path, site_url: str, now: datetime, fetch_remote: bool = True) -> dict:
    """Return {day: [item dicts]} for the retained window.

    Local files win over remote: a local `dist/` is either a developer iterating
    or a CI run that already wrote today, and in both cases it is fresher than
    whatever is currently published.
    """
    days = set(recent_days(now))
    store: dict[str, list[dict]] = {}

    for day in days:
        local = out_dir / "data" / f"{day}.json"
        if local.exists():
            try:
                store[day] = json.loads(local.read_text(encoding="utf-8"))
                continue
            except (ValueError, OSError):
                pass

    if fetch_remote and site_url:
        import requests
        for day in sorted(days - set(store), reverse=True):
            try:
                r = requests.get(f"{site_url}/data/{day}.json", timeout=15)
                if r.status_code == 200:
                    store[day] = r.json()
            except Exception:  # noqa: BLE001 - history is best-effort
                pass
    return store


def merge(store: dict, items, now: datetime) -> dict:
    """Fold this build's items into their calendar days, newest wins on uid.

    Hourly rebuilds mean the same story arrives repeatedly; later passes may have
    a better summary after a body enrichment or repair, so the new copy replaces
    the old rather than being discarded.
    """
    keep = set(recent_days(now))
    for it in items:
        day = day_key(getattr(it, "published", ""))
        if day not in keep:
            continue
        bucket = store.setdefault(day, [])
        thin = _thin(it)
        for n, existing in enumerate(bucket):
            if existing.get("uid") == thin.get("uid"):
                bucket[n] = thin
                break
        else:
            bucket.append(thin)
    return {d: v for d, v in store.items() if d in keep}


def save(store: dict, out_dir: Path) -> None:
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for day, rows in store.items():
        (data_dir / f"{day}.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )


def tabs(store: dict, now: datetime, current: str | None) -> list[dict]:
    """Tab bar model. Today is the front page; earlier days link to /d/<date>."""
    out = []
    today = now.strftime("%Y-%m-%d")
    for day in recent_days(now):
        if day != today and day not in store:
            continue
        dt = datetime.strptime(day, "%Y-%m-%d")
        if day == today:
            label = "Today"
        elif (now.date() - dt.date()).days == 1:
            label = "Yesterday"
        else:
            label = dt.strftime("%a %-d %b")
        out.append({
            "day": day,
            "label": label,
            # Cloudflare Pages strips .html and 308-redirects to the bare path,
            # so link the bare path directly and skip a redirect per click. The
            # file on disk keeps its .html name; only the URL changes.
            "href": "/" if day == today else f"/d/{day}",
            "count": len(store.get(day, [])),
            "current": day == current,
        })
    return out
