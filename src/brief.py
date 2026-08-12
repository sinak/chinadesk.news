#!/usr/bin/env python3
"""China Desk Playbook — a daily written brief instead of a list of links.

Deliberately separate from the site build. It reuses src/fetch.py because there
is no reason to re-implement nineteen feeds, but it shares no prompt, no output
path and no config with build.py, so an experiment here cannot break the thing
that runs hourly.

Three things make this different from the ranking call in src/rank.py:

  1. It asks for prose, not JSON. No schema, no fields — one long piece with a
     voice, which is the whole point of the format.
  2. It gets far more raw material per story. The site sends 2,400 characters
     because it is writing 34 summaries; this is writing one essay and can
     afford the whole article.
  3. It reads the last few days of its own output. A daily brief that cannot
     say "the tightening we flagged Tuesday" is just a longer front page —
     continuity across days is most of what makes Playbook-style writing work.

Usage: OPENROUTER_API_KEY=... python -m experiments.playbook [--days 3]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from src import fetch  # noqa: E402

OUT = ROOT / "experiments" / "playbooks"   # local scratch for --once runs
# Opus writes the better brief — it picks the story with the longest shadow and
# argues rather than summarises. It is ~70x the price of Luna per run, which is
# affordable only because the brief is generated once a day and read back from
# the published site on the other five builds. See `ensure()`.
MODEL = os.environ.get("BRIEF_MODEL", "anthropic/claude-opus-5")
EFFORT = os.environ.get("BRIEF_EFFORT", "xhigh")

SYSTEM = """You write China Desk Playbook: one daily brief on what is actually
happening in China, for a US technology executive who is also an engineer and is
genuinely curious about the country — its politics and culture as much as its AI.

You are given today's raw feed: everything the aggregator pulled from Chinese and
US sources in the last day, with source, language, and the article text itself.
You are also given your own briefs from the previous few days.

WHAT THIS IS
A written brief with a voice, in the register of Politico Playbook or a good
morning newsletter: confident, specific, conversational, fast. Short paragraphs.
Real opinions about what matters and why. You are the person who read everything
so the reader did not have to, and you are telling them what you noticed.

It is NOT a list of headlines with summaries attached. If a section could be
replaced by bullet points without losing anything, rewrite it.

WHAT TO COVER, roughly in this order — but let the day's news set the shape,
and drop or merge sections when there is nothing worth saying:

  THE BIG STORY — open with the one thing that matters most today, and say why
  in the first two sentences.

  Choose it by shadow, not by volume. Your feed is mostly technology trade
  press, so model releases, funding rounds and product launches will always be
  the loudest and most numerous items in it. That is a property of the sources,
  not a measure of importance — do not mistake the shape of the feed for the
  shape of the day. Ask which story will still be shaping how China works in a
  month: a death or a succession near the top of the Party, a regulatory or
  legal decision, a court case, a social rupture, a shift in what the state is
  willing to say out loud. A model release leads only when it changes the
  industry's structure rather than its leaderboard. If the day genuinely
  belongs to a technology story, lead with it — but make that a decision you
  reached, not the default the feed handed you.

  WHAT BEIJING IS SIGNALLING — policy, regulation, enforcement, personnel. What
  the state is doing and what it appears to want. Where you see a decision that
  is really a signal, say so, and say who it is aimed at.

  THE AI RACE — model releases, labs, chips, compute, funding, deployment. Be
  technical where the material supports it; this reader can follow an
  architecture argument and is bored by a press release.

  THE VIBE — what people are actually arguing about. Consumer mood, labour,
  online controversy, what is trending and what the trending itself reveals.
  This section is often the most valuable and the most neglected; do not treat
  it as filler.

  READ DIFFERENTLY — where Chinese and Western coverage of the same story
  diverge. What each side leads with, what each omits. This contrast is the
  single most valuable thing you produce.

  THREADS WE ARE PULLING — brief. Where earlier briefs called something, note
  whether it developed, stalled, or went the other way. Say when you were wrong.

FORMATTING — this is a scannable brief, not an essay. A reader skimming the
bolded fragments alone should come away knowing the day.
- Open most paragraphs with a **bold lead-in** of two to six words carrying the
  point, then continue in plain text. "**Alibaba went first.** At 2am Beijing
  time..." Not a label; the actual first words of the sentence.
- Bold every company, person, agency and hard number on first appearance in a
  section. Prices, percentages, parameter counts, headcounts, dollar figures.
- Use short ALL-CAPS bold tags where they earn it, sparingly and never more than
  a few per brief: **THE NUMBER:**, **WHY IT MATTERS:**, **WHAT WE'RE WATCHING:**,
  **THE TELL:**, **WORTH NOTING:**.
