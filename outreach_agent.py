#!/usr/bin/env python3
"""
outreach_agent.py — OutreachFlow-Engine, wired to the tools that actually exist.

Implements the cascade in agent_prompt.md, with the substitutions documented in that
file's Reconciliation section:

  spec said                      this does
  ---------------------------    ----------------------------------------------------
  StealthScrape                  scrape_contact.py (StealthScrape cannot extract emails)
  Hunter Sequences API           writes outreach.csv for send_campaign.py (no mailbox
                                 is connected to the Hunter account)
  Hunter-first cascade           scrape-first by default, see below
  50 emails/day                  25, matching send_campaign.py's DAILY_CAP

WHY SCRAPE-FIRST
  The free plan carries ~2x as many verifications as domain-searches (100 vs 50). The
  contact-page scrape is free and hits ~50% on small companies. So scrape -> verify
  spends the plentiful credit and saves the scarce one; Hunter domain-search is the
  fallback, not the opener. Pass --strategy hunter-first for the literal spec order.

  Hunter's verifier runs a real SMTP check server-side, which is the probe this machine
  cannot do (home ISPs block port 25). A 'valid' verdict here is genuinely verified,
  not a guess.

  python outreach_agent.py --limit 25                 # dry run, spends nothing
  python outreach_agent.py --limit 25 --run           # executes the cascade
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from find_emails import size_tier, _clean
from scrape_contact import scrape_emails

HUNTER_KEY = os.getenv("HUNTER_API_KEY")
DB = "outreach.sqlite"
DAILY_SEND_CAP = 25          # must match send_campaign.DAILY_CAP
RETRY_PAUSE = 60             # spec: pause 60s on 429, then one retry
PROSE_SCAN_MULTIPLIER = 4    # rows to scan per --limit when filtering to written hooks


# ---------- credit budget ----------
class Budget:
    """Local mirror of the Hunter quota. Refuses to overspend rather than discovering
    the ceiling by collecting 403s."""

    def __init__(self, searches=0, verifications=0):
        self.searches = searches
        self.verifications = verifications
        self.spent_searches = 0
        self.spent_verifications = 0

    @classmethod
    def from_account(cls):
        if not HUNTER_KEY:
            return cls(0, 0)
        try:
            r = requests.get("https://api.hunter.io/v2/account",
                             params={"api_key": HUNTER_KEY}, timeout=20)
            d = r.json()["data"]["requests"]
            return cls(d["searches"]["remaining"], d["verifications"]["remaining"])
        except Exception as e:
            print(f"!! could not read hunter quota ({type(e).__name__}); "
                  f"assuming zero and running scrape-only", file=sys.stderr)
            return cls(0, 0)

    def can_search(self):
        return self.searches - self.spent_searches > 0

    def can_verify(self):
        return self.verifications - self.spent_verifications > 0

    def __str__(self):
        return (f"searches {self.searches - self.spent_searches}/{self.searches}, "
                f"verifications {self.verifications - self.spent_verifications}/"
                f"{self.verifications}")


def _get(url, params, budget_attr, budget):
    """One GET with the spec's 429 discipline: pause 60s, retry once, then give up.

    Returns (data, charged). `charged` is False only when the request never reached
    Hunter — a connection or DNS failure consumes no credit server-side, so the
    caller must not spend one locally. A read timeout may well have been processed,
    so it counts as charged: over-counting the local mirror wastes at most one
    credit, under-counting walks into the 403 the mirror exists to avoid.
    """
    for attempt in (1, 2):
        try:
            r = requests.get(url, params=params, timeout=25)
        except requests.exceptions.ConnectionError:
            return None, False        # never opened a socket; nothing was spent
        except Exception:
            return None, True
        if r.status_code == 429 and attempt == 1:
            print(f"   429 — pausing {RETRY_PAUSE}s", file=sys.stderr)
            time.sleep(RETRY_PAUSE)
            continue
        if r.status_code in (401, 403, 451):
            # Quota or key problem: burn the local budget so we stop trying.
            setattr(budget, budget_attr, 0)
            return None, True
        if r.status_code != 200:
            return None, True
        try:
            return r.json().get("data"), True
        except Exception:
            return None, True
    return None, True


# ---------- cascade steps ----------
def hunter_search(domain, budget):
    if not HUNTER_KEY or not budget.can_search():
        return None
    data, charged = _get("https://api.hunter.io/v2/domain-search",
                         {"domain": domain, "api_key": HUNTER_KEY, "limit": 10},
                         "searches", budget)
    if charged:
        budget.spent_searches += 1
    if not data:
        return None
    emails = data.get("emails") or []

    def rank(e):
        pos = (e.get("position") or "").lower()
        senior = any(k in pos for k in ("ceo", "founder", "cto", "head", "vp", "director"))
        return (senior, e.get("confidence") or 0)

    emails.sort(key=rank, reverse=True)
    if emails:
        return {"email": emails[0].get("value"), "source": "hunter_io",
                "position": emails[0].get("position")}
    return None


def hunter_verify(email, budget):
    """-> (status, score). status is the spec's vocabulary: valid|risky|invalid|null."""
    if not HUNTER_KEY or not budget.can_verify() or not email:
        return None, None
    data, charged = _get("https://api.hunter.io/v2/email-verifier",
                         {"email": email, "api_key": HUNTER_KEY}, "verifications", budget)
    if charged:
        budget.spent_verifications += 1
    if not data:
        return None, None
    raw = (data.get("status") or "").lower()
    score = data.get("score")
    # Hunter's vocabulary is wider than the spec's; collapse it.
    if raw == "valid":
        return "valid", score
    if raw in ("invalid", "disposable"):
        return "invalid", score
    # accept_all, webmail, unknown — and any status Hunter adds later — collapse
    # to "risky". The spec's vocabulary is narrower than Hunter's on purpose.
    return "risky", score


