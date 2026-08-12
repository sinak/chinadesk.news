#!/usr/bin/env python3
"""Run the daily brief once, on demand, without building the site.

The brief graduated to `src/brief.py` when it went into production. This is now
a thin CLI over that module rather than a second copy of it: the prompt is ~200
lines of carefully tuned editorial instruction, and two copies of it would drift
within a week — the experiment would be tuned, the site would not, and nobody
would notice until the published brief stopped matching the one being tested.

Use it to iterate on the prompt or compare models without paying for a full
build or touching what is published:

    OPENROUTER_API_KEY=... python -m experiments.playbook
    BRIEF_MODEL=openai/gpt-5.6-luna BRIEF_EFFORT=max python -m experiments.playbook

Output goes to experiments/playbooks/<date>.md, which is scratch space — the
real brief is published to dist/brief/<date>.md by the build.
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
from src import brief, fetch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=26)
    ap.add_argument("--days", type=int, default=3, help="prior briefs to carry")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("[playbook] OPENROUTER_API_KEY unset", file=sys.stderr)
        return 1

    cfg = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text())
    items, ledger = fetch.fetch_all(cfg, window_hours=args.window)
    live = sum(1 for f in ledger if f["ok"] and f["kept"])
    print(f"[playbook] {len(items)} items from {live}/{len(ledger)} live feeds",
          file=sys.stderr)
    if not items:
        print("[playbook] no items", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    prior = brief.prior_briefs(args.days)
    if prior:
        print(f"[playbook] carrying {len(prior)} prior brief(s): "
              f"{', '.join(d for d, _ in prior)}", file=sys.stderr)

    system = (brief.SYSTEM
              .replace("{USD_CNY}", str((cfg.get("fx") or {}).get("usd_cny", 7.1)))
              .replace("{TODAY}", now.strftime("%A, %d %B %Y"))
              .replace("{THREADS}", brief.THREADS if prior else ""))
    user = brief.payload(items, prior)
    print(f"[playbook] prompt ~{len(user)//1000}k chars, "
          f"model={brief.MODEL} effort={brief.EFFORT}", file=sys.stderr)

    text, err = brief.call(system, user)
    if text is None:
        print(f"[playbook] FAILED: {err}", file=sys.stderr)
        return 1

    brief.OUT.mkdir(parents=True, exist_ok=True)
    path = brief.OUT / f"{now.strftime('%Y-%m-%d')}.md"
    path.write_text(text, encoding="utf-8")
    print(f"[playbook] wrote {path} — {len(text)} chars, ~{len(text.split())} words")

    # Same gate the build applies, so a prompt change that would be rejected in
    # production is visible here rather than at deploy time.
    from src import checks
    for r in checks.check_brief(text):
        mark = "ok  " if r.ok else ("FAIL" if r.blocking else "warn")
        print(f"  {mark} {r.name:<20} {r.detail}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
