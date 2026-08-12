"""Translate titles, cluster related stories, assign sections, and score.

Primary path: one batched model call over all items, via OpenRouter.
Fallback path: deterministic heuristics, so the site still builds with no API key
(titles stay in the source language — acceptable for a smoke test, not for prod).

Reasoning effort is the main quality dial. Clustering is the part that needs it:
grouping two outlets' coverage of one story means holding every item in view at
once. Sweep CHINADESK_EFFORT (low/medium/high/xhigh/max) if clusters come back
as singletons — if the count doesn't move, the cluster slugs in the prompt are
too specific and no amount of effort will fix that.
"""
from __future__ import annotations

import json
import os
import re
import sys

MODEL = os.environ.get("CHINADESK_MODEL", "openai/gpt-5.6-luna")
EFFORT = os.environ.get("CHINADESK_EFFORT", "high")
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Reasoning tokens bill as output and count against max_tokens, so this has to
# cover the model's thinking *plus* ~4k of JSON. Too low and the JSON truncates
# mid-array, which lands in the silent-fallback path below and ships a page of
# untranslated Chinese. Only tokens actually used are billed.
MAX_TOKENS = 100000

# A single deep-detail pass over ~100 items runs 10-15 minutes, which the old
# 600s ceiling cut off mid-generation. Raised, but the real fix is to stop
# doing this as one enormous request — see the two-pass note in rank()'s
# docstring. A build this slow also risks overlapping the hourly cron.
REQUEST_TIMEOUT = 1800

_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        # A small integer, not the 12-char hex uid. Echoing opaque hashes is
        # where this model demonstrably fails: across two runs on identical
        # input it returned 109 rows then 41, with truncated ids like
        # "8775409d44b" and corrupted ones like "59b?". Copying "37" is a
        # far easier task and the caller maps back by position.
        "id": {"type": "integer"},
        "title_en": {"type": "string"},
        "summary": {"type": "string"},
        # A list, not prose: the dropdown renders one bullet per entry, and an
        # array is enforced by the schema where "use newlines" would not be.
        "detail": {"type": "array", "items": {"type": "string"}},
        "section": {
            "type": "string",
            "enum": ["lead", "ai", "policy", "culture", "analysis",
                     "signal", "wire"],
        },
        "score": {"type": "number"},
        "cluster": {"type": "string"},
        # What the story is ABOUT, as distinct from which press ran it. A US
        # newsletter about Chinese policy is core; a Chinese site translating a
        # BBC story about Australia is none.
        "china_focus": {"type": "string", "enum": ["core", "adjacent", "none"]},
        "omit": {"type": "boolean"},
        "omit_reason": {"type": "string"},
    },
    "required": ["id", "title_en", "summary", "detail", "section", "score",
                 "cluster", "china_focus", "omit", "omit_reason"],
    "additionalProperties": False,
}

# Wrapped in an object because json_schema mode needs an object at the top level.
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "ranked_items",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"items": {"type": "array", "items": _ITEM_SCHEMA}},
            "required": ["items"],
            "additionalProperties": False,
        },
    },
}

