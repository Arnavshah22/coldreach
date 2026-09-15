#!/usr/bin/env python3
"""
send_campaign.py — throttled, resumable cold email sender.

Defaults to DRY RUN. Nothing leaves your machine until you pass --send.

Before you use this at all:
  1. Buy a separate domain. Do NOT send from your primary Gmail.
  2. Set SPF, DKIM, DMARC on it. Check with mail-tester.com (aim 9+/10).
  3. Warm the mailbox for 2-3 weeks: 5/day, then 10, then 20.
  4. Plain text only. No tracking pixels, no HTML, at most one link.
  5. 25 emails/day/mailbox hard ceiling. This script enforces it.

  pip install pandas jinja2
  export SMTP_HOST=smtp.zoho.com SMTP_PORT=587
  export SMTP_USER=you@example.com SMTP_PASS=...
  python send_campaign.py verified.csv --template template.txt          # dry run
  python send_campaign.py verified.csv --template template.txt --send
"""
import argparse
import json
import os
import random
import re
import smtplib
import sqlite3
import sys
import textwrap
import time
import unicodedata
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

import pandas as pd
from jinja2 import Template

DB = "campaign.sqlite"
DAILY_CAP = 25
FROM_NAME = os.getenv("FROM_NAME", "")      # set it in .env; blank sends a bare address
FROM_ADDR = os.getenv("SMTP_USER", "")
SEND_STATES = {"verified", "found"}


