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
import re
import sys
from datetime import datetime, timedelta, timezone
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

SECTION HEADINGS ARE YOURS TO WRITE. The subjects below are what to look for,
not a set of boxes to fill and not the words to put on the page. Write a
heading that says what is actually in its section, in the same voice as the
rest of the brief: three to seven words, specific to today, no colon-label
formatting. "Washington answers with a 25-page report" over "WHAT BEIJING IS
SIGNALLING" when the section is about Washington — a standing heading that
contradicts the paragraph under it is worse than no heading, and the reader
notices immediately.

The number of sections is yours too. Four strong ones beat six padded ones.
Merge two subjects when one argument covers both; drop a subject entirely when
the day gave you nothing on it, and do not announce the omission.

WHAT TO LOOK FOR, roughly in this order of prominence:

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

  STATECRAFT — policy, regulation, enforcement, personnel. What the state is
  doing and what it appears to want. Where you see a decision that is really a
  signal, say so, and say who it is aimed at. When the move came from
  Washington rather than Beijing, that is still this subject — say whose move
  it was in the heading rather than filing it under China's.

  THE AI RACE — model releases, labs, chips, compute, funding, deployment. Be
  technical where the material supports it; this reader can follow an
  architecture argument and is bored by a press release.

  THE VIBE — what people are actually arguing about. Consumer mood, labour,
  online controversy, what is trending and what the trending itself reveals.
  This subject is often the most valuable and the most neglected; do not treat
  it as filler.

  COVERAGE CONTRAST — where Chinese and Western reporting of the same story
  diverge. What each side leads with, what each omits. This contrast is the
  single most valuable thing you produce.

  THREADS — where earlier briefs called something, note whether it developed,
  stalled, or went the other way. Say when you were wrong.

  This is the one exception to writing your own headings: its heading MUST
  contain the word "Threads". Tomorrow's brief is built by extracting this
  section from today's and carrying it forward as memory, and that extraction
  finds the section by its name. Rename it and the continuity breaks silently —
  the next brief simply forgets what this one claimed.

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
- Keep paragraphs to roughly 55 words, and never past 75. Count words, not
  sentences — three long sentences is still a wall. When a paragraph runs over,
  do not trim it: split it where the thought turns, and give the second half its
  own **bold lead-in**. Two tight paragraphs beat one dense one, and the second
  lead-in is another handhold for someone skimming.
- Group the day into takes, not a stream. Each section is several distinct
  takes, each two to four short paragraphs long, each opening on its own bold
  lead-in. A reader should be able to stop after any take and have got a whole
  thought. If two adjacent paragraphs are about the same thing, they belong to
  one take; if the subject changes, that is a new take and a new lead-in.
- Name the subject in every section that discusses it. Nobody reads this top
  to bottom. The bold lead-ins invite skimming, so assume every section is
  someone's first — a section that opens "one release, two different stories"
  without saying which release is unreadable to the person who landed there,
  even though you named it eight hundred words earlier. Say "DeepSeek's
  V4-Pro", not "the release"; "Zhu Rongji's death", not "the news". Once per
  section, worked into the sentence, is enough — you are re-anchoring, not
  reintroducing, and repeating it every paragraph reads like a machine.
- Never allude to an event you have not described. This is the same rule as
  glossing a person, applied to things that happened. "Tang Jie's promise to
  Musk" tells a reader who has not followed the story nothing: not who Tang Jie
  is, not what he promised, not when, not why Musk is in the sentence. Either
  state it — "Zhipu chief executive Tang Jie, who said in July that the weights
  would be opened" — or cut the reference entirely. A compressed allusion is
  worse than silence, because it announces there is something to know and then
  withholds it.
- Do not stack possessives. "QbitAI's framing of Tang Jie's promise to Musk"
  makes the reader resolve three references inside one noun phrase before the
  sentence even reaches its verb. Two is the limit, and one is better; break
  the rest into their own sentences. This gets worse in the coverage-contrast
  writing, where outlet, person and event all want to be in the same clause.
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
- Every Chinese figure gets a short gloss the first time they appear — who they
  are and why their involvement matters here. A clause or a short sentence, not
  a biography: "Liang Wenfeng, the former hedge-fund manager who founded
  DeepSeek", "Zhu Rongji, premier from 1998 to 2003 and the architect of the
  state-enterprise layoffs". Assume the reader follows US technology closely
  and China not at all. Xi Jinping is the one exception; he needs no
  introduction. When the figure is the author of a commentary or opinion piece,
  the gloss should place them — the institution they write from and the
  position they are known for — so the reader can weigh the argument. Do not
  invent a credential: if you are not confident who someone is, say what the
  source says about them and no more.

  This holds however briefly the name appears. A passing reference is where it
  matters most: a name dropped once, mid-clause, with no gloss is a name the
  reader can only skip. If a figure is worth naming they are worth placing in
  six words, and if they are not worth six words, use their role instead of
  their name — "Zhipu's chief executive" reads perfectly well and asks nothing
  of the reader.
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
- Markdown only. No HTML tags of any kind, and in particular none of the
  citation markup the search tool uses internally: no <cite>, no index
  attributes, no reference brackets pointing at result numbers. A brief shipped
  with "<cite index="4-4,4-5">" sitting in the middle of a sentence, which is
  what the reader saw. Cite a source the way the rest of the brief does, with
  an inline markdown link, or not at all.
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