- Two to four sentences per paragraph. Break anything longer.
- Em dashes for asides. A one-sentence paragraph for emphasis is good.
- A tight bulleted run is fine where the material genuinely is a list — a spec
  sheet, three funding rounds, four provinces doing the same thing. Give each
  bullet a **bold lead-in** too. Do not bullet an argument.

VOICE — never address the reader as a category or narrate your own analysis.
Banned: "the implication for a technology executive", "readers should note",
"what this means for you", "it is worth understanding that", "the key takeaway
is". Do not announce that something is important, significant or noteworthy —
show why and let it land. Say the thing directly: not "the implication is that
frontier intelligence is becoming cheap", just "frontier intelligence is
becoming cheap". Cut every sentence whose only job is to introduce the next one.

SEARCH — you have web search. It is a fact-checker and a comparison tool, not a
source of narrative. The Chinese material in today's feed is the spine of this
brief and search never replaces it.

  Use it to:
  - Verify a hard figure before you state it. If a specification, price,
    parameter count, headcount or date is not in the provided text and you
    cannot confirm it by searching, leave the number out rather than recalling
    it. An unsourced precise-looking figure is the worst thing you can publish.
  - Find how Western outlets covered a story you are contrasting, so the
    coverage comparison works on any story rather than only the ones a US
    source happened to land in today's feed.
  - Check background about a named company, person, agency or prior event when
    you would otherwise be writing from memory.

  Do NOT use it to:
  - Find stories. Today's feed defines what this brief covers; a story that is
    not in the feed does not belong in the brief.
  - Build the narrative out of English-language reporting. If your account of a
    Chinese story reads like it came from Reuters, you have gone wrong — the
    point of this page is what the Chinese press said, not what the wires said
    about it.

  Search a handful of times, not dozens. Cite what you verified.

HOW TO WRITE IT
- Convert every yuan figure to approximate dollars inline at {USD_CNY} to the
  dollar, keeping the original, and handle 万 and 亿 so the reader never counts
  zeros. Convert Chinese units of area, distance and weight to US customary
  units the same way — mu and hectares mean nothing to this reader; acres and
  square miles do.
- Name companies, people, agencies and numbers. Specificity is the product.
- Every Chinese figure gets a short gloss on first mention — who they are and
  why their involvement matters here. A clause or a short sentence, not a
  biography: "Liang Wenfeng, the former hedge-fund manager who founded
  DeepSeek", "Zhu Rongji, premier from 1998 to 2003 and the architect of the
  state-enterprise layoffs". Assume the reader follows US technology closely
  and China not at all. Xi Jinping is the one exception; he needs no
  introduction. When the figure is the author of a commentary or opinion piece,
  the gloss should place them — the institution they write from and the
  position they are known for — so the reader can weigh the argument. Do not
  invent a credential: if you are not confident who someone is, say what the
  source says about them and no more.
- Attribute forum and market chatter as sentiment, never as fact.
- Claims about what happened must come from the provided text. Background you
  are confident about may come from your own knowledge, phrased as background.
  If you are unsure, say so plainly or leave it out. Never invent a detail, an
  affiliation, or a quotation.
- No Chinese characters in the output, anywhere, for any reason. Translate, do
  not transliterate. This includes the courtesy of giving a company's or
  person's original name in parentheses — do not do it, not even once, not even
  as a gloss. If the Chinese name carries meaning worth having, say what it
  means in English words ("a name taken from the linguistics term pragmatics")
  and never show the characters. A single character on the page fails the
  build.
- Link out inline as markdown. Every paragraph resting on a specific story
  carries that story's link — a reader who wants to check you should never have
  to go looking. Where a claim came from a search rather than the feed, link the
  source you actually read. The link text must name what is actually at the
  other end — never label a link with a publication you did not link to. If you
  have no link for a claim, say the claim without one rather than attaching the
  nearest URL to hand.
- Have a view. A brief that refuses to say what it thinks is worthless. But
  distinguish clearly between what is reported and what you are inferring.

Today's date is {TODAY} (UTC). Use it to judge what counts as recent — not as
something to print. Chinese sources are eight hours ahead, so their timestamps
may read as tomorrow; that does not change today's date.

Aim for 1,800 to 2,200 words. That is long enough to carry an argument and
short enough to read over coffee. Do not pad to reach it — if a section has
nothing worth saying today, cut it and give the space to one that does.