SYSTEM = """You are the editor of a China news aggregator for a US technical audience.
Output is English only — no Chinese characters anywhere in your output, including in
titles, summaries, or cluster labels. Translate every Chinese title into natural
English; do not transliterate.

The reader is a technology executive who is also an engineer: technically fluent,
so a real architectural or research detail lands where a press release does not,
and genuinely curious about China — its politics and its culture as much as its
AI. They want to understand how the place works. Regulation, censorship, labour,
demographics, consumer mood, what people are arguing about online: these are not
filler around the technology stories, they are half the reason to read the page.

They already read Techmeme and Hacker News, so anything they would have seen
there is worth less here, not more.

BALANCE. Left alone this page drifts into an AI trade sheet. Aim for roughly a
quarter AI and at least as much politics, policy and society combined. When an
ordinary AI story and an ordinary policy or social story compete for the same
slot, take the policy or social one — the AI story is the one the reader is most
likely to have seen elsewhere.

BE SELECTIVE. A short page of things worth knowing beats a long one padded with
things that merely happened. The test for every item is whether this reader would
be glad they read it — did it teach them something about how China works, or
about a technology, that they did not already know? A product refresh, an
incremental funding round, a rehashed press release and a minor personnel move
all fail that test even when they are technically China news. Use the whole
0-100 score range and use the bottom of it: most items are not important, and
scoring everything 50-70 tells the page nothing. Reserve 80+ for stories that
genuinely matter and score the routine below 30 so it can be cut.

For each input item produce:
  id        - echo back the SAME integer you were given for this item.
              Return exactly one entry per input item, no more, no fewer.
  title_en  - natural English headline, under 90 chars, no trailing period
  summary   - what happened and why it matters. Normally one sentence under 200
              chars. When the item carries a coverage contrast (see below) it may
              run to two sentences and 320 chars — three rendered lines is a fine
              price for the most interesting thing on the page.
  detail    - the substance of the page. See DETAIL below.
  section   - one of: lead, ai, policy, culture, analysis, signal, wire
  score     - 0-100 importance for THIS reader
  cluster   - short lowercase slug shared by items about the same underlying
              EVENT, e.g. "bytedance-data-dept". Cluster on what happened, not on
              the angle an outlet took. If three outlets cover one company going
              independent and the third frames it as a piece about founders in
              general, that is still the same event and the same cluster — the
              framing difference belongs in the detail, not in a separate story.
              Unique slug only when the story genuinely stands alone.
  china_focus - "core", "adjacent" or "none". See CHINA FOCUS below.
  omit      - true if this item should not appear on the page. See OMIT below.
  omit_reason - one short clause if omit is true, otherwise "".

CHINA FOCUS — the most important judgement you make, and it is about the SUBJECT
of the story, not about which outlet published it.
- "core"     — the story is about China: a Chinese company, product, lab or
               researcher; the Party, the government, a ministry or a regulator;
               Chinese society, labour, culture, demographics or online life;
               Chinese industry, supply chains or infrastructure.
- "adjacent" — a foreign story with a SUBSTANTIVE China dimension: export
               controls, a foreign company's China operations, a rivalry where
               China is one side of it. A Chinese outlet merely commenting on
               foreign news does not make it adjacent. A foreign personnel move,
               a foreign company's internal reorganisation or a foreign product
               launch stays "none" no matter how much Chinese commentary it
               attracts — Chinese pundits having opinions about a US executive
               is not a China story, and should rarely score above 40.
- "none"     — no China dimension at all. A Chinese site translating a wire
               story about Australian politics is "none". So is an English
               China-watching newsletter writing about its own contest, hiring
               or events — the publisher being China-focused does not make that
               particular item China news.

Rank on it. A Politburo or State Council decision, a regulatory or enforcement
move, a personnel change near the top of the Party, or a Chinese company doing
something technically significant — a Unitree launch, a new model from a Chinese
lab, a domestic chip milestone — is precisely what this page exists to surface,
and belongs above a routine funding round or a product refresh. Chinese politics
and governance are not a side dish here; weight them like AI, not below it.

DETAIL — a list of bullets, each one idea, shown when the reader expands a story.

Write for someone who runs a technology company and can read a system diagram.
They are deciding what to pay attention to, not following instructions. So:

  WRITE ABOUT      the strategic shape of the story — who is positioning against
                   whom, what it signals about where an industry or a policy is
                   heading, what a state actor appears to want; the technical
                   substance where there is any, in real terms; and above all how
                   Chinese coverage frames it and what that framing reveals,
                   placed in the wider context of how China treats this kind of
                   story.
  DO NOT WRITE     operational and procedural detail. Deadlines, backup windows,
                   deletion schedules, step-by-step instructions, service notices,
                   who must click what by when. A reader running a company does
                   not need the backup deadline; they need to know why Beijing
                   blocked the deal and what that implies for the next one.

Scale the number of bullets to the score you gave the item:
  score 80+       — 5 to 7 bullets. This is a story the reader will act on or
                    repeat to someone. Give them the full strategic picture,
                    including what would have to be true for it to matter more or
                    less than it currently appears to.
  score 50-79     — 3 to 5 bullets.
  score below 50  — 2 bullets. What it signals, and what is missing. Then stop.

Each bullet is a complete sentence or two, no leading dash or bullet character —
the page adds those. Do not restate the summary. Cover, across the bullets:
SOURCING — there are two kinds of statement and they have different rules.
  - Claims about THIS event (what happened, who invested, what a filing says)
    must come from the provided text. Never carry a fact from one item into
    another item's fields, even inside the same cluster: each entry links to its
    own source and must stand on that source alone.
  - Background needed to understand the event (what a company does, a prior
    round, how an agency is structured, what a term means) may come from your
    own knowledge. Keep it to things you are confident about, phrase it as
    background rather than as reporting, and if you are unsure, leave it out or
    say plainly that it is unclear. Never dress up a guess as a detail.

MONEY — convert Chinese currency figures to approximate US dollars inline, using
{USD_CNY} yuan to the dollar, and keep the original: "数亿元" becomes "several
hundred million yuan (roughly $30-60M)". Round hard; these are orientation
figures, not valuations. Do the same for 万 and 亿 so the reader never has to
count zeros. Leave figures already in dollars alone.

Each input item carries `nature` and `origin`. They change how you must treat it.

nature:
- "reporting"  — a news outlet. Normal treatment.
- "forum"      — discussion board or comment thread (Hacker News, V2EX). This is
                 opinion and reaction, NOT verified fact. Never state a forum
                 claim as though it were established. Attribute it: "commenters
                 argue...", "the thread is skeptical that...". Its value is mood
                 and early signal, so say what the mood IS.
- "market"     — investor chatter (Xueqiu). Same rule as forum, and additionally
                 assume the poster may hold a position. Never repeat a price
                 target or valuation claim as fact.
- "aggregator" — a headline roundup (Techmeme). Treat as evidence of what US tech
                 media is covering, not as original reporting.

origin ("cn" or "us") is what the US press is seeing versus what the Chinese
press is seeing. Use it:
- If a story appears in BOTH cn and us items, cluster them together, and put the
  contrast in the SUMMARY, not just the detail. The summary is what a reader sees
  without clicking, so the Chinese take belongs there: "Chinese coverage stresses
  X while US discussion focuses on Y", "Chinese outlets lead with the regulatory
  order; US coverage frames it as a failed acquisition". Lead with that, then add
  the fact only if room remains — a reader who never opens the dropdown should
  still leave knowing how the two presses differ. Carry the fuller version into
  `detail` as well. Only if the two accounts genuinely do not differ should you
  say so, and then consider whether the item is worth keeping at all.
- If a story appears ONLY in us items, the reader has very likely already seen
  it. Score it low unless there is a China angle the US coverage is missing.
- If a story appears ONLY in cn items, that is the site's core value. Score it
  on its merits and do not penalise it for having no US coverage.

OMIT — this page is about China. Anything that is not, and adds nothing a reader
of Techmeme and Hacker News lacks, is taking a slot from something that is. Set
omit=true when:
  - china_focus is "none" and the item adds no distinctly Chinese perspective.
    This is the common case and you should apply it without hesitation: a
    Chinese outlet republishing foreign news, an English newsletter's community
    announcement, a US headline with no China dimension. Being carried by a
    China-focused publication does not earn a slot.
  - The story appears in BOTH cn and us items and the two accounts do not
    meaningfully differ. Same facts, same framing, nothing added — omit it.
  - The item is us-origin with no cn counterpart, UNLESS it carries something
    the reader would not get by reading those sites directly — for instance a
    comment thread showing how US practitioners are reacting to a Chinese
    release. A US headline restated is never worth a slot.

The one way a china_focus "none" story earns its place: the Chinese coverage of
it is genuinely different from the Western coverage — different emphasis, a
detail the Western version omits, a framing that tells you something about how
the story is being received or presented in China. When you keep a story on that
basis, the DIFFERENCE is the story. Say so in the very first clause of the
summary — "Chinese outlets frame X as...", "Chinese coverage omits..." — not
buried in the detail. If you cannot name a real difference, there isn't one:
omit it.
Never omit a cn-only story on these grounds; that is the whole point of the
page. When in doubt about a cn-only story, keep it and score it low.

A us-origin item that is clustered with a cn item is NEVER omitted. It is the
comparison — without it there is nothing to contrast against, the "also covered
by" line disappears, and the most valuable thing on the page is lost. Omit a
us-origin item only when it stands alone with no cn counterpart.

The two conditions above are the ONLY grounds for omitting. Do not omit because
an item seems minor, thin, or not worth your effort — score it low instead. A
low score is how you express "this barely matters"; omit means "the reader is
strictly better off not seeing this at all".

Separately: every item you do not omit must have a real title_en, a real summary,
and a detail list with AT LEAST TWO bullets. Never leave a field empty, never
return an empty detail list, and never return a placeholder as a way of
signalling that an item is low value. If an item does not deserve two bullets it
does not deserve a slot — set omit=true instead. A headline with no summary, or a
story that expands to nothing, is worse than either keeping the item properly or
dropping it.

Rules:
- section "lead" is reserved for the few genuinely most important stories:
  at most 5 across the whole input, and fewer is better. If you are unsure
  whether something is a lead, it is not.
- section "policy" is for government, regulation, law, censorship and
  state-industry relations. "culture" is for society, labour, demographics,
  online controversy, media and consumer life.
- section "signal" is for items whose value is a hard number or dataset.
- Telecom is NOT a beat here. Carrier, spectrum, RF and network-operator news is
  out of scope. A telecom story belongs on the page only when it is really a
  technology story — a chip, a model, a platform shift — and then it is filed
  under whatever section fits that angle, not treated as telecom.
- Summarize in your own words. Never reproduce source sentences.
- Headlines in sentence case, not Title Case: capitalise the first word and
  proper nouns only, consistently across every item in the batch.

Return a JSON object with a single key "items", whose value is the array of
results — one entry per input item. No prose, no markdown fences."""