Length: 1,200 to 1,500 words. Treat 1,500 as a hard ceiling, not a target you
may drift past — a brief that runs long stops being a brief. This sits beside a
list of thirty-odd stories the reader can go read themselves, so your job is
the argument, not the coverage.

Getting there is a question of how many takes you run, not how compressed each
one is. Cut whole takes rather than thinning all of them: four sharp ones beat
seven hedged ones, and a section with nothing to say today should be dropped
entirely rather than filled. Never shorten by removing the specifics — the
numbers, names and mechanisms are the product. Shorten by covering less.

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
  THREADS — brief. Where earlier briefs called something, note whether it
  developed, stalled, or went the other way. Say when you were wrong. Its
  heading must contain the word "Threads"; tomorrow's brief finds this section
  by name to carry forward as memory.
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

# The brief's day does not start at midnight UTC. Keyed on UTC, the generating
# build was 00:07 UTC — 08:07 in Beijing — so the brief was written before
# China's news day had happened, from a feed still full of yesterday.
#
# The boundary is 12:00 UTC, which is 20:00 in Beijing. That is deliberately
# NOT the end of the China day, and the reason is GitHub's scheduler: it runs
# cron workflows on a best-effort basis and has been observed 1h13m to 2h31m
# late on this repo. Anchoring to midnight Beijing (16:00 UTC, 09:00 Pacific)
# meant generation could not even start until the reader's morning, and the
# delay pushed a finished brief to mid-morning. Anchoring at 20:00 Beijing lets
# the 12:07 UTC slot generate — around 13:37 in practice — so the brief is on
# the site by roughly 07:00 Pacific, before it is read. The cost is the last
# four hours of the Beijing day, which are mostly quiet.
#
# Because China does not observe DST the Beijing anchor never drifts; the
# Pacific times move an hour with US DST.
#
# The label stays honest: a brief generated at 13:37 UTC on the 13th keys to
# 2026-08-13 and covers Beijing's 13th up to that evening.
BRIEF_DAY_OFFSET_HOURS = 12


def brief_day(now: datetime) -> str:
    """The China news day this brief covers."""
    return (now - timedelta(hours=BRIEF_DAY_OFFSET_HOURS)).strftime("%Y-%m-%d")


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


def validate(text: str) -> tuple[bool, str]:
    """Run the publish gate. Imported late to keep the import graph acyclic."""
    from src import checks
    bad = [r for r in checks.check_brief(text) if not r.ok and r.blocking]
    return (not bad), ", ".join(f"{r.name} ({r.detail})" for r in bad)


def load_rejection(out_dir: Path, site_url: str, day: str) -> str | None:
    """Why today's generation attempt was rejected, if it was."""
    local = brief_dir(out_dir) / f"{day}.rejected"
    if local.exists():
        try:
            return local.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            pass
    if not site_url:
        return None
    try:
        import requests
        r = requests.get(f"{site_url}/brief/{day}.rejected", timeout=15)
        if r.status_code == 200 and "html" not in r.headers.get("content-type", "").lower():
            return r.text.strip() or "unknown"
    except Exception:  # noqa: BLE001
        pass
    return None


def save_rejection(out_dir: Path, day: str, reason: str) -> None:
    """Record that this day was attempted and produced unpublishable output.

    Published alongside the briefs so it survives CI's clean checkout, for the
    same reason the briefs are. Without it a brief that fails its gate would be
    regenerated by every build for the rest of the day — six Opus calls instead
    of one, each rejected, all invisible because the page still publishes.
    """
    d = brief_dir(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{day}.rejected").write_text(reason, encoding="utf-8")


_LIST_MARK = re.compile(r"^[-*]\s+")
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]*)\)")
_SENT_END = re.compile(r"(?<=[.!?])\s+")


def _blocks(lines: list[str], want_threads: bool) -> list[str]:
    """Paragraphs and bullets of the threads section, kept as whole units."""
    out: list[str] = []
    buf: list[str] = []
    inside = not want_threads          # when not filtering, take everything
    for line in lines:
        if line.startswith("#"):
            if buf:
                out.append(" ".join(buf))
                buf = []
            if want_threads:
                inside = line.startswith("##") and "THREAD" in line.upper()
            continue
        if not inside:
            continue
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            if buf:
                out.append(" ".join(buf))
                buf = []
            # Strip the list marker only. `lstrip("-* ")` also eats the opening
            # ** of a bold lead-in and leaves its closing ** orphaned mid-line.
            out.append(_LIST_MARK.sub("", s, count=1).strip())
        elif s:
            buf.append(s)
        elif buf:
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))
    return [b for b in out if b]


