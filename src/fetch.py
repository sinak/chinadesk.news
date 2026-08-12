"""Fetch and normalize RSS feeds into a flat list of Item dicts."""
from __future__ import annotations

import calendar
import hashlib
import html
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Any

import feedparser
import requests

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


@dataclass
class Item:
    uid: str
    title: str
    url: str
    source_id: str
    source_name: str
    lang: str
    tier: str
    weight: float
    published: str          # ISO 8601 UTC
    # How much epistemic weight the item carries. The ranking prompt treats these
    # very differently: `forum` and `market` are sentiment, not verified fact.
    nature: str = "reporting"   # reporting | forum | market | aggregator
    origin: str = "cn"          # cn | us — lets the model contrast coverage
    body: str = ""          # plain text, truncated
    # filled in later by rank.py
    title_en: str = ""
    summary: str = ""
    detail: list[str] = field(default_factory=list)
    section: str = "wire"
    score: float = 0.0
    china_focus: str = "core"   # core | adjacent | none — subject, not publisher
    omit: bool = False          # ranker judged it a duplicate of US coverage
    omit_reason: str = ""
    cluster_id: str | None = None
    related: list[dict[str, str]] = field(default_factory=list)

    def dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean(raw: str, limit: int = 4000) -> str:
    if not raw:
        return ""
    text = html.unescape(TAG_RE.sub(" ", raw))
    return WS_RE.sub(" ", text).strip()[:limit]


def _uid(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _parse_date(entry: Any) -> datetime | None:
    """feedparser normalizes *_parsed to UTC, so convert with timegm, not mktime.

    mktime() reads the struct as local time: on a UTC-8 machine it shifted every
    timestamp +8h, which silently widened the window and made every age on the
    page read 8 hours fresher than reality. It happened to be invisible in CI,
    where the runner's local zone is already UTC.
    """
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None)
        if val:
            return datetime.fromtimestamp(calendar.timegm(val), tz=timezone.utc)
    return None