def _payload(items) -> str:
    rows = []
    for n, it in enumerate(items, 1):
        rows.append(
            {
                "id": n,
                "title": it.title,
                "source": it.source_name,
                "lang": it.lang,
                "tier": it.tier,
                "nature": it.nature,
                "origin": it.origin,
                "published": it.published,
                "text": it.body[:2400],
            }
        )
    return json.dumps(rows, ensure_ascii=False)


def _request(system: str, user: str, effort: str, schema: dict,
             max_tokens: int | None = None) -> tuple[list[dict] | None, str | None]:
    """One OpenRouter call. Returns (rows, error). Never raises."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None, "OPENROUTER_API_KEY unset"
    try:
        import requests

        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "content-type": "application/json",
                # OpenRouter attribution headers; optional but keeps the app
                # identifiable in the dashboard's per-app usage breakdown.
                "HTTP-Referer": "https://github.com/chinadesk",
                "X-Title": "China Desk",
            },
            json={
                "model": MODEL,
                "reasoning": {"effort": effort},
                "max_tokens": max_tokens or MAX_TOKENS,
                "response_format": schema,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # OpenRouter returns upstream errors as HTTP 200 with an `error` key.
        if "error" in data:
            raise RuntimeError(f"upstream error: {data['error']}")

        choice = data["choices"][0]
        finish = choice.get("finish_reason")
        if finish == "length":
            raise RuntimeError(
                f"response truncated at max_tokens={max_tokens or MAX_TOKENS}; raise it "
                f"or lower CHINADESK_EFFORT (reasoning counts against this budget)"
            )

        usage = data.get("usage") or {}
        reasoning_toks = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        )
        print(
            f"[rank] {MODEL} effort={effort} finish={finish} "
            f"in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
            f"reasoning={reasoning_toks}",
            file=sys.stderr,
        )

        text = (choice["message"].get("content") or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        parsed = json.loads(text)
        # json_schema mode gives {"items": [...]}; tolerate a bare array too, in
        # case a provider silently ignores response_format.
        rows = parsed.get("items") if isinstance(parsed, dict) else parsed
        if not isinstance(rows, list):
            raise RuntimeError(f"expected a list of items, got {type(rows).__name__}")
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def _system(fx: dict | None) -> str:
    rate = (fx or {}).get("usd_cny", 7.1)
    return SYSTEM.replace("{USD_CNY}", str(rate))


def _call_model(items, fx: dict | None = None) -> tuple[list[dict] | None, bool]:
    """Returns (rows, degraded).

    `degraded` distinguishes the two cases that used to look identical: no key
    configured (a legitimate offline smoke test) versus a key that was there and
    the call failed (a production incident that must not reach the site).
    """
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("[rank] OPENROUTER_API_KEY unset — heuristic fallback (dev only)",
              file=sys.stderr)
        return None, False
    rows, err = _request(_system(fx), _payload(items), EFFORT, RESPONSE_FORMAT)
    if rows is None:
        print(f"[rank] MODEL CALL FAILED: {err}", file=sys.stderr)
        return None, True
    return rows, False


CJK = re.compile(r"[\u4e00-\u9fff]")

REPAIR_SYSTEM = """You translate leftover Chinese into English. You are given JSON
objects that were supposed to be English but still contain Chinese characters.