def _digest(text: str, limit: int = 1200) -> str:
    """Headline plus the threads section — what was claimed, not how it was said.

    Carrying a week of full prose invites the model to imitate its own voice and
    reuse its own framings; we watched a model lift an example headline verbatim
    from this very prompt. Older days therefore contribute memory, not style.

    Trimming happens at unit boundaries. An earlier version sliced the joined
    text at a fixed character count and left the last thread ending mid-word
    ("Doubao now charges 12% on"), which is worse than dropping it: a truncated
    claim reads as a complete one and the model has no way to tell. Whole
    bullets and paragraphs are kept or dropped; if a single block is itself
    over budget it is cut at a sentence end. Link syntax is stripped because
    the digest is for recall, not for citing.
    """
    lines = text.splitlines()
    head = next((l for l in lines if l.startswith("# ")), "").lstrip("# ").strip()

    blocks = _blocks(lines, want_threads=True)
    if not blocks:
        # No threads section — the prompt allows dropping it. Fall back to the
        # opening take, which still records what the day was judged to be about.
        blocks = _blocks(lines, want_threads=False)[:2]

    blocks = [_MD_LINK.sub(r"\1", b).strip() for b in blocks]

    kept: list[str] = []
    used = 0
    for b in blocks:
        if used + len(b) <= limit:
            kept.append(b)
            used += len(b)
            continue
        if kept:
            break
        # First block alone exceeds the budget: keep whole sentences from it.
        room, acc = limit, []
        for sent in _SENT_END.split(b):
            if room - len(sent) < 0:
                break
            acc.append(sent)
            room -= len(sent) + 1
        kept.append(" ".join(acc) if acc else b[:limit].rsplit(" ", 1)[0])
        break

    return f"{head}\n" + "\n".join(f"- {b}" for b in kept) if kept else head


def history(out_dir: Path, site_url: str, now: datetime) -> list[tuple[str, str]]:
    """Recent briefs, most recent first: recent ones whole, older ones digested.

    Also re-publishes every prior brief it finds. `wrangler pages deploy dist`
    serves exactly what is in dist/ and nothing else, and CI checks out clean,
    so a day file that is not rewritten each build silently disappears from the
    site on the next deploy. Without this the archive never accumulates: every
    build would find exactly one prior day forever, the two-full-days tier could
    never fill, and `_digest` would never run at all. `archive.py` re-saves all
    seven retained days for precisely this reason; the brief has to do the same.
    """
    got: list[tuple[str, str]] = []
    for i in range(1, FULL_DAYS + DIGEST_DAYS + 1):
        day = brief_day(now - timedelta(days=i))
        text = load_brief(out_dir, site_url, day)
        if not text:
            continue
        save_brief(out_dir, day, text)      # keep it on the site for tomorrow
        got.append((day, text if len(got) < FULL_DAYS else _digest(text)))
    return got


def ensure(out_dir: Path, site_url: str, now: datetime, window: int,
           cfg: dict, items) -> tuple[str | None, str]:
    """Return (brief_markdown, status). Generates at most once per China day."""
    day = brief_day(now)

    # Carry the archive forward first, on every build rather than only on the
    # one that generates. Five of the six daily builds hit the cache and return
    # early, and each of them deploys — so if prior days were only rewritten on
    # a generating build, the next cached build would drop them again.
    prior = history(out_dir, site_url, now)

    cached = load_brief(out_dir, site_url, day)
    if cached:
        save_brief(out_dir, day, cached)   # re-publish so the file survives
        return cached, f"cached ({len(prior)} prior kept)"

    # Already tried today and the output was unpublishable. Carry the marker
    # forward and stop — regenerating would spend again on every remaining
    # build of the day, and the result was already judged unfit once.
    rejected = load_rejection(out_dir, site_url, day)
    if rejected:
        save_rejection(out_dir, day, rejected)
        return None, f"rejected earlier today, not retrying: {rejected}"

    if not os.environ.get("OPENROUTER_API_KEY"):
        return None, "no api key"
    if not items:
        return None, "no items"

    system = (SYSTEM
              .replace("{USD_CNY}", str((cfg.get("fx") or {}).get("usd_cny", 7.1)))
              .replace("{TODAY}", datetime.strptime(day, "%Y-%m-%d")
                       .strftime("%A, %d %B %Y"))
              .replace("{THREADS}", THREADS if prior else ""))
    text, err = call(system, payload(items, prior))
    if text is None:
        return None, f"failed: {err}"

    # Gate before publishing, not after. Written the other way round, a brief
    # that fails its checks is still deployed to /brief/<day>.md — dropped from
    # the homepage, but served as a file, treated as a valid cache hit by every
    # later build, and fed to tomorrow's model call as history. Only output
    # that passes is ever written as a brief.
    ok, why = validate(text)
    if not ok:
        save_rejection(out_dir, day, why)
        return None, f"rejected: {why}"

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