def fetch_feed(feed_cfg: dict, defaults: dict, window_hours: int = 26) -> tuple[list[Item], dict]:
    """Return (items, status). Never raises — failures come back in status."""
    status = {
        "id": feed_cfg["id"],
        "name": feed_cfg["name"],
        "ok": False,
        "http": None,
        "count": 0,
        "kept": 0,
        "capped": 0,
        "error": None,
    }
    headers = {"User-Agent": defaults.get("user_agent", "chinadesk/1.0")}
    try:
        resp = requests.get(
            feed_cfg["url"],
            headers=headers,
            timeout=defaults.get("timeout", 30),
        )
        status["http"] = resp.status_code
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - a dead feed must not kill the build
        status["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return [], status

    parsed = feedparser.parse(resp.content)
    status["count"] = len(parsed.entries)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    drop_paths = feed_cfg.get("drop_patterns") or []
    drop_words = feed_cfg.get("drop_title_keywords") or []
    # Inverse of drop_title_keywords: if set, an item must match at least one of
    # these to be kept. Used for the US feeds, whose front pages are mostly US
    # stories that can never contrast with Chinese coverage.
    keep_words = [w.lower() for w in (feed_cfg.get("keep_keywords") or [])]

    items: list[Item] = []
    for entry in parsed.entries:
        url = (entry.get("link") or "").strip()
        title = _clean(entry.get("title", ""), 400)
        if not url or not title:
            continue

        published = _parse_date(entry)
        if published is None or published < cutoff:
            continue
        if any(p in url for p in drop_paths):
            continue
        if any(w in title for w in drop_words):
            continue
        if keep_words:
            hay = f"{title} {entry.get('summary','') or ''}".lower()
            if not any(w in hay for w in keep_words):
                continue

        body = _clean(
            entry.get("content", [{}])[0].get("value", "")
            if entry.get("content")
            else entry.get("summary", "")
        )

        items.append(
            Item(
                uid=_uid(url),
                title=title,
                url=url,
                source_id=feed_cfg["id"],
                source_name=feed_cfg["name"],
                lang=feed_cfg.get("lang", "en"),
                tier=feed_cfg.get("tier", "wire"),
                weight=float(feed_cfg.get("weight", 1.0)),
                published=published.isoformat(),
                nature=feed_cfg.get("nature", "reporting"),
                origin=feed_cfg.get("origin", "cn"),
                body=body,
            )
        )

    # A high-volume feed would otherwise dominate the ranking payload and crowd
    # out lower-volume, higher-signal sources. Keep the newest `max_items`.
    cap = feed_cfg.get("max_items")
    if cap:
        items.sort(key=lambda i: i.published, reverse=True)
        if len(items) > cap:
            status["capped"] = len(items) - cap
            items = items[:cap]

    status["ok"] = True
    status["kept"] = len(items)
    return items, status


P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
# Boilerplate that leads QbitAI article bodies ("scan to follow us").
BOILERPLATE = ("扫码关注量子位", "点击关注", "版权所有")


def _article_text(html_text: str, limit: int = 4000) -> str:
    """Pull readable text out of an article page via its <p> tags.

    Deliberately generic rather than per-site: a CSS-selector-per-source scheme
    is one redesign away from rotting silently. If this returns too little the
    caller keeps the RSS body, so a miss degrades instead of breaking.
    """
    text = _clean(" ".join(P_RE.findall(html_text)), limit=limit)
    for junk in BOILERPLATE:
        text = text.replace(junk, " ")
    return WS_RE.sub(" ", text).strip()


def enrich_bodies(items: list[Item], defaults: dict, ids: set[str]) -> dict:
    """Fetch article pages for feeds whose RSS body is a stub.

    QbitAI's feed ships ~14 characters, so those items were being summarized
    from the headline alone — which is what let facts bleed in from clustered
    siblings. Failures are non-fatal: we keep whatever the feed gave us.
    """
    targets = [i for i in items if i.source_id in ids and len(i.body) < 600]
    stats = {"attempted": len(targets), "enriched": 0, "failed": 0}
    if not targets:
        return stats

    headers = {"User-Agent": defaults.get("user_agent", "chinadesk/1.0")}
    timeout = defaults.get("timeout", 30)

    def grab(item: Item) -> None:
        try:
            resp = requests.get(item.url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            text = _article_text(resp.text)
            if len(text) > len(item.body):
                item.body = text
                stats["enriched"] += 1
            else:
                stats["failed"] += 1
        except Exception:  # noqa: BLE001 - enrichment is best-effort
            stats["failed"] += 1

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(grab, targets))
    return stats


def fetch_hn(feed_cfg: dict, defaults: dict, window_hours: int = 26) -> tuple[list[Item], dict]:
    """Hacker News front page via Algolia, with top comments as the body.

    The point of carrying HN is the discussion, not the headline — the headline
    is usually already on Techmeme. Comments are what let the ranking step say
    how US readers are reacting versus how Chinese outlets are framing it.
    """
    status = {"id": feed_cfg["id"], "name": feed_cfg["name"], "ok": False,
              "http": None, "count": 0, "kept": 0, "capped": 0, "error": None}
    headers = {"User-Agent": defaults.get("user_agent", "chinadesk/1.0")}
    base = "https://hn.algolia.com/api/v1"
    try:
        resp = requests.get(f"{base}/search?tags=front_page", headers=headers,
                            timeout=defaults.get("timeout", 30))
        status["http"] = resp.status_code
        resp.raise_for_status()
        hits = resp.json().get("hits", [])
    except Exception as exc:  # noqa: BLE001
        status["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return [], status

    status["count"] = len(hits)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    min_points = int(feed_cfg.get("min_points", 0))
    keep_words = [w.lower() for w in (feed_cfg.get("keep_keywords") or [])]
    cap = feed_cfg.get("max_items") or len(hits)

    def build(hit: dict) -> Item | None:
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        title = _clean(hit.get("title") or "", 400)
        ts = hit.get("created_at_i")
        if not title or not ts or (hit.get("points") or 0) < min_points:
            return None
        if keep_words and not any(w in title.lower() for w in keep_words):
            return None
        published = datetime.fromtimestamp(ts, tz=timezone.utc)
        if published < cutoff:
            return None
        pts, ncom = hit.get("points") or 0, hit.get("num_comments") or 0
        parts = [f"Hacker News discussion: {pts} points, {ncom} comments."]
        try:
            c = requests.get(
                f"{base}/search?tags=comment,story_{hit['objectID']}&hitsPerPage=4",
                headers=headers, timeout=defaults.get("timeout", 30),
            ).json()
            for ch in c.get("hits", []):
                body = _clean(ch.get("comment_text") or "", 500)
                if body:
                    parts.append(f"Commenter: {body}")
        except Exception:  # noqa: BLE001 - comments are a bonus, not required
            pass
        return Item(
            uid=_uid(url), title=title, url=url,
            source_id=feed_cfg["id"], source_name=feed_cfg["name"],
            lang=feed_cfg.get("lang", "en"), tier=feed_cfg.get("tier", "wire"),
            weight=float(feed_cfg.get("weight", 1.0)),
            published=published.isoformat(),
            nature=feed_cfg.get("nature", "forum"),
            origin=feed_cfg.get("origin", "us"),
            body=_clean(" ".join(parts)),
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        items = [i for i in pool.map(build, hits) if i]
    items.sort(key=lambda i: i.published, reverse=True)
    if len(items) > cap:
        status["capped"] = len(items) - cap
        items = items[:cap]
    status["ok"] = True
    status["kept"] = len(items)
    return items, status


def fetch_all(config: dict, window_hours: int = 26) -> tuple[list[Item], list[dict]]:
    defaults = config.get("defaults", {})
    all_items: list[Item] = []
    ledger: list[dict] = []
    for feed_cfg in config.get("feeds", []):
        fetcher = fetch_hn if feed_cfg.get("fetcher") == "hn" else fetch_feed
        items, status = fetcher(feed_cfg, defaults, window_hours)
        all_items.extend(items)
        ledger.append(status)

    enrich_ids = {f["id"] for f in config.get("feeds", []) if f.get("fetch_body")}
    if enrich_ids:
        stats = enrich_bodies(all_items, defaults, enrich_ids)
        for entry in ledger:
            if entry["id"] in enrich_ids:
                entry["enriched"] = stats
    # newest first, dedup by url hash
    seen: set[str] = set()
    deduped: list[Item] = []
    for item in sorted(all_items, key=lambda i: i.published, reverse=True):
        if item.uid in seen:
            continue
        seen.add(item.uid)
        deduped.append(item)
    return deduped, ledger