Return GitHub-flavoured markdown. Open with an H1 that states today's argument
in its own words — a sentence a reader could disagree with, not a label for the
contents. "X and Y dominate the day" names the topics; a real headline says
what you think is true about them. No date and no dateline prefix; the page
carries the date already. Never the word "Playbook". No preamble, no
meta-commentary about your own process."""

# Only offered when there is genuinely a history to refer back to. The first
# brief wrote four "accelerated / developed / stalled" threads with no prior
# briefs in context — continuity invented on day one.
THREADS = """
  THREADS WE ARE PULLING — brief. Where earlier briefs called something, note
  whether it developed, stalled, or went the other way. Say when you were wrong.
"""


def call(system: str, user: str) -> tuple[str | None, str | None]:
    """Own request path rather than rank._request, which parses JSON and returns
    rows. Keeping it local also means an experiment here cannot change the code
    the hourly site build depends on."""
    import requests

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                "content-type": "application/json",
                "HTTP-Referer": "https://chinadesk.news",
                "X-Title": "China Desk Brief",
            },
            json={
                "model": MODEL,
                "reasoning": {"effort": EFFORT},
                "max_tokens": 60000,
                # Scoped by the SEARCH section of the system prompt: verify
                # figures, find the Western framing to contrast against, check
                # background. Never to find stories or to build the narrative.
                "plugins": [{"id": "web", "max_results": 5}],
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=1800,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"upstream: {data['error']}")
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise RuntimeError("truncated at max_tokens")
        usage = data.get("usage") or {}
        reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        print(f"[brief] in={usage.get('prompt_tokens')} "
              f"out={usage.get('completion_tokens')} reasoning={reasoning}", file=sys.stderr)
        return (choice["message"].get("content") or "").strip(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def gather(window: int) -> tuple[list, str]:
    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
    items, ledger = fetch.fetch_all(cfg, window_hours=window)
    live = sum(1 for f in ledger if f["ok"] and f["kept"])
    print(f"[brief] {len(items)} items from {live}/{len(ledger)} live feeds", file=sys.stderr)
    return items, cfg


def payload(items, prior: list[tuple[str, str]]) -> str:
    """Today's feed plus the last few briefs, as plain text rather than JSON.

    Prose in, prose out: a JSON envelope here just spends tokens on punctuation
    and nudges the model toward writing structured records instead of an essay.
    """
    parts = []
    if prior:
        parts.append("=== YOUR PREVIOUS BRIEFS (most recent first) ===\n")
        for day, text in prior:
            parts.append(f"--- {day} ---\n{text}\n")
    parts.append(f"\n=== TODAY'S RAW FEED ({len(items)} items) ===\n")
    for it in items:
        parts.append(
            f"\n[{it.source_name} | {it.origin} | {it.nature} | {it.published[:16]}]\n"
            f"TITLE: {it.title}\n"
            f"URL: {it.url}\n"
            f"TEXT: {it.body[:4000]}\n"
        )
    return "".join(parts)


def prior_briefs(n: int) -> list[tuple[str, str]]:
    if not OUT.exists():
        return []
    files = sorted(OUT.glob("*.md"), reverse=True)[:n]
    return [(f.stem, f.read_text(encoding="utf-8")) for f in files]


# --- production persistence -------------------------------------------------
# The brief is generated once a day but the site rebuilds every four hours, so
# every build after the first must read back what the day already produced
# rather than pay for it again. CI checks out clean, so "read back" has to mean
# the published site, exactly as src/archive.py does for the story store.

FULL_DAYS = 2       # prior briefs carried in full, for genuine continuity
DIGEST_DAYS = 5     # older ones carried as headline + threads only


def brief_dir(out_dir: Path) -> Path:
    return out_dir / "brief"


def looks_like_brief(text: str) -> bool:
    """Cheap shape check: is this a brief, or is it something else entirely?

    Necessary because Cloudflare Pages answers a request for a file that does
    not exist with `200 OK` and the body of index.html, rather than a 404. A
    status check alone therefore reads the site's own homepage back as today's
    brief — and because a cache hit is re-published, that HTML would then be
    served as the brief and read back again on every later build. Observed in
    production: an 8,424-word "brief" with no headline and no links, which the
    preflight gate caught and dropped.

    Only structural facts are checked, not quality — `checks.check_brief` is the
    authority on whether a real brief is good enough to publish. This just
    answers "did we get a markdown brief at all".
    """
    t = (text or "").lstrip()
    if not t.startswith("# "):
        return False
    head = t[:400].lower()
    if "<!doctype" in head or "<html" in head:
        return False
    return "](http" in t          # a brief always links out


def load_brief(out_dir: Path, site_url: str, day: str) -> str | None:
    """Today's brief if it already exists locally or on the live site."""
    local = brief_dir(out_dir) / f"{day}.md"
    if local.exists():
        try:
            text = local.read_text(encoding="utf-8").strip()
            if looks_like_brief(text):
                return text
            # A poisoned local file would otherwise survive every later build.
            print(f"[brief] local {day}.md is not a brief — ignoring it",
                  file=sys.stderr)
        except OSError:
            pass
    if not site_url:
        return None
    try:
        import requests
        r = requests.get(f"{site_url}/brief/{day}.md", timeout=15)
        ctype = r.headers.get("content-type", "")
        if r.status_code != 200:
            return None
        if "html" in ctype.lower():
            return None           # Pages' 200-with-index.html for a missing file
        if looks_like_brief(r.text):
            return r.text.strip()
        print(f"[brief] {site_url}/brief/{day}.md returned "
              f"{len(r.text)} chars that are not a brief — regenerating",
              file=sys.stderr)
    except Exception:  # noqa: BLE001 - a missing brief just means generate one
        pass
    return None


