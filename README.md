# coldreach

A three-stage pipeline for sending **individually written** job-application emails to small
companies: clean a startup list → find a real address per company → send slowly, from your
own mailbox, with a human-written paragraph in every message.

It is deliberately **not** a bulk mailer. The cap is 25 a day, there is no HTML part and no
tracking pixel, and a message whose per-company paragraph you haven't written is refused
rather than sent. Those limits are the point — see [Why it's slow on purpose](#why-its-slow-on-purpose).

```
  your startup list (CSV)
        │  clean_list.py        dedupe by domain, score, build name permutations
        ▼
  startups_clean.csv
        │  research_jobs.py     is this still a company? are they hiring? remote-friendly?
        ▼  (optional, free)
  research.csv
        │  outreach_agent.py    scrape their contact page → verify with Hunter
        ▼
  outreach.csv                  one verified address per company
        │  send_campaign.py     render per company, send ≤25/day, dry run by default
        ▼
  sent mail
```

---

## Quick start

Requires **Python 3.10+**. Nothing else — no database, no server, no account beyond an
email address.

```bash
git clone https://github.com/YOUR-USERNAME/coldreach.git
cd coldreach
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env                 # fill in your mailbox
cp profile.example.json profile.json # fill in who you are
```

Then see it work, against invented companies, sending nothing:

```bash
python send_campaign.py sample_startups.csv --template template.txt --custom sample_custom.csv
```

That prints two fully rendered emails and refuses the other four, because only two of the
six sample companies have a written paragraph. **That refusal is the feature.** Fill in
`profile.json` first or it will refuse all six.

---

## Setup

### 1. Your mailbox — `.env`

Copy `.env.example` to `.env` and fill it in. `.env` is gitignored.

Nothing auto-loads it. Export before running:

```powershell
# PowerShell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v
}
```

```bash
# bash / zsh
set -a && source .env && set +a
```

**Gmail:** `SMTP_PASS` must be a 16-character [App Password](https://myaccount.google.com/apppasswords),
not your account password — Gmail has rejected those over SMTP since 2022. App Passwords
require 2-Step Verification to be on.

### 2. Who you are — `profile.json`

Copy `profile.example.json` to `profile.json` and replace every value. This is your name,
your one-line intro, the project you're proudest of, your links. It is gitignored, because
it's yours.

Every field is required. A missing one renders as `<<< profile.json is missing 'project' >>>`
and the send is refused — the same guard that catches an unwritten company paragraph.

> Write the `project` paragraph around **what the hard part turned out to be**, not what the
> thing does. The hard part is the only bit a reader can't get from your CV, and it's what
> earns a reply.

### 3. Hunter.io — optional

[Hunter](https://hunter.io/api-keys) finds and verifies addresses. The free plan gives 50
domain-searches and 100 verifications a month, resetting on the 24th.

Without a key everything still runs: the pipeline falls back to reading companies' own
contact pages, which is free and hits roughly half the time.

---

## Running it

### Stage 1 — clean your list

```bash
python clean_list.py "your-startup-list.csv" --out startups_clean.csv
```

Dedupes by domain, scores each row, and builds six email-candidate permutations from the
CEO's name. Expects the real header on the second row (`--header 1`, the default, which is
how spreadsheet exports usually land); pass `--header 0` for a plain CSV.

The score is `3*fresh + 2*tech_hit + size_ok + has_jobpage - 2*jobpage_stale`, max 7.
Downstream stages gate on it, so changing the weights changes who gets contacted.

### Stage 2 — check the companies still exist

```bash
python research_jobs.py startups_clean.csv --size 1-10 --limit 40 --out research.csv
```

Free, sends nothing, spends no API credit. Reads each company's own site and answers three
questions the rest of the pipeline was guessing at: is this still a company at this domain,
is a relevant role open, and can they hire someone outside their own country.

**Run this.** On the author's list, **28% of companies were no longer at their domain** —
acquired, rebranded or dead. A verified mailbox is not a live company.

### Stage 3 — find an address

```bash
python outreach_agent.py --limit 25                          # dry plan, spends nothing
python outreach_agent.py --limit 25 --run --out outreach.csv
```

Scrapes the company's contact page first, then spends a Hunter verification on what it
found. Scrape-first is the default because the free plan carries twice as many
verifications as searches, and the scrape is free. `--strategy hunter-first` inverts it.

Hunter's verifier runs a real SMTP check server-side, which is what makes this trustworthy
from a home connection — most ISPs and cloud providers block outbound port 25, so the
local probe in `find_emails.py` can't complete a handshake.

### Stage 4 — write the emails

This is the part that isn't automated, on purpose.

`custom.csv` is keyed on `domain` and holds four columns per company:

| field | what it is |
|---|---|
| `role` | the role you're pitching for; also the subject line |
| `hook` | answers a requirement or constraint **they** named, in their language |
| `tie_back` | one sentence mapping your hard problem onto their problem |
| `why` | the hard problem in their domain, and why its feedback loop appeals to you |

Look at `sample_custom.csv` for the shape. Rows you haven't written are left blank and are
refused at send time, one at a time — the rest of the run continues.

> If a company's description is too thin to write a real hook from, **say so plainly in the
> hook or skip the company**. A generic paragraph formatted as personalisation reads worse
> than no email at all.

### Stage 5 — send

```bash
# dry run: prints every rendered email, sends nothing
python send_campaign.py outreach.csv --template template.txt --custom custom.csv

# to yourself first, through the real SMTP path
python send_campaign.py outreach.csv --template template.txt --custom custom.csv \
    --test-to you@example.com --cap 2

# for real
python send_campaign.py outreach.csv --template template.txt --custom custom.csv --send
```

`--test-to` redirects every message to one inbox, prefixes the subject, and **never writes
to the sent table** — so a test can't consume a lead. It's the only way to see real
rendering, threading and spam-folder placement before touching a real company.

Add `--research research.csv` to drop companies that no longer exist at their domain.

---

## Why it's slow on purpose

These are load-bearing. Raising them is how you get your domain burned and your mail
filtered, and the people receiving these are being asked for a job.

| limit | value | why |
|---|---|---|
| Daily cap | 25 per mailbox | Counted by calendar date in `campaign.sqlite`. |
| Spacing | random 90–400s between sends | Spacing, not a burst. |
| Format | text/plain only | No HTML part, no tracking pixel. A tracked job application is a bad look. |
| Dedupe | `sent` table keyed on email | Re-running never double-sends. |
| Placeholder guard | refuses `<<<`, `TODO`, `REPLACE THIS`, `XXX` | A half-written template cannot reach 60 founders in one run. |
| Scraper | ≤6 pages per domain, serial, 1.5s apart, robots.txt honoured | You're about to ask these people for a job. Don't hammer their site first. |

Before you send to anyone real:

- Use a mailbox with some history. A brand-new address sending cold mail is the classic
  spam signature.
- Check SPF, DKIM and DMARC if you're on your own domain.
- If it's a new domain, warm it up for two to three weeks first.
- Send to yourself with `--test-to` and read the result on a phone.

---

## Do not commit your leads

The output of this pipeline is **other people's names and working email addresses**. The
`.gitignore` excludes every file that can carry them — `startups_clean.csv`, `custom.csv`,
`outreach*.csv`, `research.csv`, `verified.csv` and all `*.sqlite`.

Keep it that way. A public repo is indexed and scraped within days, and deleting a file
does not reach the forks and caches that already have it. If you add a new output file, add
it to `.gitignore` first.

The only data files in this repo are `sample_startups.csv` and `sample_custom.csv`. Every
domain in them is under `example.com` / `.org` / `.net`, which
[RFC 2606](https://www.rfc-editor.org/rfc/rfc2606) reserves so they can never belong to
anyone.

---

## Known limits

- **Outbound port 25 is blocked** on most home ISPs and on AWS/GCP, so `find_emails.py`'s
  SMTP probe returns `probe_blocked` for everything. Use `outreach_agent.py`, which gets a
  real SMTP verdict through Hunter's server-side verifier instead.
- **Hunter's free tier is the real constraint** on stage 3, not `--limit`. Budget the
  credits at the top of your score distribution.
- **Startup lists rot fast.** Run `research_jobs.py` before spending anything.
- **Most tiny companies have no careers page at all** — 30 of 42 live ones, in the author's
  measurements. That's a real property of 1–10 person startups, which is why the template
  doesn't claim to be answering a posting.
- `same_brand()` uses a short list of two-part TLDs (`co.uk`, `com.au`, …) rather than the
  full public-suffix list. A domain on a suffix outside that list will mislabel; add it to
  `TWO_PART_TLDS` or install `tldextract`.

## Use it honestly

This sends real email to real people. Keep to the register it was built for: a small number
of individually written applications, from your own mailbox, to companies you'd actually
work at. Honour unsubscribe requests immediately, and don't mail anyone who has told you no.

Every claim in your template should be checkable against your CV. A cold email whose claims
contradict the attached résumé fails at exactly the moment it was working.

## License

MIT — see [LICENSE](LICENSE).
