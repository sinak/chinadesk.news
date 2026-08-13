#!/usr/bin/env python3
"""Build the static site. Usage: python -m src.build [--out dist] [--window 26]"""
from __future__ import annotations

import argparse
import collections
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace

import markdown
import nh3
import yaml
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import archive, brief, checks, fetch, rank, translate  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SITE_NAME = "China Desk"


# What the brief is allowed to render. Everything else is stripped. The brief
# needs prose, emphasis, links, lists and headings — nothing that loads a
# resource, carries script, or restyles the page.
_BRIEF_TAGS = {"p", "strong", "em", "b", "i", "a", "ul", "ol", "li",
               "h1", "h2", "h3", "h4", "blockquote", "code", "br", "hr"}
_BRIEF_ATTRS = {"a": {"href", "title"}}
_BRIEF_SCHEMES = {"http", "https", "mailto"}


def brief_to_html(md_text: str) -> str:
    """Markdown -> sanitised HTML for the daily brief.

    The template renders this with `|safe`, the one place on the page that opts
    out of autoescaping — it has to, or the brief's own bold, links and lists
    would render as literal markup. That makes this function the trust boundary,
    and the brief is model prose written from scraped pages, so it is prose an
    attacker can influence.

    Escaping `<` and `>` before conversion is NOT sufficient, which is how this
    was first written and it was wrong. Markdown manufactures active content
    from its own syntax, with no angle bracket anywhere in the source:

        [click](javascript:alert(1))        -> href="javascript:..."
        [x](https://a){: onclick="..." }    -> onclick=, via attr_list in `extra`

    Both were reproduced against the earlier implementation. So: escape first
    (kills literal `<script>` in the source), render with the smallest useful
    extension set — `extra` is dropped precisely because it carries attr_list —
    then sanitise the output against an allowlist of tags, attributes and URL
    schemes. `&` is left unescaped so query strings in link URLs survive; the
    sanitiser is what catches entity-obfuscated schemes like `jav&#x61;script:`.
    """
    escaped = md_text.replace("<", "&lt;").replace(">", "&gt;")
    html = markdown.markdown(escaped, extensions=["sane_lists"])
    return nh3.clean(html, tags=_BRIEF_TAGS, attributes=_BRIEF_ATTRS,
                     url_schemes=_BRIEF_SCHEMES, link_rel="noopener nofollow")


def humanize_age(published_iso: str, now: datetime) -> str:
    try:
        then = datetime.fromisoformat(published_iso)
    except ValueError:
        return ""
    mins = int((now - then).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 0)}m"
    if mins < 60 * 24:
        return f"{mins // 60}h"
    return f"{mins // (60 * 24)}d"


SLUG_RE = re.compile(r"[^a-z0-9]+")


def anchor_for(cluster_id: str, taken: set[str]) -> str:
    """Stable, URL-safe fragment for a story.

    Cluster ids come from the model, so they can contain anything. These are not
    permalinks — a story rolls off the page within a day and the fragment then
    resolves to the top of the homepage, which is the correct degradation.
    """
    base = SLUG_RE.sub("-", (cluster_id or "").lower()).strip("-") or "story"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    taken.add(slug)
    return slug


def decorate(clusters: list[dict], labels: dict) -> list[dict]:
    """Ranking, tiering, anchors and the inline 'Also:' line.

    Shared by the front page and the per-day archive pages so a Tuesday rendered
    from stored data looks identical to the way Tuesday looked live.
    """
    LEAD_FLOAT = 1000.0
    for c in clusters:
        c["label"] = labels.get(c["section"], c["section"].title())
        c["rank_score"] = c["score"] + (LEAD_FLOAT if c["section"] == "lead" else 0.0)
    clusters.sort(key=lambda c: c["rank_score"], reverse=True)

    for i, c in enumerate(clusters):
        c["tier"] = 1 if i < 3 else 2 if i < 15 else 3

    taken: set[str] = set()
    for c in clusters:
        c["anchor"] = anchor_for(c["id"], taken)
        also, seen = [], {c["lead"].source_name}
        for r in c["related"]:
            if r.source_name in seen:
                continue
            seen.add(r.source_name)
            also.append(r)
        c["also"] = also
    return clusters