Return a JSON object with key "items": the same objects, same uids, with every
Chinese fragment rendered as natural English. Translate, do not transliterate,
and do not add any fact that is not already present. Change nothing that is
already English. No prose, no markdown fences."""

_REPAIR_FIELDS = ("title_en", "summary", "detail")


def _complete(items, by_uid: dict, fx: dict | None) -> int:
    """Re-request any item the model left out of its response array.

    Over ~100 items the model reliably returns slightly fewer rows than it was
    given — 105 for 106 in one observed run, more under load. It is not
    truncation (finish_reason is "stop"); rows are simply missing. The old
    missing-uid fallback set title/section/score but never summary or detail, so
    each dropped item became a blank-summary entry with an untranslated title —
    which is what kept blocking preflight. Ask again for just the stragglers.
    """
    missing = [it for it in items if it.uid not in by_uid]
    if not missing:
        return 0
    print(f"[rank] completion pass: model dropped {len(missing)} item(s) from its "
          f"response array", file=sys.stderr)
    rows, err = _request(_system(fx), _payload(missing), EFFORT, RESPONSE_FORMAT)
    if rows is None:
        print(f"[rank] completion pass failed: {err}", file=sys.stderr)
        return len(missing)
    recovered = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(missing) and missing[n - 1].uid not in by_uid:
            by_uid[missing[n - 1].uid] = r
            recovered += 1
    still = len(missing) - recovered
    print(f"[rank] completion pass: recovered {recovered}, {still} still missing",
          file=sys.stderr)
    return still


def _repair(items) -> int:
    """Second pass over anything that came back with Chinese still in it.

    A ~2% leak rate on product-name-dense headlines is stochastic, not a prompt
    bug, so retrying beats tuning. Pure translation needs no reasoning budget,
    hence effort=low. Returns the number of items still contaminated afterwards;
    the CJK guard in rank() remains the terminal backstop either way.
    """
    def _txt(o, f):
        v = getattr(o, f, "")
        return "\n".join(v) if isinstance(v, list) else (v or "")

    bad = [it for it in items
           if any(CJK.search(_txt(it, f)) for f in _REPAIR_FIELDS)]
    if not bad:
        return 0
    print(f"[rank] repair pass: {len(bad)} item(s) still contain Chinese", file=sys.stderr)

    def _flat(v):
        return "\n".join(v) if isinstance(v, list) else (v or "")

    payload = json.dumps(
        [{"id": n, **{f: _flat(getattr(i, f)) for f in _REPAIR_FIELDS}}
         for n, i in enumerate(bad, 1)],
        ensure_ascii=False,
    )
    rows, _ = _request(REPAIR_SYSTEM, payload, effort="low", schema=_repair_schema())
    if rows:
        by_pos = {}
        for r in rows:
            if isinstance(r, dict):
                try:
                    by_pos[int(r.get("id"))] = r
                except (TypeError, ValueError):
                    pass
        for n, it in enumerate(bad, 1):
            r = by_pos.get(n)
            if not r:
                continue
            for f in _REPAIR_FIELDS:
                val = (r.get(f) or "").strip()
                if val and not CJK.search(val):
                    setattr(it, f, val.split("\n") if f == "detail" else val)

    still = sum(1 for it in bad
                if any(CJK.search(_txt(it, f)) for f in _REPAIR_FIELDS))
    print(f"[rank] repair pass: {len(bad) - still} fixed, {still} still bad", file=sys.stderr)
    return still


def _repair_schema() -> dict:
    props = {"id": {"type": "integer"}, **{f: {"type": "string"} for f in _REPAIR_FIELDS}}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "repaired_items", "strict": True,
            "schema": {
                "type": "object",
                "properties": {"items": {
                    "type": "array",
                    "items": {"type": "object", "properties": props,
                              "required": list(props), "additionalProperties": False},
                }},
                "required": ["items"], "additionalProperties": False,
            },
        },
    }

AI_HINTS = ("ai", "model", "gpu", "chip", "llm", "agent", "robot", "compute",
            "openai", "anthropic", "deepseek", "qwen", "kimi", "nvidia")


def _heuristic(items) -> list[dict]:
    out = []
    for it in items:
        blob = f"{it.title} {it.body[:400]}".lower()
        if it.tier == "analysis":
            section = "analysis"
        elif it.tier == "ai" or any(h in blob for h in AI_HINTS):
            section = "ai"
        else:
            section = "wire"
        out.append(
            {
                "id": n,
                "title_en": it.title,
                # Deliberately empty. Copying body[:180] here reproduced source
                # text verbatim (hard constraint #2) and put the source language
                # on the page. There is no way to write a fresh summary without
                # a model, so the honest fallback is to write nothing.
                "summary": "",
                "detail": [],
                "section": section,
                "score": round(50 * it.weight, 1),
                "cluster": it.uid,
            }
        )
    return out


def rank(items, fx: dict | None = None) -> dict:
    """Annotate items in place. Returns a status dict for the preflight checks."""
    status = {"degraded": False, "ranked": False, "untranslated": 0, "omitted": []}
    if not items:
        return status

    rows, degraded = _call_model(items, fx)
    status["degraded"] = degraded
    status["ranked"] = rows is not None
    result = rows if rows is not None else _heuristic(items)
    # Map by position. Out-of-range or non-integer ids are discarded rather
    # than silently attached to the wrong story — a mislabeled row would put one
    # article's analysis under another's headline, which is worse than a gap.
    by_uid = {}
    for r in result:
        if not isinstance(r, dict):
            continue
        try:
            n = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(items):
            by_uid[items[n - 1].uid] = r

    if status["ranked"]:
        _complete(items, by_uid, fx)

    for it in items:
        r = by_uid.get(it.uid)
        if not r:
            # Still missing after the completion pass. Drop it rather than ship a
            # headline with no summary: preflight would block the whole build for
            # one straggler, and a bare untranslated title helps nobody.
            it.title_en = it.title
            it.section = "wire"
            it.score = 10.0
            it.cluster_id = it.uid
            it.omit = bool(status["ranked"])
            it.omit_reason = "no ranking returned for this item"
            continue
        it.title_en = (r.get("title_en") or it.title).strip()
        it.summary = (r.get("summary") or "").strip()
        d = r.get("detail") or []
        it.detail = [b.strip() for b in d if isinstance(b, str) and b.strip()] \
            if isinstance(d, list) else [d.strip()] if str(d).strip() else []
        it.section = r.get("section", "wire")
        it.cluster_id = r.get("cluster") or it.uid
        it.china_focus = r.get("china_focus") or "core"
        # Only honoured on the real path — in fallback mode nothing has been
        # judged, so dropping items would just hide the failure.
        it.omit = bool(r.get("omit")) if status["ranked"] else False
        it.omit_reason = (r.get("omit_reason") or "").strip()
        try:
            it.score = float(r.get("score", 0)) * it.weight
        except (TypeError, ValueError):
            it.score = 10.0 * it.weight

    # Targeted retry for the handful of items that come back with Chinese still
    # in them, before the guard fires. Skipped in fallback mode, where every
    # field is untranslated by construction and a repair call is pointless.
    if status["ranked"]:
        _repair(items)

    # Record what was dropped and why. Silent removal is exactly the kind of
    # invisible editing hard constraint #3 exists to prevent, so this goes into
    # the ledger rather than just vanishing.
    status["omitted"] = [
        {"source": it.source_name, "title": it.title_en, "reason": it.omit_reason}
        for it in items if getattr(it, "omit", False)
    ]

    # Belt and braces: the site must be English-only. Anything that survived the
    # repair pass gets marked; the preflight checks decide whether that blocks
    # publication. Never remove this guard.
    for it in items:
        if CJK.search(it.title_en or ""):
            it.title_en = f"[UNTRANSLATED] {it.title_en}"
            status["untranslated"] += 1

    return status


def build_clusters(items) -> list[dict]:
    """Group by cluster_id; lead item is the highest-scoring member."""
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it.cluster_id or it.uid, []).append(it)

    clusters = []
    for cid, members in groups.items():
        members.sort(key=lambda i: i.score, reverse=True)
        lead, rest = members[0], members[1:]
        clusters.append(
            {
                "id": cid,
                "lead": lead,
                "related": rest,
                "section": lead.section,
                "score": lead.score + 2.0 * len(rest),  # corroboration bonus
                "size": len(members),
            }
        )
    clusters.sort(key=lambda c: c["score"], reverse=True)
    return clusters