def process(row, budget, strategy, scrape_delay, prose_ok):
    """Run the cascade for one company. Returns the spec's JSON log dict plus extras."""
    company = row.get("company")
    domain = _clean(row.get("domain"))
    ceo = _clean(row.get("ceo"))
    first, last = _clean(row.get("ceo_first")), _clean(row.get("ceo_last"))

    log = {
        "company_processed": company,
        "strategy_used": "skipped",
        "email_found": None,
        "verification_status": None,
        "action_executed": "logged_error",
        "internal_thought": "",
        # extras the middleware does not need but the CSV does
        "_domain": domain, "_ceo": ceo, "_source": None, "_score": None,
        "_tier": row.get("size_tier"), "_ts": datetime.now(timezone.utc).isoformat(),
    }

    if not domain or not ceo:
        log["internal_thought"] = "SKIPPED_INSUFFICIENT_DATA: missing domain or CEO name."
        return log
    if not prose_ok:
        log["internal_thought"] = ("SKIPPED_NO_PROSE: custom.csv has no written hook for "
                                   "this company; sending boilerplate would waste the lead.")
        return log

    hit = None
    if strategy == "hunter-first":
        hit = hunter_search(domain, budget)
        if hit:
            log["strategy_used"] = "hunter_io"
        if not hit:
            found = scrape_emails(domain, first, last, delay=scrape_delay)
            if found:
                hit = {"email": found[0][0], "source": "contact_page"}
                log["strategy_used"] = "stealth_scrape"
    else:                                   # scrape-first (default)
        found = scrape_emails(domain, first, last, delay=scrape_delay)
        if found:
            hit = {"email": found[0][0], "source": "contact_page"}
            log["strategy_used"] = "stealth_scrape"
        if not hit:
            hit = hunter_search(domain, budget)
            if hit:
                log["strategy_used"] = "hunter_io"

    if not hit:
        log["internal_thought"] = "No address from contact page or Hunter; nothing to verify."
        return log

    log["email_found"] = hit["email"]
    log["_source"] = hit["source"]

    status, score = hunter_verify(hit["email"], budget)
    log["verification_status"] = status
    log["_score"] = score

    if status == "valid":
        log["action_executed"] = "sequence_queued"
        log["internal_thought"] = (f"Verified via {hit['source']} and queued for send "
                                   f"(score {score}).")
    elif status is None:
        log["internal_thought"] = ("Address found but verification budget is exhausted; "
                                   "holding as unverified.")
    else:
        log["internal_thought"] = (f"Address found but verifier returned {status}; "
                                   f"not queueing.")
    return log


