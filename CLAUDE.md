# China Desk

A Memeorandum-style static aggregator for Chinese tech and AI news, in English,
rebuilt hourly.

## What this is

Chinese tech media breaks stories hours to days before English coverage picks them
up, and much of the best material never crosses over at all. This site pulls the
sources directly, translates and clusters them, and publishes a dense single page.

Reader profile: a US technology executive who wants to understand China, not only
its tech industry. AI and the tech sector matter, and so do the politics, policy,
society and culture that shape them — regulation, censorship, labour, demographics,
consumer mood, what people are arguing about online. A viral controversy or a court
case can outrank a funding round. Telecom is explicitly *not* a beat — carrier,
spectrum and RF news is out of scope even though it is the reader's own industry;
a telecom story belongs here only when it is really a technology story, filed
under whatever section that angle fits. They already read Techmeme and Hacker News, so a story they would have
seen there is worth *less* here, not more — and a story whose US coverage says the
same thing is dropped outright (see `omit` in the ranking prompt).

## Hard constraints — do not violate

1. **English only on the rendered page.** No Chinese characters in titles, summaries,
   or labels. `src/rank.py` has a CJK regex guard that prefixes `[UNTRANSLATED]` to
   anything that slips through. Never remove that guard; fix the prompt instead.
2. **Never reproduce source text.** Summaries and details are written fresh. No
   verbatim sentences from articles, no long quotes. Link out instead.
3. **A failed source is reported, never hidden.** The source ledger at the bottom of
   the page shows every feed's status. Silent misses are worse than visible ones.
4. **The build refuses to publish a bad page.** `src/checks.py` runs a preflight
   suite and `build.py` writes nothing unless every blocking check passes — zero
   items, a failed ranking call, Chinese left on the page, a collapsed source
   set. It exits 1 and leaves the previous site in place. `--skip-checks`
   overrides this for local inspection only and must never be used in CI.

## Architecture

    config/sources.yaml   feeds, weights, filters, sections, blocked hosts
    src/fetch.py          HTTP + feedparser -> Item dataclasses, time-windowed.
                          Also article-body enrichment (fetch_body) and the HN
                          Algolia fetcher, which carries comments as the body.
    src/checks.py         preflight suite; build refuses to publish on failure
    src/rank.py           one batched model call via OpenRouter: translate, summarize,
                          cluster, score (deterministic heuristic fallback, no API key)
    src/translate.py      rewrites zh links to the translate.goog proxy
    src/build.py          orchestrates, renders Jinja2, writes dist/
    templates/            index.html.j2 — all CSS inline, no JS, no build step
    src/archive.py        rolling 7-day store; reads back the last published day
                          files so history survives CI's clean checkout
    dist/                 index.html, feed.xml, d/YYYY-MM-DD.html (day pages),
                          data/YYYY-MM-DD.json (the store), latest.json

Run it: `OPENROUTER_API_KEY=... python -m src.build --out dist`
Options: `--window 26` (hours of history), `--config path`
Model:   `CHINADESK_MODEL` (default `openai/gpt-5.6-luna`),
         `CHINADESK_EFFORT` (default `high`; low/medium/high/xhigh/max)

**Server-side by default; inline progressive enhancement when a feature genuinely
cannot be done at build time.** Expandable stories use native `<details>`/`<summary>`
rather than script. The constraint that matters is *no bundler, no framework, no
npm* — that is what keeps this a Python script and a Jinja template with no
frontend dependency rot, which matters for something that runs unattended.

A short inline `<script>` is fine when it earns its place: client-side filtering
across the story list, a "hide what I've already seen" toggle, that kind of
thing. None of the current features need it. If you find yourself wanting a
build step for the frontend, that is the line — stop and reconsider.

## Source quirks — learned the hard way, don't rediscover

- `qbitai.com/feed/` **requires the trailing slash**. `/feed` returns HTTP 503.
- `36kr.com/feed` is roughly 80% A-share market wire. Filtered in sources.yaml via
  `drop_patterns` (`/newsflashes/`) and `drop_title_keywords`. Roughly 20 of 30 items
  per pull are dropped; that is expected, not a bug.
- **`mp.weixin.qq.com` is robots-blocked.** Never fetch it. Much of the best Chinese
  commentary lives only on WeChat. 36Kr republishes some of it under licence — look
  for the 36Kr version instead.
- **36Kr article pages block plain curl** with a bot interstitial, though the RSS feed
  is fine. Article-level fetches need full browser headers.
- Substack feeds (ChinAI, ChinaTalk, Pekingnology, Ginger River, Sinocism) serve
  **full article text**, not truncated stubs. ChinAI is weekly — most hourly runs will
  legitimately find nothing new from it.
- Feeds carry 20–30 items each, mostly old. Time-windowing is what makes the page
  current; without it the site fills with month-old links.
- XML feeds must be fetched as bytes and parsed. Some HTTP tooling mislabels them as
  binary and returns nothing useful.

## Editorial rules encoded in the ranking prompt

- `lead` is reserved — only genuinely top stories.
- `signal` is for items whose value is a hard number or dataset.
- The `detail` field is behind a dropdown, so it must earn the click: numbers, names,
  mechanism, what's disputed. Never a restatement of the summary.

## Things worth building next

- `src/scrape.py` for tophub.today/c/ai — covers Jiqizhixin, AIbase, HyperAI, CSDN,
  Juejin, none of which have usable feeds. Listed under `scrape:` in sources.yaml
  but not yet implemented.
- Cross-run story memory, so a story that led three hours ago isn't re-led. Requires
  persisting cluster IDs between builds.