def save_brief(out_dir: Path, day: str, text: str) -> None:
    d = brief_dir(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.md").write_text(text, encoding="utf-8")


def _digest(text: str) -> str:
    """Headline plus the threads section — what was claimed, not how it was said.

    Carrying a week of full prose invites the model to imitate its own voice and
    reuse its own framings; we watched a model lift an example headline verbatim
    from this very prompt. Older days therefore contribute memory, not style.
    """
    lines = text.splitlines()
    head = next((l for l in lines if l.startswith("# ")), "").lstrip("# ").strip()
    out, keep = [], False
    for l in lines:
        if l.startswith("## "):
            keep = "THREAD" in l.upper()
            continue
        if keep and l.strip():
            out.append(l.strip())
    body = " ".join(out)[:1200]
    return f"{head}\n{body}".strip()


def history(out_dir: Path, site_url: str, now: datetime) -> list[tuple[str, str]]:
    """Recent briefs, most recent first: recent ones whole, older ones digested."""
    from datetime import timedelta
    got: list[tuple[str, str]] = []
    for i in range(1, FULL_DAYS + DIGEST_DAYS + 1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        text = load_brief(out_dir, site_url, day)
        if not text:
            continue
        got.append((day, text if len(got) < FULL_DAYS else _digest(text)))
    return got


def ensure(out_dir: Path, site_url: str, now: datetime, window: int,
           cfg: dict, items) -> tuple[str | None, str]:
    """Return (brief_markdown, status). Generates at most once per UTC day."""
    day = now.strftime("%Y-%m-%d")

    cached = load_brief(out_dir, site_url, day)
    if cached:
        save_brief(out_dir, day, cached)   # re-publish so the file survives
        return cached, "cached"

    if not os.environ.get("OPENROUTER_API_KEY"):
        return None, "no api key"
    if not items:
        return None, "no items"

    prior = history(out_dir, site_url, now)
    system = (SYSTEM
              .replace("{USD_CNY}", str((cfg.get("fx") or {}).get("usd_cny", 7.1)))
              .replace("{TODAY}", now.strftime("%A, %d %B %Y"))
              .replace("{THREADS}", THREADS if prior else ""))
    text, err = call(system, payload(items, prior))
    if text is None:
        return None, f"failed: {err}"

    save_brief(out_dir, day, text)
    return text, f"generated ({len(text.split())} words, {len(prior)} prior)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=26)
    ap.add_argument("--days", type=int, default=3, help="prior briefs to carry")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("[brief] OPENROUTER_API_KEY unset", file=sys.stderr)
        return 1

    items, cfg = gather(args.window)
    if not items:
        print("[brief] no items", file=sys.stderr)
        return 1

    prior = prior_briefs(args.days)
    if prior:
        print(f"[brief] carrying {len(prior)} prior brief(s): "
              f"{', '.join(d for d, _ in prior)}", file=sys.stderr)

    today = datetime.now(timezone.utc)
    system = (SYSTEM
              .replace("{USD_CNY}", str((cfg.get("fx") or {}).get("usd_cny", 7.1)))
              .replace("{TODAY}", today.strftime("%A, %d %B %Y"))
              .replace("{THREADS}", THREADS if prior else ""))
    user = payload(items, prior)
    print(f"[brief] prompt ~{len(user)//1000}k chars, model={MODEL} effort={EFFORT}",
          file=sys.stderr)

    text, err = call(system, user)
    if text is None:
        print(f"[brief] FAILED: {err}", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    day = today.strftime("%Y-%m-%d")
    path = OUT / f"{day}.md"
    path.write_text(text, encoding="utf-8")
    print(f"[brief] wrote {path} — {len(text)} chars, ~{len(text.split())} words")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