# ---------- resume cache ----------
def db_init():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS seen (domain TEXT PRIMARY KEY, payload TEXT)")
    con.commit()
    return con


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--infile", default="startups_clean.csv")
    ap.add_argument("--custom", default="custom.csv")
    ap.add_argument("--out", default="outreach.csv")
    ap.add_argument("--limit", type=int, default=DAILY_SEND_CAP)
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--size", default=None, help="restrict to one or more size tiers")
    ap.add_argument("--strategy", choices=("scrape-first", "hunter-first"),
                    default="scrape-first")
    ap.add_argument("--scrape-delay", type=float, default=1.5)
    ap.add_argument("--any-prose", action="store_true",
                    help="process companies with no written hook (they will be skipped "
                         "at send time by the placeholder guard anyway)")
    ap.add_argument("--research", default="research.csv", metavar="CSV",
                    help="research_jobs.py output; skips companies whose domain no "
                         "longer serves them. Pass '' to disable.")
    ap.add_argument("--skip-sent", default="campaign.sqlite", metavar="DB",
                    help="skip companies already emailed. Pass '' to disable.")
    ap.add_argument("--run", action="store_true",
                    help="actually call the APIs and scrape. Default is a dry plan.")
    args = ap.parse_args()

    df = pd.read_csv(args.infile)
    df = df[df["score"] >= args.min_score].copy()
    tiers = df["size"].map(size_tier)
    df["size_rank"] = [t[0] for t in tiers]
    df["size_tier"] = [t[1] for t in tiers]
    if args.size:
        df = df[df["size_tier"].isin({s.strip() for s in args.size.split(",")})]
    df = df.sort_values(["size_rank", "score"], ascending=[True, False])

    # Liveness gate, applied *here* rather than only at send time: this is the point
    # where money is spent. 18 of the 64 rows in the 1-10 tier are acquired, rebranded
    # or dead, and a domain-search against one of those is a credit bought for nothing.
    if args.research and os.path.exists(args.research):
        res = pd.read_csv(args.research)
        dead = set(res.loc[res.site_status.isin(
            ["redirected_offsite", "unreachable", "dead", "parked"]), "domain"])
        before = len(df)
        df = df[~df["domain"].isin(dead)]
        print(f"{args.research}: skipping {before - len(df)} dead/moved-on companies",
              file=sys.stderr)

    # Don't re-resolve someone who has already had the email. The send path dedupes
    # too, but re-resolving spends a Hunter credit to learn nothing.
    if args.skip_sent and os.path.exists(args.skip_sent):
        con_s = sqlite3.connect(args.skip_sent)
        sent = {e for (e,) in con_s.execute("SELECT email FROM sent")}
        con_s.close()
        if sent and os.path.exists(args.out):
            prev = pd.read_csv(args.out)
            done = set(prev.loc[prev["email"].isin(sent), "domain"])
            before = len(df)
            df = df[~df["domain"].isin(done)]
            print(f"{args.skip_sent}: skipping {before - len(df)} already emailed",
                  file=sys.stderr)

    # Which companies have hand-written prose? Those are the only ones worth an address.
    prose = set()
    if os.path.exists(args.custom):
        c = pd.read_csv(args.custom)
        need = [col for col in ("hook", "why") if col in c.columns]
        if need:
            written = c.dropna(subset=need)
            written = written[(written[need] != "").all(axis=1)]
            prose = set(written["domain"])

    # Prose coverage is patchy, so when we are filtering to companies that have a
    # written hook, scan a wider slice than --limit and cut back after the filter.
    scan = args.limit if args.any_prose else args.limit * PROSE_SCAN_MULTIPLIER
    df = df.head(scan)
    rows = [r for r in df.to_dict("records")
            if args.any_prose or r["domain"] in prose][:args.limit]

    budget = Budget.from_account() if args.run else Budget(0, 0)
    print(f"queue: {len(rows)} companies | strategy: {args.strategy} | "
          f"{'RUNNING' if args.run else 'DRY PLAN'}", file=sys.stderr)
    if args.run:
        print(f"hunter budget: {budget}", file=sys.stderr)
    print(f"prose written for {len(prose)} domains; "
          f"{'including' if args.any_prose else 'skipping'} companies without it\n",
          file=sys.stderr)

    if not args.run:
        for i, r in enumerate(rows, 1):
            print(f"{i:>3}. {r['company'][:30]:<32} {r['domain'][:28]:<30} "
                  f"{r['size_tier']:<9} score={r['score']}", file=sys.stderr)
        print(f"\nwould scrape up to {len(rows)} sites and spend at most {len(rows)} "
              f"verifications\n(+ domain-searches only where the scrape finds nothing).\n"
              f"re-run with --run to execute.", file=sys.stderr)
        return

    con = db_init()
    results = []
    for i, r in enumerate(rows, 1):
        cached = con.execute("SELECT payload FROM seen WHERE domain=?",
                             (r["domain"],)).fetchone()
        if cached:
            log = json.loads(cached[0])
        else:
            log = process(r, budget, args.strategy, args.scrape_delay,
                          args.any_prose or r["domain"] in prose)
            con.execute("INSERT OR REPLACE INTO seen VALUES (?,?)",
                        (r["domain"], json.dumps(log)))
            con.commit()
        results.append(log)

        # The spec's required per-turn output.
        print(json.dumps({k: v for k, v in log.items() if not k.startswith("_")},
                         indent=2))
        print(f"   [{i}/{len(rows)}] {budget}", file=sys.stderr)

    out = pd.DataFrame(results).rename(columns={
        "company_processed": "company", "email_found": "email",
        "_domain": "domain", "_ceo": "ceo", "_source": "source",
        "_score": "verify_score", "_tier": "size_tier", "_ts": "checked_at",
    })
    # Accumulate into --out rather than overwriting it. The --skip-sent gate reads its
    # "already emailed" list back out of this same file, so a wholesale overwrite threw
    # away the memory that gate depends on: every company resolved in an earlier run
    # silently became eligible again, and the next run paid Hunter to re-resolve people
    # who had already been mailed. Newest row per domain wins.
    if os.path.exists(args.out):
        prev = pd.read_csv(args.out)
        keep = prev[~prev["domain"].isin(set(out["domain"]))] if "domain" in prev else prev
        merged = pd.concat([keep, out], ignore_index=True)
        print(f"{args.out}: {len(keep)} earlier rows kept, {len(out)} from this run",
              file=sys.stderr)
    else:
        merged = out
    merged.to_csv(args.out, index=False, encoding="utf-8")

    counts = {}
    for r in results:
        key = r["verification_status"] or r["strategy_used"]
        counts[key] = counts.get(key, 0) + 1
    valid = sum(1 for r in results if r["verification_status"] == "valid")
    print(f"\n{json.dumps(counts, indent=2)}", file=sys.stderr)
    print(f"\n{valid} verified and queueable -> {args.out}", file=sys.stderr)
    print(f"hunter budget left: {budget}", file=sys.stderr)
    print(f"\nnext:\n  python send_campaign.py {args.out} --template template.txt "
          f"--custom custom.csv        # dry run", file=sys.stderr)


if __name__ == "__main__":
    main()
