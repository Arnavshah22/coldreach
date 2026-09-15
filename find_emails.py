#!/usr/bin/env python3
"""
find_emails.py — resolve a working email per company.

Order of attack per domain:
  1. Hunter.io domain-search (if HUNTER_API_KEY set) -> real email + pattern
  2. Pattern permutations from CEO name (already in startups_clean.csv)
  3. MX lookup, then SMTP RCPT TO probe with catch-all detection

Run this LOCALLY, not in a sandbox. Two things matter:
  - Outbound port 25 must be open. Most home ISPs and AWS/GCP block it.
    If blocked, every probe returns "unknown" and you should either use a
    cheap VPS that allows 25 (Hetzner, OVH) or skip step 3 and rely on Hunter.
  - Probe from an IP you don't care about. Some mail servers greylist or
    blacklist probing IPs. Never probe from the IP you'll send from.

  pip install pandas dnspython requests
  export HUNTER_API_KEY=...        # optional
  python find_emails.py startups_clean.csv --limit 100 --out verified.csv

If port 25 is blocked, step 3 can never return anything but "probe_blocked". In that
case skip it and let Hunter be the only source:

  python find_emails.py startups_clean.csv --hunter-only --size 1-10 --limit 45 \
      --out verified.csv

--hunter-only never produces a "verified" row, because nothing is verified without an
SMTP handshake. Its best output is "found" (a real address Hunter had on file); pattern
guesses come back as "hunter_guess", which send_campaign.py will not mail.
"""
import argparse
import csv
import json
import os
import random
import re
import smtplib
import socket
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import dns.resolver
import pandas as pd
import requests

from scrape_contact import scrape_emails

HUNTER_KEY = os.getenv("HUNTER_API_KEY")
PROBE_FROM = os.getenv("PROBE_FROM", "checker@example.com")  # envelope sender
SOCKET_TIMEOUT = 10
DB = "findemails.sqlite"


# ---------- resume cache ----------
def db_init():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS result (domain TEXT PRIMARY KEY, payload TEXT)")
    con.commit()
    return con


def db_get(con, domain):
    row = con.execute("SELECT payload FROM result WHERE domain=?", (domain,)).fetchone()
    return json.loads(row[0]) if row else None


def db_put(con, domain, payload):
    con.execute("INSERT OR REPLACE INTO result VALUES (?,?)", (domain, json.dumps(payload)))
    con.commit()


# ---------- step 1: hunter ----------
# Set when Hunter says the monthly credit pool is gone. Workers check it and bail
# instead of firing another 40 requests that can only fail.
CREDITS_EXHAUSTED = threading.Event()


def hunter_lookup(domain):
    if not HUNTER_KEY or CREDITS_EXHAUSTED.is_set():
        return None
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": HUNTER_KEY, "limit": 10},
            timeout=20,
        )
        # 401 bad key, 403/451 plan or usage limit, 429 rate limit. 429 is the only
        # transient one, but we do not retry it inline — that stalls a worker on one
        # domain. Back off briefly and let this domain fall through to the guess or
        # scrape path; the rest mean every subsequent call fails the same way.
        if r.status_code == 429:
            time.sleep(5)
            return None
        if r.status_code in (401, 403, 451):
            detail = ""
            try:
                errs = r.json().get("errors") or []
                detail = errs[0].get("details", "") if errs else ""
            except Exception:
                pass
            if not CREDITS_EXHAUSTED.is_set():
                CREDITS_EXHAUSTED.set()
                print(f"\n!! hunter returned {r.status_code}: {detail}\n"
                      f"!! stopping hunter lookups; remaining domains fall through\n",
                      file=sys.stderr)
            return None
        data = r.json().get("data", {})
    except Exception:
        return None

    emails = data.get("emails") or []
    # prefer decision makers, then anyone with high confidence
    def rank(e):
        pos = (e.get("position") or "").lower()
        senior = any(k in pos for k in ("ceo", "founder", "cto", "head", "vp", "director", "lead"))
        eng = any(k in pos for k in ("engineer", "technical", "technology"))
        return (senior * 2 + eng, e.get("confidence") or 0)

    emails.sort(key=rank, reverse=True)
    if emails:
        top = emails[0]
        return {
            "email": top.get("value"),
            "source": "hunter",
            "confidence": top.get("confidence"),
            "position": top.get("position"),
            "pattern": data.get("pattern"),
        }
    if data.get("pattern"):
        return {"email": None, "source": "hunter_pattern", "pattern": data["pattern"]}
    return None


