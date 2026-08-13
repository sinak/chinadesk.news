"""Preflight checks. The build publishes only if every blocking check passes.

The governing idea is that a wrong page is worse than a stale page. GitHub Pages
keeps serving the last good deploy when the build step exits non-zero, so
blocking is cheap and shipping something broken is not.

Severity is deliberately split. `block` is for states that make the page wrong or
misleading — untranslated text, a failed ranking call, a collapsed source set.
`warn` is for states that are merely unusual, because a check that fires on
normal operation gets ignored, and then so do the real ones.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from urllib.parse import urlsplit

CJK = re.compile(r"[一-鿿]")
# Derived from the ranker's schema rather than restated. These drifted once —
# the schema gained policy/culture, this list didn't, and a correct build got
# blocked by its own checker. One source of truth removes the whole class.
from src.rank import _ITEM_SCHEMA  # noqa: E402

VALID_SECTIONS = set(_ITEM_SCHEMA["properties"]["section"]["enum"])

MIN_ITEMS = 8            # below this the page isn't worth a deploy
MIN_LIVE_FEEDS = 4       # a collapsed source set means something systemic broke
MIN_HTML_BYTES = 4000    # catches a template that rendered to nothing
MAX_FLAGGED_TITLES = 2   # a couple of visible [UNTRANSLATED] tags is tolerable
MAX_THIN_ITEMS = 3       # a few short dropdowns is variance; twenty is a broken ranker
BRIEF_MIN_WORDS = 900    # half the target; below this the model bailed early
BRIEF_MIN_LINKS = 12     # a brief nobody can check is not worth publishing


@dataclass
class Result:
    name: str
    ok: bool
    blocking: bool
    detail: str = ""


def check_brief(brief: str) -> list[Result]:
    """Gate the daily brief on its own, separately from the page.

    Deliberately not part of `run()`. The brief is one component of the page and
    the story list does not depend on it, so a bad brief should cost the reader
    the brief — not the whole site. `build.py` runs this first and drops the
    brief on failure, which means these are blocking for the brief and invisible
    to the page verdict. A dropped brief is reported loudly; silently shipping a
    degraded page is the failure mode this project has already been bitten by.

    Nothing else covers this text: `no_cjk_in_body` in `run()` walks the story
    items, so a Chinese character in the brief would otherwise ship unseen.
    """
    out: list[Result] = []

    def add(name, ok, blocking, detail=""):
        out.append(Result(name, ok, blocking, detail))

    words = len(brief.split())
    add("brief_nonempty", words >= BRIEF_MIN_WORDS, True,
        f"{words} words (floor {BRIEF_MIN_WORDS})")

    # Hard constraint #1 again, on text the page-level check never sees. Opus in
    # particular likes to gloss a company with its original Chinese name.
    cjk = CJK.findall(brief)
    add("brief_no_cjk", not cjk, True,
        "clean" if not cjk else f"{len(cjk)} Chinese char(s): {''.join(cjk[:12])}")

    # A brief with no links is unfalsifiable — the reader cannot check a single
    # claim. Observed for real: 2,900 words citing nothing.
    links = re.findall(r"\[[^\]]+\]\((https?://[^)]+)\)", brief)
    add("brief_has_links", len(links) >= BRIEF_MIN_LINKS, True,
        f"{len(links)} links (floor {BRIEF_MIN_LINKS})")

    add("brief_has_headline", brief.lstrip().startswith("# "), True,
        "h1 present" if brief.lstrip().startswith("# ") else "no H1")

    hosts = {u.split("/")[2].lower().replace("www.", "") for u in links}
    add("brief_source_spread", len(hosts) >= 6, False, f"{len(hosts)} distinct hosts")
    return out


def run(items, clusters, ledger, rank_status: dict, html: str | None = None,
        feed: str | None = None) -> list[Result]:
    """Preflight the build. `html` and `feed` are optional.

    Called twice: once on the story data alone, before the brief is generated,
    and once on everything after rendering. The first pass is what stops the
    build paying ~$2 for a brief that a failed ranking call was always going to
    prevent publishing — the expensive call now sits behind the cheap gate.
    Checks that need rendered output are simply skipped when it is not there yet.
    """
    out: list[Result] = []

    def add(name, ok, blocking, detail=""):
        out.append(Result(name, ok, blocking, detail))

    # --- the page exists and has substance -------------------------------
    add("items_present", bool(items), True, f"{len(items)} items")
    add("min_items", len(items) >= MIN_ITEMS, True,
        f"{len(items)} items (floor {MIN_ITEMS})")
    if html is not None:
        add("html_rendered", len(html) >= MIN_HTML_BYTES, True,
            f"{len(html)} bytes (floor {MIN_HTML_BYTES})")
    add("clusters_present", bool(clusters), True, f"{len(clusters)} clusters")

    # --- the ranking actually ran ----------------------------------------
    # A failed call silently degrades to heuristics, which emit raw source text
    # in the source language. That must never reach the site.
    add("ranking_succeeded", not rank_status.get("degraded"), True,
        "model call failed — would publish heuristic output" if rank_status.get("degraded")
        else "ok")

    # --- hard constraint #1: English only ---------------------------------
    # Titles that survived the repair pass are marked and therefore visible, so a
    # couple are survivable. Chinese in summary or detail is NOT marked anywhere,
    # so it would ship invisibly — that always blocks.
    flagged = rank_status.get("untranslated", 0)
    add("untranslated_titles", flagged <= MAX_FLAGGED_TITLES, True,
        f"{flagged} flagged (max {MAX_FLAGGED_TITLES})")

    def _detail_text(i):
        d = getattr(i, "detail", "") or ""
        return "\n".join(d) if isinstance(d, list) else d

    leaked = [i for i in items
              if CJK.search(f"{i.summary or ''}{_detail_text(i)}")]
    leaked_detail = "clean"
    if leaked:
        names = ", ".join(sorted({i.source_name for i in leaked})[:4])
        leaked_detail = f"{len(leaked)} item(s) with Chinese in summary/detail: {names}"
    add("no_cjk_in_body", not leaked, True, leaked_detail)

    # --- structural integrity --------------------------------------------
    missing = [i for i in items if not (i.title_en or "").strip()]
    add("titles_present", not missing, True, f"{len(missing)} empty")

    # The ranker has been observed "soft-omitting": deciding an item isn't worth
    # featuring and emptying its fields instead of setting omit=true. That ships
    # bare headlines with no summary and no dropdown. Dropping is a legitimate
    # outcome; a blank entry is not.
    blank = [i for i in items if not (i.summary or "").strip()]
    blank_detail = ", ".join(sorted({i.source_name for i in blank})[:4])
    add("summaries_present", not blank, True,
        f"{len(blank)} item(s) kept with an empty summary: {blank_detail}"
        if blank else "all present")

    # Third costume for the same behaviour: the ranker signals "not worth
    # writing up" by emptying a field rather than setting omit. It has used
    # empty strings, then dropped rows, now empty bullet lists — an empty array
    # satisfies the schema, so only a check catches it. A story that expands to
    # nothing renders with no dropdown at all.
    misshaped = [i for i in items if not isinstance(getattr(i, "detail", None), list)]
    add("detail_is_list", not misshaped, True,
        f"{len(misshaped)} item(s) whose detail is not a list — the template "
        f"would iterate it character by character" if misshaped else "ok")

    # Tolerance, not zero tolerance. This check exists to catch the ranker
    # breaking systemically — detail stopping altogether, or arriving as a
    # string that renders one character per bullet, which has happened. One
    # thin item out of seventy is model variance, and blocking the whole
    # publish over it means the site silently stops updating on an ordinary
    # day. A stale page is worse than a page with one weak dropdown.
    thin = [i for i in items if len(getattr(i, "detail", None) or []) < 2]
    thin_names = ", ".join(sorted({i.source_name for i in thin})[:4])
    add("details_present", len(thin) <= MAX_THIN_ITEMS, True,
        f"{len(thin)} item(s) with fewer than 2 bullets (max {MAX_THIN_ITEMS}): "
        f"{thin_names}" if thin else "all have detail")

    uids = [i.uid for i in items]
    add("no_duplicate_uids", len(uids) == len(set(uids)), True,
        f"{len(uids) - len(set(uids))} duplicates")

    bad_sec = {i.section for i in items} - VALID_SECTIONS
    add("sections_valid", not bad_sec, True, f"unknown: {bad_sec}" if bad_sec else "ok")

    bad_links = [i for i in items
                 if urlsplit(getattr(i, "link", "") or "").scheme not in ("http", "https")]
    add("links_valid", not bad_links, True, f"{len(bad_links)} malformed")

    # --- the feed is a published artifact too ------------------------------
    # An unescaped ampersand in one headline makes the whole document
    # unparseable, and every subscriber's reader goes silent without telling
    # anyone. Parse it here rather than finding out from a reader.
    if feed:
        try:
            ET.fromstring(feed)
            add("feed_wellformed", True, True, f"{len(feed)} bytes, parses")
        except ET.ParseError as exc:
            add("feed_wellformed", False, True, f"XML parse error: {exc}")

    # --- source health ----------------------------------------------------
    live = [f for f in ledger if f["ok"] and f["kept"] > 0]
    add("min_live_feeds", len(live) >= MIN_LIVE_FEEDS, True,
        f"{len(live)}/{len(ledger)} feeds returned items (floor {MIN_LIVE_FEEDS})")

    failed = [f["name"] for f in ledger if not f["ok"]]
    add("all_feeds_ok", not failed, False,
        f"failed: {', '.join(failed)}" if failed else "all ok")

    # --- editorial smells (never blocking) --------------------------------
    multi = sum(1 for c in clusters if c.get("related"))
    add("cluster_density", multi > 0, False,
        f"{multi} multi-source cluster(s) of {len(clusters)}")

    # The US sources exist to be contrasted against. If every one of them is
    # dropped, the cross-origin comparison silently stops happening — which is
    # exactly what a too-broad omit rule did once.
    us_kept = sum(1 for i in items if getattr(i, "origin", "cn") == "us")
    add("us_contrast_alive", us_kept > 0, False,
        f"{us_kept} us-origin item(s) survived omit"
        + ("" if us_kept else " — no cross-origin contrast possible"))

    leads = sum(1 for c in clusters if c["section"] == "lead")
    add("lead_restraint", leads <= 5, False, f"{leads} lead items")

    return out


def report(results: list[Result]) -> tuple[bool, str]:
    """Returns (should_publish, printable summary)."""
    lines = []
    for r in results:
        mark = "PASS" if r.ok else ("FAIL" if r.blocking else "WARN")
        lines.append(f"  [{mark}] {r.name:<22} {r.detail}")
    blocked = [r for r in results if not r.ok and r.blocking]
    return not blocked, "\n".join(lines)