def clusters_from_rows(rows: list[dict]) -> list[dict]:
    """Rebuild renderable clusters from thinned archive rows."""
    objs = [SimpleNamespace(**r) for r in rows]
    for o in objs:
        o.detail = archive.as_bullets(getattr(o, "detail", None))
        o.age = ""                       # ages are meaningless on a past day
        o.score = float(getattr(o, "score", 0) or 0)
    return rank.build_clusters(objs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist")
    ap.add_argument("--window", type=int, default=26, help="hours of history to include")
    ap.add_argument("--config", default="config/sources.yaml")
    ap.add_argument(
        "--skip-checks", action="store_true",
        help="Render even if preflight fails. Local inspection only — CI must "
             "never pass this, or a broken page reaches the live site.",
    )
    args = ap.parse_args()

    config = yaml.safe_load((ROOT / args.config).read_text())
    now = datetime.now(timezone.utc)

    items, ledger = fetch.fetch_all(config, window_hours=args.window)
    print(f"[build] fetched {len(items)} items from {len(ledger)} feeds")

    if not items:
        print("[build] no items — refusing to overwrite the site", file=sys.stderr)
        return 1

    site_url_early = (config.get("site", {}).get("url") or "").rstrip("/")
    rank_status = rank.rank(items, config.get("fx"))

    # Drop stories the ranker judged to add nothing over the US coverage the
    # reader already sees. `fetched` stays as the honest denominator for the
    # ledger — these were considered and rejected, not never seen.
    fetched = len(items)
    omitted = [i for i in items if getattr(i, "omit", False)]
    items = [i for i in items if not getattr(i, "omit", False)]
    if omitted:
        by_origin = collections.Counter(getattr(i, "origin", "?") for i in omitted)
        print(f"[build] omitted {len(omitted)} of {fetched} item(s): {dict(by_origin)}")
        for i in omitted[:5]:
            print(f"          - [{i.source_name}] {i.omit_reason or 'no reason given'}")

    for it in items:
        translate.apply(it)
        it.age = humanize_age(it.published, now)

    clusters = rank.build_clusters(items)

    # One ranked river, not fixed topic sections. `section` survives as a tag on
    # the row so the ranking prompt's editorial call still shows, but it no longer
    # dictates position. `lead` floats, because the prompt reserves it.
    labels = {s["key"]: s["label"] for s in config.get("sections", [])}
    clusters = decorate(clusters, labels)

    # Trim the tail. Everything below the cap is still archived to the day file,
    # so nothing is lost — it just doesn't compete for attention on the front
    # page. Report the cut rather than hiding it.
    cap = int(config.get("max_stories") or 0)
    if cap and len(clusters) > cap:
        shares = config.get("section_share") or {}
        limits = {k: max(1, int(cap * v)) for k, v in shares.items()}
        picked, deferred, used = [], [], collections.Counter()
        for c in clusters:
            sec = c["section"]
            if len(picked) >= cap:
                break
            if sec in limits and used[sec] >= limits[sec]:
                deferred.append(c)          # over its share; try the next one
                continue
            picked.append(c)
            used[sec] += 1
        # If the capped sections were the only thing left, fill rather than
        # publish a short page — the share is a preference, not a hard rule.
        for c in deferred:
            if len(picked) >= cap:
                break
            picked.append(c)
        picked.sort(key=lambda c: c["rank_score"], reverse=True)
        held = sum(1 for c in deferred if c in picked)
        clusters, cut = picked, [c for c in clusters if c not in picked]
        lo = min(c["score"] for c in clusters)
        note = f", {len(deferred) - held} held back by section share" if deferred else ""
        print(f"[build] trimmed {len(cut)} cluster(s) below the top {cap} "
              f"(lowest kept scored {lo:.0f}{note}; still in the day archive)")

    # Fold today's items into the rolling 7-day store, reading back what the last
    # successful build published so history survives CI's clean checkout.
    store = archive.load(ROOT / args.out, site_url_early, now)
    store = archive.merge(store, items, now)
    day_tabs = archive.tabs(store, now, current=now.strftime("%Y-%m-%d"))

    # Shape-not-specifics for the unfurl. Platforms cache an unfurl for hours to
    # days, so naming today's headlines here would promise stories that have
    # already rotated off by the time anyone clicks. Counts go stale harmlessly.
    site = config.get("site", {})
    site_url = (site.get("url") or "").rstrip("/")
    live_feeds = sum(1 for f in ledger if f["ok"] and f["kept"] > 0)
    only_cn = sum(
        1 for c in clusters
        if all(getattr(m, "origin", "cn") != "us" for m in [c["lead"], *c["related"]])
    )
    # Deliberately narrow wording. `only_cn` means "no US-origin item in THIS
    # build", and the US sample is just Techmeme plus HN — which is not the same
    # as "unreported in English". Naming the two comparators keeps the claim
    # true and is more concrete for a reader who already reads both.
    us_names = sorted({f["name"] for f in ledger
                       if f["kept"] and f["id"] in ("techmeme", "hn")})
    versus = " or ".join(us_names) if us_names else "US tech media"
    site_description = (
        f"{len(clusters)} stories from {live_feeds} Chinese and US sources · "
        f"{only_cn} not picked up by {versus} · rebuilt every four hours"
    )

    for it in items:
        try:
            it.pub_rfc822 = format_datetime(datetime.fromisoformat(it.published))
        except ValueError:
            it.pub_rfc822 = format_datetime(now)

    # Gate on the story data BEFORE spending on the brief. The brief costs about
    # $2; a failed ranking call or a collapsed source set means the page cannot
    # publish no matter how good the brief is, so paying for one first buys
    # nothing. These same checks run again after rendering, against the full
    # page — this pass is purely to fail cheap.
    pre = checks.run(items, clusters, ledger, rank_status)
    pre_bad = [r for r in pre if not r.ok and r.blocking]
    if pre_bad and not args.skip_checks:
        names = ", ".join(r.name for r in pre_bad)
        print(f"[build] BLOCKED before the brief ({names}) — refusing to "
              f"publish, and not paying for a brief that cannot ship",
              file=sys.stderr)
        print(f"[build] preflight (data):\n{checks.report(pre)[1]}")
        return 1

    # The daily brief. Generated at most once per China day and read back from
    # the published site on every later build, so the four-hourly cadence costs
    # one model call a day rather than six. Gated on its own: the story list does
    # not depend on it, so a brief that fails its checks is dropped and the site
    # publishes without it rather than not publishing at all.
    brief_html = None
    if config.get("brief", {}).get("enabled", True):
        brief_md, brief_status = brief.ensure(
            ROOT / args.out, site_url_early, now, args.window, config, items)
        print(f"[build] brief: {brief_status}")
        if brief_md:
            bres = checks.check_brief(brief_md)
            bok, bsummary = checks.report(bres)
            print(f"[build] brief preflight:\n{bsummary}")
            if bok:
                brief_html = brief_to_html(brief_md)
            else:
                bad = ", ".join(r.name for r in bres if not r.ok and r.blocking)
                print(f"[build] BRIEF DROPPED ({bad}) — publishing the story "
                      f"list without it", file=sys.stderr)

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        # Unconditional, NOT select_autoescape(["html"]) — that matches on the
        # filename suffix, and these templates end in `.j2`, so it silently
        # returned False and escaping was off entirely. Every field rendered
        # here is model output derived from untrusted scraped pages, so this is
        # the boundary that stops a crafted headline becoming markup.
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    shared = dict(
        site_name=site.get("title", SITE_NAME),
        tagline=site.get("tagline", "China tech & AI, in English"),
        site_url=site_url,
        theme=site.get("theme", "wire"),
        site_description=site_description,
        clusters=clusters,
        total=len(clusters),
        only_cn=only_cn,
    )
    html = env.get_template("index.html.j2").render(
        ledger=ledger,
        day_tabs=day_tabs,
        archive_day=None,
        ok_feeds=sum(1 for f in ledger if f["ok"]),
        all_feeds=len(ledger),
        built_iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        built_human=now.strftime("%d %b %Y, %H:%M UTC"),
        brief=brief_html,
        **shared,
    )
    feed = env.get_template("feed.xml.j2").render(
        built_rfc822=format_datetime(now), **shared
    )

    # Preflight. Nothing is written until every blocking check passes — a wrong
    # page is worse than a stale one, and exiting non-zero here keeps the last
    # good deploy live because the Pages upload step never runs.
    results = checks.run(items, clusters, ledger, rank_status, html, feed)
    ok, summary = checks.report(results)
    print(f"[build] preflight:\n{summary}")
    if not ok:
        failed = ", ".join(r.name for r in results if not r.ok and r.blocking)
        if not args.skip_checks:
            print(f"[build] BLOCKED by preflight ({failed}) — refusing to publish",
                  file=sys.stderr)
            return 1
        print(f"[build] preflight FAILED ({failed}) but --skip-checks was passed; "
              f"writing anyway. Do not do this in CI.", file=sys.stderr)

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / "feed.xml").write_text(feed, encoding="utf-8")

    # Per-day archive pages. Regenerated every build, not just today's, because
    # the tab bar changes as the window rolls and a stale tab bar on Tuesday's
    # page would link to days that have since been pruned.
    archive.save(store, out)
    day_dir = out / "d"
    day_dir.mkdir(parents=True, exist_ok=True)
    today = now.strftime("%Y-%m-%d")
    for day, rows in store.items():
        if day == today or not rows:
            continue
        day_clusters = decorate(clusters_from_rows(rows), labels)
        (day_dir / f"{day}.html").write_text(
            env.get_template("index.html.j2").render(
                ledger=[], day_tabs=archive.tabs(store, now, current=day),
                archive_day=day, ok_feeds=0, all_feeds=0,
                built_iso=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                built_human=datetime.strptime(day, "%Y-%m-%d").strftime("%d %b %Y"),
                **{**shared, "clusters": day_clusters, "total": len(day_clusters)},
            ),
            encoding="utf-8",
        )
    print(f"[build] archive: {len(store)} day(s) — "
          + ", ".join(f"{d}:{len(v)}" for d, v in sorted(store.items(), reverse=True)))

    # The old a/YYYY/MM/DD-HH/ hourly snapshot is gone. It never worked in
    # production — dist/ is rebuilt from a clean checkout each run, so the
    # published copy only ever held the current hour and every permalink from a
    # previous build 404'd. The day archive above replaces it and actually
    # persists. (It also shadowed the `archive` module name, which is how this
    # got noticed.)
    (out / "latest.json").write_text(
        json.dumps(
            {
                "built": now.isoformat(),
                "ledger": ledger,
                "items": [i.dict() for i in items],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    static = ROOT / "static"
    if static.exists():
        shutil.copytree(static, out / "static", dirs_exist_ok=True)

    # Cloudflare reads _headers and _redirects from the ROOT of the deployed
    # directory, not from a subfolder, so these two are copied up rather than
    # into static/. Cache control matters here beyond the usual: archive.load()
    # re-reads /data/<day>.json off the live site on every build, so a long edge
    # cache would feed the next build stale history.
    for special in ("_headers", "_redirects"):
        src = ROOT / "site" / special
        if src.exists():
            shutil.copy2(src, out / special)

    print(f"[build] wrote {out/'index.html'} — {len(clusters)} clusters")
    failed = [f["name"] for f in ledger if not f["ok"]]
    if failed:
        print(f"[build] FAILED SOURCES: {', '.join(failed)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