# ---------- step 2 + 3: mx + smtp probe ----------
def mx_hosts(domain):
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return [str(r.exchange).rstrip(".") for r in sorted(answers, key=lambda r: r.preference)]
    except Exception:
        return []


def smtp_probe(host, domain, addresses):
    """Return dict addr -> True/False, plus catch_all flag. One connection, many RCPTs."""
    out = {}
    catch_all = False
    try:
        server = smtplib.SMTP(timeout=SOCKET_TIMEOUT)
        server.connect(host, 25)
        server.helo(socket.getfqdn())
        server.mail(PROBE_FROM)

        # catch-all test with a random local part
        rand = f"zz{random.randint(10**9, 10**10)}@{domain}"
        code, _ = server.rcpt(rand)
        catch_all = code in (250, 251)

        if not catch_all:
            for addr in addresses:
                code, _ = server.rcpt(addr)
                out[addr] = code in (250, 251)
                time.sleep(0.4)  # be polite, avoid rate-limit disconnects
        server.quit()
    except Exception:
        return {}, None
    return out, catch_all


# ---------- company size tiers ----------
# The source column is inconsistent: '1-10', '2-10', '11-50 ' with a trailing space,
# '1,001-5,000' and '1001-5000' side by side, '10,001+', a bare '0', and blanks.
# Parse the bracket rather than string-matching it, and tier on the upper bound so
# '10-50' lands in small rather than micro.
TIERS = [(10, "1-10"), (50, "11-50"), (200, "51-200"), (500, "201-500"),
         (1000, "501-1000"), (5000, "1001-5000"), (float("inf"), "5000+")]
UNKNOWN_TIER = "unknown"


def size_bounds(raw):
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if not s or s.lower() in ("nan", "none"):
        return None
    m = re.fullmatch(r"(\d+)[-–—](\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"(\d+)\+", s)
    if m:
        return int(m.group(1)), 10 ** 9
    m = re.fullmatch(r"(\d+)", s)
    if m:
        return int(m.group(1)), int(m.group(1))
    return None


def size_tier(raw):
    """-> (rank, label). Unknown sizes rank last so they never displace a real lead."""
    bounds = size_bounds(raw)
    if bounds is None:
        return len(TIERS), UNKNOWN_TIER
    upper = bounds[1]
    for i, (ceiling, label) in enumerate(TIERS):
        if upper <= ceiling:
            return i, label
    return len(TIERS), UNKNOWN_TIER


def _clean(value):
    """pandas gives NaN for blank cells, and NaN is truthy — so `x or ''` does not
    protect you. Everything read off a row goes through here first."""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "") else text


def try_scrape(result, domain, ceo_first, ceo_last, delay):
    """Hunter had nothing. Read the company's own contact page instead.
    Returns True if it filled in an address."""
    try:
        hits = scrape_emails(domain, ceo_first, ceo_last, delay=delay)
    except Exception as e:
        result["notes"] = f"scrape failed: {type(e).__name__}"
        return False
    if not hits:
        return False
    addr, score, src = hits[0]
    result.update(
        email=addr,
        status="scraped",
        source="contact_page",
        notes=f"published on {src} (rank {score}"
              + (f", {len(hits)} found)" if len(hits) > 1 else ")"),
    )
    return True


