#!/usr/bin/env python3
"""Prototype: put the daily brief and the story river on one page.

Renders two candidate layouts from data that already exists — today's archive
day file and today's playbook — so nothing here calls a model or touches the
site build.

  split   the layout as asked: brief and stories side by side, brief wider.
  stacked the alternative: brief full width on top, stories beneath.

The tension worth judging by eye: the two halves have different cadences (daily
vs hourly) and different reading modes (read vs skim). Long prose in a column
is harder to read than the same prose full width, and a story list beside it is
competing for the same attention. `stacked` gives each what it wants at the cost
of the brief pushing the list below the fold.

Usage: python -m experiments.layout
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import markdown as md
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import archive, build as B  # noqa: E402

SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>China Desk — {mode} prototype</title>
<style>
{theme}
/* --- prototype-only layout on top of the utility theme --- */
.wrap{{max-width:{maxw};}}
.cols{{display:grid;gap:2.2rem;grid-template-columns:{cols};align-items:start}}
.brief{{min-width:0}}
.river{{min-width:0}}
.railhead{{font-size:.72rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);border-bottom:2px solid var(--ink);padding-bottom:.3rem;margin:0 0 .5rem}}
/* The brief is prose, so it gets prose treatment: a real measure, larger type,
   looser leading. The river keeps the dense settings from the theme. */
.brief .doc{{font-size:1rem;line-height:1.62;color:var(--ink-2);max-width:40rem}}
.brief .doc h1{{font-size:1.75rem;line-height:1.2;color:var(--ink);letter-spacing:-.02em;
  margin:0 0 .1rem}}
.brief .doc h2{{font-size:.74rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--accent);border-bottom:1px solid var(--rule);padding-bottom:.25rem;
  margin:1.6rem 0 .6rem}}
.brief .doc p{{margin:0 0 .85rem}}
.brief .doc strong{{color:var(--ink);font-weight:700}}
.brief .doc a{{color:var(--seal)}}
.brief .doc ul{{margin:0 0 .9rem;padding-left:1.2rem}}
.brief .doc li{{margin:0 0 .35rem}}
.stamp{{font-size:.76rem;color:var(--ink-3);margin:0 0 1.1rem}}
@media (max-width:900px){{
  .cols{{grid-template-columns:1fr;gap:1.6rem}}
  .brief .doc{{max-width:none}}
}}
</style></head><body><div class="wrap">
<header class="mast">
  <h1>China Desk</h1><span class="tagline">China News, Summarized</span>
  <span class="meta"><span><span class="dot"></span>{built}</span>
  <span>{n} stories</span><span>{mode} prototype</span></span>
</header>
{body}
</div></body></html>
"""

STORY = """<article class="story t{tier}" id="{anchor}">
  <div class="hl"><h3><a href="{link}" rel="noopener nofollow">{title}</a></h3>
  <span class="src">{source}</span><span class="age">{age}</span></div>
  {summary}{also}{details}
</article>"""


def story_html(c, compact: bool) -> str:
    it = c["lead"]
    also = ""
    if c["also"]:
        links = ", ".join(
            f'<a href="{r.link}" rel="noopener nofollow">{r.source_name}</a>' for r in c["also"])
        also = f'<p class="also"><b>Also:</b> {links}</p>'
    details = ""
    if not compact and it.detail:
        pts = "".join(f"<li>{p}</li>" for p in it.detail)
        details = f'<details><summary>More</summary><ul class="body">{pts}</ul></details>'
    return STORY.format(
        tier=c["tier"], anchor=c["anchor"], link=it.link, title=it.title_en,
        source=it.source_name, age=getattr(it, "age", "") or "",
        summary=f'<p class="sum">{it.summary}</p>' if it.summary and not compact else "",
        also=also, details=details)


def main() -> int:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())

    brief_path = ROOT / "experiments" / "playbooks" / f"{today}.md"
    if not brief_path.exists():
        print(f"no brief for {today}", file=sys.stderr)
        return 1
    doc = md.markdown(brief_path.read_text(encoding="utf-8"), extensions=["extra"])

    rows = json.loads((ROOT / "dist" / "data" / f"{today}.json").read_text())
    labels = {s["key"]: s["label"] for s in cfg.get("sections", [])}
    clusters = B.decorate(B.clusters_from_rows(rows), labels)[:int(cfg.get("max_stories") or 34)]
    theme = (ROOT / "templates" / "themes" / "utility.css").read_text()
    built = now.strftime("%d %b %Y, %H:%M UTC")

    variants = {
        # As asked. Brief gets the wider column because prose needs the measure;
        # the river becomes a compact rail — headline, source, age, no summaries.
        # compact=False: the river keeps summaries and the More dropdown. Most
        # readers will not click through to a Chinese-language source, so the
        # bullets are the substance, not a detail view.
        "split": dict(maxw="82rem", cols="minmax(0,1.6fr) minmax(0,1fr)", compact=False),
        # The alternative. Each half gets the full width it wants, at the cost of
        # the list starting below the fold.
        "stacked": dict(maxw="52rem", cols="1fr", compact=False),
    }

    out = ROOT / "experiments" / "layouts"
    out.mkdir(parents=True, exist_ok=True)
    for mode, v in variants.items():
        stories = "\n".join(story_html(c, v["compact"]) for c in clusters)
        if mode == "split":
            body = (f'<div class="cols"><div class="brief">'
                    f'<p class="stamp">Today\'s brief</p><div class="doc">{doc}</div></div>'
                    f'<div class="river"><p class="railhead">The river · {len(clusters)}</p>'
                    f'<main>{stories}</main></div></div>')
        else:
            body = (f'<div class="brief"><div class="doc">{doc}</div></div>'
                    f'<p class="railhead" style="margin-top:2.4rem">'
                    f'Everything else · {len(clusters)}</p><main>{stories}</main>')
        html = SHELL.format(theme=theme, maxw=v["maxw"], cols=v["cols"],
                            mode=mode, built=built, n=len(clusters), body=body)
        (out / f"{mode}.html").write_text(html, encoding="utf-8")
        print(f"  {mode:<8} {len(html):>7} bytes  cols={v['cols']}  compact_river={v['compact']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