def db_init():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS sent (
        email TEXT PRIMARY KEY, company TEXT, subject TEXT, sent_on TEXT, ts REAL)""")
    con.commit()
    return con


def sent_today(con):
    return con.execute("SELECT COUNT(*) FROM sent WHERE sent_on=?", (date.today().isoformat(),)).fetchone()[0]


def sent_addresses(con):
    """Every address already mailed, as a set — one query, not one per row."""
    return {e for (e,) in con.execute("SELECT email FROM sent")}


WRAP_AT = 78


def _rewrap(text):
    """Normalise line length across the whole body.

    The template's fixed prose is hand-wrapped, but the per-company paragraphs arrive
    from custom.csv as one long line. Mixing the two reads as machine-assembled. Reflow
    every prose paragraph to one width, leaving lists, links and the sign-off alone.
    """
    out = []
    for block in text.split("\n\n"):
        lines = block.split("\n")
        preformatted = (
            any(ln.lstrip().startswith(("-", "*", "•")) for ln in lines)
            or all(len(ln) < 40 for ln in lines)      # greeting, sign-off
            or any("http" in ln for ln in lines)
        )
        if preformatted:
            out.append(block)
        else:
            joined = " ".join(ln.strip() for ln in lines if ln.strip())
            out.append(textwrap.fill(joined, width=WRAP_AT, break_long_words=False,
                                     break_on_hyphens=False))
    return "\n\n".join(out)


# Anything still carrying a placeholder is a draft, not an email. Sending one of these
# to a founder is unrecoverable — you do not get a second first impression.
PLACEHOLDER_MARKERS = ("<<<", ">>>", "TODO", "REPLACE THIS", "XXX")


class PlaceholderError(Exception):
    """A single row is not ready to send. Skip it; keep the campaign running."""


def _fold(s):
    """Lowercase and strip accents, so 'Áine' and 'aine' compare equal."""
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


# Local parts that are a desk, not a person. Greeting one of these by name is the
# clearest possible signal that the list was scraped.
ROLE_LOCALPARTS = {
    "info", "contact", "contactus", "hello", "hi", "hey", "help", "support",
    "admin", "administrator", "office", "mail", "email", "enquiries", "inquiries",
    "team", "sales", "marketing", "press", "media", "partnerships", "partners",
    "business", "bd", "careers", "jobs", "hiring", "recruiting", "hr", "people",
    "founders", "founder", "ceo", "billing", "accounts", "accounting", "finance",
    "legal", "privacy", "security", "abuse", "postmaster", "webmaster", "noreply",
    "no-reply", "donotreply", "newsletter", "feedback", "service", "customerservice",
    # Compound desks arrive dotted — supplier.relations@, partner.support@. The head
    # token is matched too, so listing the first word here is enough.
    "supplier", "suppliers", "vendor", "vendors", "relations", "partner", "wholesale",
    "orders", "shop", "store", "booking", "bookings", "reservations", "community",
    "events", "editor", "editorial", "studio", "agency", "general", "inbox", "web",
}


def mailbox_matches_person(email, ceo):
    """Does this address plausibly belong to the person named in the `ceo` column?"""
    email, ceo = str(email or ""), _fold(ceo).strip()
    if "@" not in email or not ceo:
        return False
    names = [p for p in re.split(r"[^a-z]+", ceo) if len(p) > 1]
    if not names:
        return False
    first, last = names[0], names[-1]
    local = _fold(email).split("@", 1)[0]
    tokens = [t for t in re.split(r"[^a-z]+", local) if t]
    squashed = re.sub(r"[^a-z]", "", local)
    # first+last-initial ("koreyb" for Korey Bachelder) is common and was falling
    # through to the local part verbatim, greeting him as "Koreyb".
    return (first in tokens or last in tokens
            or squashed in {first, first + last, last + first,
                            first[0] + last, first + last[0]})


def greeting_name(email, ceo):
    """Who to greet, or "" for "Hi there".

    The `ceo` name comes from the company record; the address comes from Hunter or a
    contact-page scrape, and the two are frequently not the same human. Both failure
    modes are live in the current verified set:

      * role inboxes (info@, support@, help@, admin@, partnerships@) — 11 of 20 rows.
        "Hi Bohuslav" is read by whoever staffs the queue and gives the list away.
      * a *different* employee's mailbox — 5 of 20 rows. An address reading
        luke@<company> sits against a `ceo` column naming someone else entirely,
        because the address came from a contact page and the name came from the
        list. Greeting Luke as Jonathan is worse than not greeting him at all.

    So: trust the CEO name only when the address carries it. Otherwise greet the
    mailbox's own name when the local part is clearly one, and fall back to "there".
    """
    if mailbox_matches_person(email, ceo):
        return str(ceo).strip().split(" ")[0]
    local = _fold(email).split("@", 1)[0]
    head = next((t for t in re.split(r"[^a-z]+", local) if t), "")
    if head in ROLE_LOCALPARTS or local.replace(".", "") in ROLE_LOCALPARTS:
        return ""
    # A bare alphabetic local part that isn't a desk is almost always a first name.
    return head.capitalize() if len(head) >= 3 and head.isalpha() else ""


PROFILE_FIELDS = ("sender_name", "sender_full_name", "default_role", "sender_intro",
                  "project", "background", "links", "availability")


def load_profile(path):
    """The sender's own facts — name, intro, project, links. Kept out of the template
    so the repo ships a structure rather than one person's application letter.

    A missing file or a missing field is not an exception here. Each one becomes a
    <<< ... >>> placeholder, which PLACEHOLDER_MARKERS then refuses to mail — the same
    failure mode as an unwritten per-company hook, and for the same reason: an
    unfinished email must fail loudly rather than go out blank. Jinja would otherwise
    render an absent field as the empty string and send a letter with no name on it.
    """
    data = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}
    for field in PROFILE_FIELDS:
        if not data.get(field):
            marker = f"<<< profile.json is missing '{field}' >>>"
            data[field] = [marker] if field == "links" else marker
    return data


def render(tpl_text, row, profile=None):
    subject_line, _, body = tpl_text.partition("\n")
    if not subject_line.lower().startswith("subject:"):
        raise SystemExit("template must start with a 'Subject: ...' line")
    # Profile first, row second: per-company fields win any name collision.
    ctx = dict(profile or {})
    ctx.update({k: ("" if pd.isna(v) else v) for k, v in row.items()})
    # Empty first_name makes the template fall through to "Hi there".
    ctx["first_name"] = greeting_name(ctx.get("email"), ctx.get("ceo"))
    subject = Template(subject_line[8:].strip()).render(**ctx)
    rendered = Template(body.strip()).render(**ctx)

    rendered = _rewrap(rendered)

    hit = next((m for m in PLACEHOLDER_MARKERS if m in rendered or m in subject), None)
    if hit:
        # Raise, don't exit: one unwritten company must not abandon the rest of the
        # campaign halfway through. main() catches this and skips the single row.
        raise PlaceholderError(
            f"rendered text still contains {hit!r} — the per-company line has not been "
            f"written yet"
        )
    return subject, rendered


def build(to_addr, subject, body, reply_to=None):
    msg = EmailMessage()
    msg["From"] = formataddr((FROM_NAME, FROM_ADDR))
    msg["To"] = to_addr
    msg["Subject"] = subject
    # RFC 5322 requires Date, and a missing one is a cheap spam signal. Gmail's
    # submission server backfills it, but not every relay does and we already
    # set Message-ID by hand.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)          # text/plain only, on purpose
    return msg


def smtp_connect():
    """One fresh connection per message.

    The pacing sleep is 90-400s and a 20-email run runs ~78 minutes. Holding a single
    SMTP session open across that is not survivable — Gmail drops idle connections in
    around 10 minutes, and the failure lands mid-campaign after some mail has already
    gone out. Reconnecting costs ~1s per message and removes the whole failure mode.
    """
    server = smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", 587)),
                          timeout=30)
    server.starttls()
    server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
    return server


def send_one(msg):
    """Send with one reconnect retry. Returns (ok, error_string)."""
    for attempt in (1, 2):
        try:
            server = smtp_connect()
            try:
                server.send_message(msg)
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return True, None
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                OSError) as e:
            if attempt == 1:
                time.sleep(5)
                continue
            return False, f"{type(e).__name__}: {e}"
        except smtplib.SMTPException as e:
            # Auth failures and refusals are not worth retrying.
            return False, f"{type(e).__name__}: {e}"
    return False, "unreachable"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile")
    ap.add_argument("--template", required=True)
    ap.add_argument("--send", action="store_true", help="actually send (default is dry run)")
    ap.add_argument("--cap", type=int, default=DAILY_CAP)
    ap.add_argument("--test-to", default=None, metavar="EMAIL",
                    help="redirect every message to this address instead of the real "
                         "recipient. Subject is prefixed [TEST -> real@addr] and nothing "
                         "is written to the sent table, so no lead is consumed.")
    ap.add_argument("--reply-to", default=None,
                    help="Reply-To header (defaults to SMTP_USER)")
    ap.add_argument("--profile", default="profile.json", metavar="JSON",
                    help="your own name, intro, project and links (see "
                         "profile.example.json). Defaults to ./profile.json.")
    ap.add_argument("--custom", default=None,
                    help="CSV of per-company template fields (role, hook, tie_back, why), "
                         "keyed on 'domain'. Merged into the row before rendering.")
    ap.add_argument("--research", default=None, metavar="CSV",
                    help="research_jobs.py output. Drops companies whose domain no "
                         "longer serves them (acquired, rebranded, dead).")
    args = ap.parse_args()

    # Explicit utf-8: Python 3.12 on Windows defaults open() to cp1252, which mangles
    # the em-dashes and middle dots in the template before they ever reach the wire.
    tpl_text = open(args.template, encoding="utf-8").read()
    profile = load_profile(args.profile)
    if not profile:
        print(f"no sender profile at {args.profile} — copy profile.example.json to "
              f"profile.json and fill it in, or every message will be refused",
              file=sys.stderr)
    df = pd.read_csv(args.infile)
    # outreach_agent.py gates on Hunter's verifier verdict; find_emails.py gates on its
    # own status column. Accept whichever this file carries.
    if "verification_status" in df.columns:
        before = len(df)
        df = df[(df["verification_status"] == "valid") & df["email"].notna()]
        print(f"{len(df)} of {before} rows verified 'valid' by hunter", file=sys.stderr)
    elif "status" in df.columns:
        df = df[df["status"].isin(SEND_STATES) & df["email"].notna()]
    else:
        raise SystemExit(
            f"{args.infile} has no 'verification_status' or 'status' column, so there "
            f"is no way to tell which addresses were verified.\n"
            f"This stage takes the output of outreach_agent.py --out or "
            f"find_emails.py --out, not a raw company list.\n"
            f"Columns found: {', '.join(map(str, df.columns))}")

    if args.research:
        # A verified mailbox is not the same thing as a live company. An acquired
        # company's domain 301s to the acquirer while a founder address on the old
        # domain still verifies `valid` — the address works, the company in the
        # list does not exist any more. Mail sent there is at best confusing and at
        # worst reaches the acquirer with a pitch written about a company they
        # bought. Four of twenty rows, on the author's list.
        DEAD = {"redirected_offsite", "unreachable", "dead", "parked"}
        res = pd.read_csv(args.research)
        if "site_status" not in res.columns:
            raise SystemExit(f"{args.research} has no 'site_status' column — "
                             f"is it research_jobs.py output?")
        status = dict(zip(res["domain"], res["site_status"]))
        gone = [r for r in df.to_dict("records") if status.get(r.get("domain")) in DEAD]
        for r in gone:
            print(f"dropped {r['company']} <{r['email']}>: "
                  f"{status[r['domain']]}", file=sys.stderr)
        df = df[~df["domain"].map(lambda d: status.get(d) in DEAD)]
        print(f"{args.research}: dropped {len(gone)}, {len(df)} companies still live",
              file=sys.stderr)

    if args.custom:
        # The hand-written per-company paragraphs live in their own file so re-running
        # find_emails.py never overwrites them.
        custom = pd.read_csv(args.custom)
        if "domain" not in custom.columns:
            raise SystemExit(f"{args.custom} needs a 'domain' column to join on")
        overlap = [c for c in custom.columns if c != "domain" and c in df.columns]
        df = df.drop(columns=overlap).merge(custom, on="domain", how="left")
        # Only the fields the template actually reads count as "written" — the _-prefixed
        # context columns and the blank needs_research flag are not part of the email.
        fields = [c for c in ("role", "hook", "tie_back", "why") if c in df.columns]
        filled = int(df[fields].notna().all(axis=1).sum()) if fields else 0
        print(f"merged {args.custom}: {filled} of {len(df)} rows have all "
              f"{len(fields)} template fields written", file=sys.stderr)

    con = db_init()
    budget = args.cap - sent_today(con)
    if budget <= 0:
        print(f"daily cap of {args.cap} already used. stop.", file=sys.stderr)
        return

    done = sent_addresses(con)
    queue = [r for r in df.to_dict("records") if r["email"] not in done][:budget]
    print(f"{len(queue)} queued, {budget} of {args.cap} left today, "
          f"{'SENDING' if args.send else 'DRY RUN'}\n", file=sys.stderr)

    if args.send:
        for var in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
            if not os.getenv(var):
                raise SystemExit(f"{var} is not set — refusing to start a send run")

    sent_count = skipped = failed = 0
    for i, row in enumerate(queue, 1):
        label = f"[{i}/{len(queue)}] {row['company']}"
        try:
            subject, body = render(tpl_text, row, profile)
        except PlaceholderError as e:
            skipped += 1
            print(f"{label}: SKIPPED — {e}", file=sys.stderr)
            continue

        # --test-to redirects everything to one inbox and never touches the sent table,
        # so a test run cannot consume a real lead or mark it as already-contacted.
        real_to = row["email"]
        to_addr = args.test_to or real_to
        testing = bool(args.test_to)
        if testing:
            subject = f"[TEST -> {real_to}] {subject}"

        msg = build(to_addr, subject, body,
                    reply_to=args.reply_to or os.getenv("SMTP_USER"))

        if not args.send:
            print("=" * 70)
            print(f"TO: {to_addr}  ({row['company']})"
                  + (f"   [would really go to {real_to}]" if testing else ""))
            print(f"SUBJECT: {subject}\n")
            print(body)
            continue

        ok, err = send_one(msg)
        if not ok:
            failed += 1
            print(f"{label}: FAILED {to_addr}: {err}", file=sys.stderr)
            continue

        sent_count += 1
        if testing:
            print(f"{label}: TEST sent -> {to_addr} (would be {real_to}); "
                  f"not recorded", file=sys.stderr)
        else:
            con.execute("INSERT INTO sent VALUES (?,?,?,?,?)",
                        (real_to, row["company"], subject,
                         date.today().isoformat(), time.time()))
            con.commit()
            print(f"{label}: sent -> {real_to}", file=sys.stderr)

        if i < len(queue):
            pause = 5 if testing else random.uniform(90, 400)
            print(f"   pausing {pause:.0f}s", file=sys.stderr)
            time.sleep(pause)          # human-ish spacing, not a burst

    if args.send:
        print(f"\nsent {sent_count}, skipped {skipped}, failed {failed}"
              + ("  (TEST RUN — nothing recorded)" if args.test_to else ""),
              file=sys.stderr)


if __name__ == "__main__":
    main()