def resolve_one(row, hunter_only=False, min_confidence=0, scrape=False, scrape_delay=1.5):
    domain = row["domain"]
    candidates = [c for c in _clean(row.get("email_candidates")).split("|") if c and "@" in c]

    result = {
        "company": row["company"],
        "domain": domain,
        "ceo": row.get("ceo"),
        "size": row.get("size"),
        "size_tier": row.get("size_tier"),
        "score": row.get("score"),
        "email": None,
        "status": "not_found",
        "source": None,
        "notes": "",
    }

    hit = hunter_lookup(domain)
    if hit and hit.get("email"):
        conf = hit.get("confidence") or 0
        # A low-confidence hunter hit is still a guess. Park it in its own status so
        # send_campaign.py's SEND_STATES filter won't mail it.
        status = "found" if conf >= min_confidence else "hunter_low_conf"
        result.update(email=hit["email"], status=status, source="hunter",
                      notes=f"conf={conf} pos={hit.get('position')}")
        return result
    ceo_first = _clean(row.get("ceo_first"))
    ceo_last = _clean(row.get("ceo_last"))
    if hit and hit.get("pattern") and ceo_first:
        f = ceo_first
        l = ceo_last
        built = (hit["pattern"].replace("{first}", f).replace("{last}", l)
                 .replace("{f}", f[0]).replace("{l}", l[:1]))
        if "{" not in built and "@" not in built:
            candidates.insert(0, f"{built}@{domain}")

    if hunter_only:
        # Hunter gave us no actual address. Before falling back to a name permutation,
        # check whether the company publishes one on its own site.
        if scrape and try_scrape(result, domain, ceo_first, ceo_last, scrape_delay):
            return result

        # No MX, no SMTP. Nothing here is verified, so everything lands in a status
        # send_campaign.py refuses to mail — by design, not by omission.
        if not candidates:
            result["status"] = "no_candidates"
            return result
        pattern_backed = bool(hit and hit.get("pattern"))
        result.update(
            email=candidates[0],
            status="hunter_guess",
            source="hunter_pattern" if pattern_backed else "guess",
            notes=("built from hunter pattern, unverified" if pattern_backed
                   else "ceo-name permutation, unverified — no hunter data"),
        )
        return result

    hosts = mx_hosts(domain)
    if not hosts:
        result["status"] = "no_mx"
        result["notes"] = "domain has no mail server — likely dead company"
        return result

    if not candidates:
        if scrape and try_scrape(result, domain, ceo_first, ceo_last, scrape_delay):
            return result
        result["status"] = "no_candidates"
        return result

    verdicts, catch_all = smtp_probe(hosts[0], domain, candidates)
    if catch_all is None:
        result.update(status="probe_blocked", email=candidates[0], source="guess",
                      notes="port 25 blocked or server refused — treat email as unverified")
        return result
    if catch_all:
        result.update(status="catch_all", email=candidates[0], source="guess",
                      notes="domain accepts everything — cannot verify, use at own risk")
        return result

    good = [a for a, ok in verdicts.items() if ok]
    if good:
        result.update(email=good[0], status="verified", source="smtp",
                      notes=f"{len(good)} of {len(candidates)} accepted")
        return result

    # Every permutation was rejected. The contact page is the last thing left to try.
    if scrape:
        try_scrape(result, domain, ceo_first, ceo_last, scrape_delay)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--out", default="verified.csv")
    ap.add_argument("--limit", type=int, default=100, help="how many companies to process")
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--hunter-only", action="store_true",
                    help="skip MX + SMTP probing entirely; hunter is the only source. "
                         "Use when port 25 is blocked (home ISP, AWS, GCP).")
    ap.add_argument("--size", default=None,
                    help="comma-separated size tiers to keep: 1-10, 11-50, 51-200, 201-500, "
                         "501-1000, 1001-5000, 5000+, unknown. Omit to take every tier in "
                         "smallest-first order.")
    ap.add_argument("--order", choices=("size", "score"), default="size",
                    help="'size' (default) walks tiers smallest-first, best score within "
                         "each. 'score' ignores size and takes the highest scores overall.")
    ap.add_argument("--min-confidence", type=int, default=0,
                    help="hunter confidence below this is marked hunter_low_conf and not mailed")
    ap.add_argument("--scrape", action="store_true",
                    help="when hunter finds nothing, read the company's own contact page "
                         "(<=6 pages, serial, obeys robots.txt). Yields status 'scraped'.")
    ap.add_argument("--scrape-delay", type=float, default=1.5,
                    help="seconds between page fetches on the same site (default 1.5)")
    args = ap.parse_args()

    if args.hunter_only and not HUNTER_KEY:
        raise SystemExit("--hunter-only needs HUNTER_API_KEY set, otherwise there is no source at all")

    df = pd.read_csv(args.infile)
    df = df[df["score"] >= args.min_score]

    tiers = df["size"].map(size_tier)
    df = df.assign(size_rank=[t[0] for t in tiers], size_tier=[t[1] for t in tiers])

    if args.size:
        wanted = {s.strip() for s in args.size.split(",")}
        unknown = wanted - {lbl for _, lbl in TIERS} - {UNKNOWN_TIER}
        if unknown:
            raise SystemExit(f"unknown size tier(s): {', '.join(sorted(unknown))}\n"
                             f"valid: {', '.join(lbl for _, lbl in TIERS)}, {UNKNOWN_TIER}")
        df = df[df["size_tier"].isin(wanted)]

    if args.order == "size":
        # Smallest companies first, best-scoring within each tier. At a 10-person
        # startup the CEO reads their own mail; at 5,000 people it goes to a recruiter.
        df = df.sort_values(["size_rank", "score"], ascending=[True, False])
    else:
        df = df.sort_values("score", ascending=False)

    df = df.head(args.limit)
    rows = df.to_dict("records")

    if not rows:
        raise SystemExit("no rows matched those filters — check --size / --min-score")

    print(f"{len(rows)} rows after filters "
          f"(min-score={args.min_score}, size={args.size or 'all tiers'}, "
          f"order={args.order}, limit={args.limit}), "
          f"{'HUNTER ONLY' if args.hunter_only else 'hunter + smtp probe'}", file=sys.stderr)
    spread = df.groupby("size_tier", sort=False).size()
    print("  queue by tier: "
          + ", ".join(f"{lbl}={n}" for lbl, n in spread.items()), file=sys.stderr)

    con = db_init()
    results = []
    todo = []
    for r in rows:
        cached = db_get(con, r["domain"])
        if cached:
            results.append(cached)
        else:
            todo.append(r)

    print(f"{len(results)} cached, {len(todo)} to resolve", file=sys.stderr)

    # Six threads against a 50-credit free plan means up to six more calls in flight
    # after the pool runs dry. Serialise so CREDITS_EXHAUSTED stops it on the first 403.
    workers = args.workers
    if args.hunter_only and workers > 2:
        workers = 2
        print(f"--hunter-only: capping workers {args.workers} -> 2 to avoid overshooting credits",
              file=sys.stderr)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_one, r, hunter_only=args.hunter_only,
                               min_confidence=args.min_confidence,
                               scrape=args.scrape, scrape_delay=args.scrape_delay): r
                   for r in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            db_put(con, res["domain"], res)
            results.append(res)
            print(f"[{i}/{len(todo)}] {res['domain']:<32} {res['status']:<14} {res['email'] or ''}",
                  file=sys.stderr)

    # Cached rows from an older run may predate the size/score fields, so union the keys
    # rather than trusting whichever result happens to be first.
    fieldnames = list(dict.fromkeys(k for r in results for k in r))
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, restval="")
        w.writeheader()
        w.writerows(results)

    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("\n" + json.dumps(counts, indent=2), file=sys.stderr)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
