# China Desk

A static aggregator for Chinese news — AI and tech, plus the politics, policy and
culture around them — read in English by someone who doesn't read Chinese. Rebuilt
hourly. No database, no server, no frontend build step.

## Quick start

```bash
pip install -r requirements.txt
export OPENROUTER_API_KEY=sk-or-...
python -m src.build --out dist
open dist/index.html
```

An API key is required to publish. Without one the build still fetches, ranks
heuristically and renders — but preflight blocks the write, because heuristic
output leaves titles in the source language. Pass `--skip-checks` to write it
anyway for local inspection; CI must never pass that flag.

## How it works

1. **`src/fetch.py`** pulls ~19 feeds, drops anything outside the time window and
   anything matching per-source title/URL filters, and caps per-feed volume so one
   firehose can't crowd out the rest. Two extras: `fetch_body` re-fetches the
   article page for feeds whose RSS carries only a stub (QbitAI ships ~14
   characters), and Hacker News comes via the Algolia API so the body carries
   points, comment counts and top comments rather than just a headline.
2. **`src/rank.py`** sends the batch to a model through OpenRouter in one call.
   It translates, writes a summary and a researched `detail`, assigns a section,
   scores importance, clusters related coverage, converts yuan figures to dollars,
   and flags items to omit. A second low-effort pass repairs anything that came
   back with Chinese still in it.
3. **`src/checks.py`** runs preflight. Nothing is written unless every blocking
   check passes.
4. **`src/build.py`** renders one dense page plus `feed.xml` and `latest.json`.

Each item carries `origin` (cn/us) and `nature` (reporting/forum/market/
aggregator). Those drive the two editorial behaviours that make this more than a
translated feed reader: forum and market items are attributed as sentiment rather
than stated as fact, and a story covered on both sides is either dropped as
duplicative or kept *because* the two accounts differ — in which case the
difference is the point of the writeup.

## Preflight

`src/checks.py` runs ~14 blocking checks and a few advisory ones before anything
is written: items present, ranking actually succeeded, no Chinese left on the
page, no empty summaries, no duplicate ids, valid sections and links, a
well-formed feed, and enough live feeds to constitute a page.

A failed build exits non-zero, so the Pages upload step is skipped and the
previous deploy stays live. A stale page beats a wrong one. This has already
caught a real model-call timeout, a heuristic-fallback page, and a schema drift
between the ranker and the checker.

## Deploying

The GitHub Actions workflow builds hourly and publishes to GitHub Pages. Add
`OPENROUTER_API_KEY` to repository secrets and enable Pages with "GitHub Actions"
as the source. Cost is roughly $0.01 per build — about $10/month at hourly cadence.

Two things to know about that schedule: GitHub's scheduled runners are best-effort
and lag under load, and GitHub disables scheduled workflows entirely after 60 days
of repository inactivity. If either matters, drive it from an external trigger
hitting `workflow_dispatch` instead.

## Configuration

Everything tunable lives in `config/sources.yaml`: the site URL used for `og:` tags
and the feed's self-link, the yuan/dollar rate injected into the prompt, feeds with
per-source weights, filters and caps, section labels, and a documented list of hosts
that must never be fetched.

Model and effort are environment variables: `CHINADESK_MODEL` (default
`openai/gpt-5.6-luna`) and `CHINADESK_EFFORT` (default `high`).

See `CLAUDE.md` for architecture, editorial rules, and the source quirks worth
knowing before changing anything.
