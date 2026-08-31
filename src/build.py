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

    Markdown manufactures active content from its own syntax, with no angle
    bracket anywhere in the source, so escaping tags is not the defence:

        [click](javascript:alert(1))        -> href="javascript:..."
        [x](https://a){: onclick="..." }    -> onclick=, via attr_list in `extra`

    Both were reproduced against an earlier version of this function. The
    defence is: render with the smallest useful extension set — `extra` is
    dropped precisely because it carries attr_list — then sanitise the output
    against an allowlist of tags, attributes and URL schemes.

    An earlier version also escaped `<` and `>` before converting, which was
    both unnecessary and harmful. Unnecessary because nh3 removes `<script>`
    along with its contents and strips every tag outside the allowlist anyway.
    Harmful because escaping turns any tag the model emits into visible text:
    the search plugin's citation markup leaked into a published brief and
    readers saw a literal `<cite index="4-4,4-5">` in the middle of a sentence.
    Sanitising instead of escaping drops the tag and keeps the sentence.
    """
    html = markdown.markdown(md_text, extensions=["sane_lists"])
    clean = nh3.clean(html, tags=_BRIEF_TAGS, attributes=_BRIEF_ATTRS,
                      url_schemes=_BRIEF_SCHEMES, link_rel="noopener nofollow")
    return _add_ask_links(clean)


_TAGS_RE = re.compile(r"<[^>]+>")
_H2_SPLIT = re.compile(r"(?=<h2>)")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
# Sized to carry a whole section, because a truncated one is the thing the
# handoff is meant to avoid: the assistant is asked to explain a passage it can
# only half see. Measured across real briefs, sections run 750 to 3,600
# characters, so 4,000 takes every observed section intact and the
# sentence-boundary trim below only fires on an outlier.
#
# That yields URLs around 6,000 characters. The 2,048 figure this was first
# sized against is an old IIS/proxy limit, not a browser one — Chrome allows
# ~32k and Firefox more. Direct probing of claude.ai and chatgpt.com was
# inconclusive because both answer curl with 403 bot detection at every length,
# including 16,000 characters, which at least means neither edge rejected the
# length before the application saw it.
_ASK_CHARS = 4000


def _trim_to_sentence(text: str, limit: int) -> str:
    """Cut at a sentence end, never mid-word. A prompt that stops in the middle
    of a clause asks the assistant to explain a fragment."""
    if len(text) <= limit:
        return text
    kept, room = [], limit
    for sent in _SENT_SPLIT.split(text):
        if room - len(sent) < 0:
            break
        kept.append(sent)
        room -= len(sent) + 1
    return " ".join(kept) if kept else text[:limit].rsplit(" ", 1)[0]


def _ask_url(host: str, prompt: str) -> str:
    from urllib.parse import quote
    return f"{host}{quote(prompt, safe='')}"


def _ask_block(heading: str, body_text: str, headline: str) -> str:
    """The 'read more about this' handoff for one section of the brief.

    Hands the section to whichever assistant the reader already pays for rather
    than building one in. The prompt carries the brief's headline, the section
    heading and the section's own text, because the assistant cannot see the
    page and a bare "tell me more" produces a bare answer.
    """
    body = _trim_to_sentence(" ".join(body_text.split()), _ASK_CHARS)
    prompt = (
        "I read this section of China Desk's daily brief, which summarises "
        f'Chinese-language news in English. The brief was headlined "{headline}". '
        f'The section is "{heading}" and it said:\n\n{body}\n\n'
        "Give me the background needed to understand this properly: who the "
        "people and organisations are, what led up to it, what is disputed, "
        "and how Chinese and Western coverage of it tend to differ."
    )
    c = _ask_url("https://claude.ai/new?q=", prompt)
    g = _ask_url("https://chatgpt.com/?q=", prompt)
    return (f'<p class="ask sectionask">Go deeper on this section: '
            f'<a href="{c}" target="_blank" rel="noopener nofollow">Claude</a>'
            f'<a href="{g}" target="_blank" rel="noopener nofollow">ChatGPT</a></p>')


def _add_ask_links(clean_html: str) -> str:
    """Append a per-section assistant handoff to the rendered brief.

    Runs after sanitisation, deliberately: the markup added here is ours and
    must not be filtered by the allowlist, while everything it wraps has already
    been through it. The section text is stripped of tags before going into the
    prompt, so nothing the model wrote can smuggle markup into the URL.

    Per section rather than per sentence because selecting a sentence needs
    client-side JS, and the page's CSP denies script outright.
    """
    parts = _H2_SPLIT.split(clean_html)
    if len(parts) < 2:
        return clean_html
    headline = _TAGS_RE.sub("", parts[0].split("</h1>")[0]).strip() or "China Desk"
    out = [parts[0]]
    for part in parts[1:]:
        heading = _TAGS_RE.sub("", part.split("</h2>")[0]).strip()
        body = _TAGS_RE.sub(" ", part.split("</h2>", 1)[-1])
        out.append(part + _ask_block(heading, body, headline))
    return "".join(out)


def og_image(root: Path) -> dict | None:
    """The unfurl image, with its real dimensions, or None if absent.

    Dimensions come out of the PNG header rather than being asserted: a card
    whose declared size disagrees with the file gets cropped or rejected by
    some platforms, and hardcoding 1200x630 would be a guess about a file
    somebody may replace later. Returning None when the file is missing keeps
    the template from emitting a meta tag pointing at a 404 — an unfurl with a
    broken image reads worse than one with no image, and platforms cache it.
    """
    f = root / "static" / "og.png"
    if not f.exists():
        return None
    try:
        head = f.read_bytes()[:24]
        if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
            return None
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
    except OSError:
        return None
    return {"path": "/static/og.png", "w": w, "h": h}


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
        og=og_image(ROOT),
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
    current_brief_day = brief.brief_day(now)
    for day, rows in store.items():
        if day == today or not rows:
            continue
        # Same cap as the front page. decorate() sorts by rank_score, so this
        # is the set that day actually led with — which is the point of an
        # archive page: what Tuesday looked like, not everything Tuesday
        # touched. Uncapped these ran to 178 stories against a 1,400-word
        # brief, a rail four times the length of the column beside it. The
        # full day survives in data/<day>.json regardless.
        day_clusters = decorate(clusters_from_rows(rows), labels)
        if cap:
            day_clusters = day_clusters[:cap]
        # A past day gets its own brief. These are already on disk — history()
        # pulled them forward into dist/brief/ so they stay published — so this
        # costs a file read, not a model call. The brief is the part of a day
        # worth going back for; a day page without it is just a list of links
        # whose stories have already rolled off.
        #
        # Except for the day whose brief is currently on the front page. The two
        # surfaces count days differently: tabs and the story archive run on UTC
        # dates, briefs on China dates twelve hours behind. So from midnight UTC
        # until the next brief generates around 13:37, "Yesterday" and the front
        # page resolve to the same file — about thirteen hours a day of the site
        # showing one brief twice and looking broken. Skipping it here leaves the
        # stories, and the brief reappears on this page once the front page has
        # moved on to a newer one.
        day_html = None
        if day != current_brief_day:
            day_brief = brief.load_brief(out, site_url_early, day)
            if day_brief:
                dres = checks.check_brief(day_brief, generated=False)
                if not [r for r in dres if not r.ok and r.blocking]:
                    day_html = brief_to_html(day_brief)
        (day_dir / f"{day}.html").write_text(
            env.get_template("index.html.j2").render(
                ledger=[], day_tabs=archive.tabs(store, now, current=day),
                archive_day=day, ok_feeds=0, all_feeds=0,
                brief=day_html,
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
