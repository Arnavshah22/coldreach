# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A three-stage job-hunt outreach pipeline: scrape/clean a startup list → resolve a real
email per company → send a throttled, personalized cold email campaign. Three standalone
Python scripts, no package, no tests, no build. Each stage hands off to the next via CSV.

```
Remotive "900 Startups" CSV
      │ clean_list.py          (one-shot, already run)
      ▼
startups_clean.csv             771 rows, deduped by domain, scored, email guesses
      │ find_emails.py         (cached in findemails.sqlite)
      ▼
verified.csv                   one email + status per company
      │ send_campaign.py       (cached in campaign.sqlite, dry run by default)
      ▼
sent mail                      ≤25/day
```

## Commands

```bash
pip install pandas dnspython requests jinja2

# stage 2 — resolve emails (writes/reads findemails.sqlite for resume)
export HUNTER_API_KEY=...                  # optional; without it, falls back to SMTP probing
python find_emails.py startups_clean.csv --limit 100 --min-score 4 --out verified.csv

# stage 3 — dry run prints every rendered email to stdout, sends nothing
python send_campaign.py verified.csv --template template.txt
# actually send
export SMTP_HOST=smtp.zoho.com SMTP_PORT=587 SMTP_USER=... SMTP_PASS=...
python send_campaign.py verified.csv --template template.txt --send
```

`clean_list.py` is **not** re-runnable as written — its `SRC` and output path are hardcoded
to `/mnt/user-data/...` sandbox paths. Its output, `startups_clean.csv` (771 rows), is checked in; edit
the paths if you need to regenerate from a fresh Remotive export.

## The scoring model (clean_list.py)

`score = 3*fresh + 2*tech_hit + size_ok + has_jobpage - 2*jobpage_stale`, max 7. `fresh` means
the freshness column says "new"; `tech_hit` is a keyword regex over the one-line description;
`size_ok` is 1-10/11-50/51-200 employees; `jobpage_stale` flags dead job boards (angel.co,
stackoverflow jobs, github jobs). Downstream stages filter on this — `--min-score 4` is the
default gate in `find_emails.py`, so changing the weights changes who gets contacted.

`email_candidates` is a `|`-joined list of six pattern permutations built from the CEO's name
(`{f}`, `{f}.{l}`, `{f}{l}`, `{fi}{l}`, `{fi}.{l}`, `{f}_{l}` @ domain). Names are
accent-stripped and initials/particles dropped before splitting.

## Email resolution order (find_emails.py)

1. **Hunter.io domain-search** — ranks returned emails by seniority keywords in the position
   field, then confidence. If Hunter gives no email but does give a `pattern`, that pattern is
   rendered with the CEO name and pushed to the front of the candidate list.
2. **MX lookup** — no MX record ⇒ status `no_mx`, treated as a dead company, no probing.
3. **SMTP RCPT TO probe** — one connection per domain, catch-all tested first with a random
   local part. Catch-all or connection failure short-circuits to a guess.

Statuses that come out: `verified` (SMTP accepted), `found` (Hunter), `catch_all`,
`probe_blocked`, `no_mx`, `no_candidates`, `not_found`. `send_campaign.py` only sends to
`verified` and `found` — widening `SEND_STATES` means mailing unverified guesses and burning
the sending domain's reputation.

Step 3 needs outbound **port 25**, which home ISPs and AWS/GCP block; when blocked every
result is `probe_blocked`. Probe from a throwaway IP, never the one you send from.

## Deliverability constraints — these are load-bearing, not style

The sender is deliberately conservative and the code enforces it:

- `DAILY_CAP = 25` per mailbox, counted against `campaign.sqlite` by calendar date.
- `time.sleep(random.uniform(90, 400))` between sends — spacing, not a burst.
- `msg.set_content(body)` only — **text/plain, no HTML part, no tracking pixel** by design.
- `sent` table is keyed on email, so re-running never double-sends.

The docstrings carry the operational preconditions (separate domain, SPF/DKIM/DMARC,
2-3 week mailbox warmup). Don't raise the cap, shorten the sleep, add HTML, or remove the
dedupe without being asked.

## Templates

`template.txt` is Jinja2 with a `Subject: ...` first line (required — `render()` raises
otherwise). The row dict is the context, plus a derived `first_name` from the `ceo` column.
The template intentionally contains a `<<< REPLACE THIS LINE PER COMPANY >>>` placeholder —
the per-company observation is written by hand, not generated. A campaign run that still has
that string in the body is not ready to send.

## Local environment (as of 2026-09-12)

Credentials live in `.env` (gitignored). Nothing in the pipeline auto-loads it — the scripts
read `os.getenv` only, so export before running:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v
}
```

**Python 3.12.10 is installed per-user and is NOT on PATH.** The `python.exe` that *is* on
PATH is the zero-byte Microsoft Store stub and will not run anything. Always call the real
interpreter by full path:

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py find_emails.py startups_clean.csv --hunter-only --size 1-10 --limit 5
```

Installed via `winget install --id Python.Python.3.12 --scope user`. Note `winget` itself is
also absent from PATH — invoke it as `$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe`.
Deps present: pandas 3.0.5, dnspython 2.8.0, requests 2.34.2, Jinja2 3.1.6. **pandas is 3.x**,
a major version ahead of what these scripts were written against — `find_emails.py` and
`send_campaign.py` are exercised and fine, but `clean_list.py` is untested on it.

**Hunter.io is on the Free plan: 50 domain-searches/month, resetting the 24th.** That is the
binding constraint on stage 2, not `--limit`. 311 of the 771 rows clear `--min-score 4`, so
Hunter can cover roughly one sixth of the eligible list per month; everything past the credit
ceiling silently falls through to the SMTP-probe path. Budget the credits at the top of the
score distribution (53 rows score 6, 6 score 5, 252 score 4) rather than letting a large
`--limit` spend them in arbitrary order.

## --hunter-only mode

Added because port 25 is blocked here, which makes the SMTP probe incapable of returning
anything but `probe_blocked`. The flag drops steps 2 and 3 entirely:

```powershell
python find_emails.py startups_clean.csv --hunter-only --size 1-10 --limit 45 `
    --min-confidence 70 --out verified.csv
```

Statuses it can emit: `found` (a real address Hunter had on file, at or above
`--min-confidence`), `hunter_low_conf`, `hunter_guess` (a permutation, pattern-backed or
not), `no_candidates`. **`verified` is unreachable in this mode** — nothing is verified
without an SMTP handshake — so `send_campaign.py` will only ever mail the `found` rows.
That is the intended safety property; don't "fix" it by widening `SEND_STATES`.

`--size` filters employee buckets and normalises the source data's inconsistent spellings
(`1-10` also matches `2-10` and a bare `0`; trailing spaces are stripped). Rows are sorted
by score descending before `--limit` applies, so the scarce Hunter credits go to the best
rows rather than to whatever order the CSV happened to be in.

A 401/403/451 from Hunter sets a module-level `CREDITS_EXHAUSTED` event; in-flight workers
stop calling the API rather than grinding through the rest of the list. `--hunter-only` also
caps workers at 2 for the same reason.

Current targeting: 64 rows are size 1-10 with score ≥ 4 (11 at score 6, 53 at score 4),
62 of them carry email candidates — against 49 remaining credits.

## Contact-page fallback (`scrape_contact.py`, `--scrape`)

When Hunter returns no address, `find_emails.py --scrape` reads the company's own site
before falling back to a name permutation. Hooked in at three points: no Hunter result in
`--hunter-only` mode, `no_candidates`, and all-SMTP-rejections. New status: `scraped`.

The politeness budget is deliberate and should stay that way — these are companies the
campaign is about to ask for a job:

- at most 6 pages per domain (`/contact`, `/contact-us`, `/about`, `/about-us`, `/team`, `/`)
- one request at a time, `--scrape-delay` (default 1.5s) between them
- `robots.txt` fetched **once per domain**, failing open only when it's missing or
  unparseable. It is site-wide, so the first version's per-path refetch meant up to six
  extra requests that never counted against the page cap — the 6-page promise above was
  really up to 12 until `_load_robots()` and `_robots_ok()` were split apart
- identifying User-Agent, no proxies, no retries, early exit once a named human is found

Addresses are filtered (asset filenames like `logo@2x.png`, vendor domains, `noreply@`,
off-domain addresses) and ranked: CEO-name match 100 → unrecognised local part 60 →
`founders@`/`careers@` 55–50 → `hello@` 45 → `info@` 30 → `support@` 10. A `mailto:` link
scores +10 over the same address found in body text, since publishing it is intentional.
That bonus holds across the whole domain, not only within one page: `found` outlives the
page loop, and the original `setdefault` kept whichever score arrived first, so an
address sitting in body text on `/about` stayed at the lower score when `/contact` later
published it as a link.

**The CEO-name match is on name tokens, not substrings.** `ceo_first.lower() in local`
scored any role inbox whose local part happened to contain a short name at 100 — the tier
reserved for a matched human — and "al" is inside "sales". `_name_score()` now matches
whole tokens plus the squashed forms real mailboxes use (`jsmith`, `john.smith`,
`johnsmith`, `koreyb`), which is the rule `mailbox_matches_person()` in `send_campaign.py`
already arrived at for the salutation. Both ends of the pipeline now agree on what counts
as "this address belongs to that person"; they did not before.

Measured hit rate on the first 10 rows of the 1-10 segment: **5/10**, one of which
was a founder's own first name at their own domain — the best case the ranking
is built for.

`scraped` is **not** in `send_campaign.py`'s `SEND_STATES`, so nothing is mailed from it
until that is changed deliberately.

### Rejected: StealthScrape

Evaluated `github.com/CanXploit/StealthScrape` for this slot and did not use it. It has no
email-extraction code in any of its five plugins (`finder.py` is a 0-byte file); it pulls a
URL list from the Wayback CDX API, filters by file extension, and bulk-downloads files with
`NUM_WORKERS = 200` threads. Wrong output type, interactive-only interface, and a request
volume that would get this campaign's IP blocked by the companies it is targeting.

## Size-tiered prioritisation (`--order size`, the default)

Leads are worked smallest-company-first, best score within each tier. The reasoning is
reply rate, not list hygiene: at a 10-person startup the CEO reads their own mail, at 5,000
people a cold pitch reaches a recruiting queue.

`size_tier()` parses the bracket rather than string-matching it, because the source column
spells the same thing many ways — 22 distinct raw values across `1-10`/`2-10`/`0`,
`11-50 ` with a trailing space, `1001-5000` next to `1,001-5,000`, `10,001+`, `10000+`, and
one `NaN`. Tiering is on the **upper** bound so `10-50` lands in `11-50` rather than `1-10`.
Unparseable sizes tier as `unknown` and sort last, so they never displace a real lead.

Eligible pool at `--min-score 4`, in queue order:

| tier | rows | cumulative |
|---|---|---|
| 1-10 | 64 | 64 |
| 11-50 | 144 | 208 |
| 51-200 | 97 | 305 |
| 201-500 | 2 | 307 |
| 501-1000 | 2 | 309 |
| 1001-5000 | 1 | 310 |
| 5000+ | 1 | 311 |

`--order score` restores the old size-blind behaviour. `--size` filters to named tiers and
rejects anything not in the list above rather than silently matching nothing.

## The full intended run

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $py find_emails.py startups_clean.csv --hunter-only --scrape --limit 45 --out verified.csv
```

Per company: Hunter → if no address, contact-page scrape → if nothing published, a
name-permutation guess. Smallest companies first. Statuses in descending trustworthiness:
`found` (Hunter had it) > `scraped` (published on their own site) > `hunter_guess` /
`hunter_low_conf` (unverified permutations). Only `found` is mailable until `SEND_STATES`
is changed.

## Template facts: check every claim against the CV

`template.txt` was written from a real application email and then checked, line by
line, against the CV that would be attached to it. Two claims in the first draft did
not survive that check and were cut: a company name that appeared nowhere on the CV,
and an employment status the CV contradicted.

That check is worth doing deliberately, because both errors were the same kind. Prose
written from memory drifts toward the more impressive version — a role becomes
full-time, a side project becomes a job — and neither drift is a lie you would tell
on purpose. Read the draft with the CV open next to it.

Anything in this template must be checkable against the CV you attach. A cold email
whose claims contradict it fails at exactly the moment it was working: the reader is
interested enough to look.

`render()` refuses to send any body still containing `<<<`, `>>>`, `TODO`, `REPLACE THIS`,
or `XXX`. The per-company line is written by hand; the guard exists so a half-finished
template cannot reach 64 founders in one run.

`open(args.template, encoding="utf-8")` is deliberate — Python 3.12 on Windows defaults to
cp1252 and would corrupt the em-dashes and middle dots before they hit the wire.

## Template structure (current)

`template.txt` follows the structure of a real application email that was sent by hand.
Fixed prose stays verbatim across every send; four fields vary per company and come from
`custom.csv` via `send_campaign.py --custom`:

| field | what it is |
|---|---|
| `role` | the role being pitched for; also the subject line |
| `hook` | answers a requirement or constraint *they* named, in their language |
| `tie_back` | one sentence mapping the retrieval-quality problem onto their problem |
| `why` | the hard problem in their domain and why its feedback loop appeals |

`custom.csv` is keyed on `domain` and joined left, so re-running `find_emails.py` never
overwrites hand-written prose. Its `_description` and `_ceo` columns are context for
writing and are ignored by the template. Scaffolded with the 64 rows of the 1-10 tier.

Unfilled fields render as `<<< … >>>` via Jinja's `default(…, true)`, which the
`PLACEHOLDER_MARKERS` guard then refuses to send. Unwritten rows fail loudly, one at a
time, instead of mailing a half-finished draft.

`_rewrap()` reflows prose paragraphs to 78 columns after rendering, because the fixed text
is hand-wrapped while `custom.csv` fields arrive as single long lines; mixing the two reads
as machine-assembled. Lists, URLs and the sign-off are left alone.

### Not claiming a posting exists

The body says "I'd like to join {{ company }} as a {{ role }}, and I'd rather make the case
directly than wait for a posting" — **not** "applying for the {{ role }} role". `role` is
`Backend / AI Engineer` for all 64 rows because it was scaffolded, not sourced from anyone's
careers page. Claiming to apply for a posting that doesn't exist puts a falsifiable
statement in the first paragraph. If a real listing is ever sourced for a company, its
`role` can carry the posted title and the sentence still reads correctly.

### One label per email

An earlier draft opened three consecutive paragraphs with a colon label — a hook beginning
`On <their domain>:`, a fixed `A project I'm proud of:`, and `Why {{ company }}:`. All 24
written hooks used the same `On X:` construction, so the personalised paragraph was the most
visibly templated part of the email. Once a reader sees the form, the rest reads as
filled-in fields. The hook openers were rewritten to vary, `A project I'm proud of:` was
dropped, and `Why {{ company }}:` is now the only label. Keep it that way: if a new hook
needs a colon lead-in, it is competing with the one label that earns its place.

### Who gets greeted by name

`greeting_name()` decides the salutation, and it does **not** trust the `ceo` column. That
name comes from the company record while the address comes from Hunter or a contact-page
scrape, and across the 20 verified rows the two disagree constantly:

- **11 are role inboxes** (`info@`, `support@`, `help@`, `admin@`, `contact@`,
  `partnerships@`, `hello@`). Greeting the founder by name into `info@` is read by
  whoever staffs the queue and gives the list away as scraped.
- **5 are a different employee's mailbox.** An address reading `luke@` sits against a
  `ceo` column naming someone else entirely — the address came from a contact page,
  the name came from the list, and there is no reason they should match. Greeting
  Luke as Jonathan is worse than not greeting him at all.

So: use the `ceo` first name only when the local part actually carries it (accent-folded,
matching `first`, `last`, `flast`, `f.last`, `firstlast`); otherwise greet the mailbox's own
name when the local part is a plain alphabetic word of 3+ characters not in
`ROLE_LOCALPARTS`; otherwise `Hi there`. Current split: 9 by name, 11 `Hi there`. Only 4 of
those 9 are the CEO (Eric, Tom, Aleix, Ivan) — the other 5 are the mailbox's own owner,
which is the right person to greet.

The `ceo` column is still what `custom.csv`'s hooks were written against, and that is fine;
it is the *salutation* that must match the address, not the research.

The full send:

```powershell
& $py send_campaign.py verified.csv --template template.txt --custom custom.csv   # dry run
& $py send_campaign.py verified.csv --template template.txt --custom custom.csv --send
```

## research_jobs.py — validating the list before spending anything on it

```powershell
& $py research_jobs.py startups_clean.csv --only custom.csv --written-only
& $py research_jobs.py startups_clean.csv --size 1-10 --limit 40 --out research.csv
& $py send_campaign.py outreach.csv --template template.txt --custom custom.csv `
      --research research.csv          # drops companies that no longer exist
```

Reads each company's own site and answers three questions the pipeline was guessing at:
is this still a company at this domain, is a relevant role open, and can they hire
someone outside their own country. Research only — it sends nothing and spends no
Hunter credit. Output joins onto everything else on `domain`; cached in
`research.sqlite` so a re-run costs nobody a request.

### The list has rotted, and that is the headline

`startups_clean.csv` is a Remotive export from several years ago. Measured on the 24
hand-written companies: **4 are gone, 2 have moved domain, 1 blocks us, 17 are live.**

| company | what happened |
|---|---|
| Pachama | `pachama.com` → `carbon-direct.com` — acquired |
| RaRe Technologies | → `pii-tools.com` — rebranded/pivoted |
| The Sensible Code C'y | → `cantabular.com` — rebranded |
| Nomics | TLS failure, domain dead |
| Pactly | `pactly.ai` → `pactly.com` — same company, new TLD |
| Zinc | `zinc.io` → `zinc.com` — same company, new TLD |

**A verified mailbox is not a live company.** A founder address on `pachama.com`
verifies `valid` through Hunter, and the company it belongs to is now Carbon Direct. That is why
`--research` is a gate in `send_campaign.py` and not a report: four of the twenty
mailable rows were pointed at companies that no longer exist, one of them carrying the
strongest hook in `custom.csv` (the SAR/EDSR research mapped onto Pachama's carbon
verification).

`moved_domain` is deliberately *not* dropped — same company, so it stays a good lead;
fix the domain in the source list and re-run. `blocked` is not dropped either: DataCite
403s our user-agent and is very much alive. Never let "the server refused us" become
evidence a company is dead.

### Why the homepage is fetched first

One request settles liveness, and a dead domain then costs nothing further — the first
version burned five requests per company discovering nothing. The homepage also *links
to its own careers page*, which beats guessing paths: Baremetrics' real page is
`/about#careers`, which no path list would have found, while `baremetrics.com/jobs`
(the value in the `jobpage` column) redirects to the homepage.

### The jobpage column is a candidate, not an answer

Sampled from the 24: `demio.com/compare-demio` is a marketing page, `clerky.com` is a
homepage, `remoteok.io/remote-startups/graphenedb` is a third-party board, and
Import2's cell reads `angel list`. So it is tried first, then the homepage's own links,
then standard paths, and every page still has to look like a careers page before it is
believed.

### Extraction rules that took a correction

- **A title needs a role keyword *and* a job noun, from different words.** `ROLE_RE`
  alone matched "You have 4+ years of experience with Python"; sharing the word
  "devops" between both checks turned Skycrapers' product "DevOps-as-a-Service" and a
  nav link reading "Azure DevOps" into open positions. `TITLE_NOUN_RE` therefore
  excludes every word `ROLE_RE` already matches.
- **Country codes are case-sensitive.** Under `re.I`, "come join us only if you love
  data" on Gradient Metrics' homepage read as a US-only restriction. `REGION_ABBR_RE`
  is case-sensitive: "US only" is a restriction, "us only" is a pronoun.
- **Politeness budget** is scrape_contact.py's, for the same reason: ≤5 fetches per
  domain, serial, 1.5s apart, robots.txt honoured (one fetch per host — the first version
  refetched it once per candidate path), identifying UA, no retries. `Fetcher` caches the
  parsed robots.txt per host, not the verdict: caching the *answer* for the first path
  applied it to every later path on that host, which is wrong for any site that allows
  `/` and disallows `/careers`.

### Measured across the whole 1-10 tier (64 rows, 2026-09-13)

| site_status | rows | |
|---|---|---|
| live | 42 | |
| unreachable | 12 | dead domains |
| redirected_offsite | 5 | acquired or rebranded |
| moved_domain | 3 | same company, new TLD — keep, fix the domain |
| blocked | 1 | DataCite; alive, refuses our UA |
| dead | 1 | homepage 404s |

**18 of 64 — 28% — are not a company at that domain any more.** Beyond the four found
in the written set: `daocloud.com` → `heal.me`, `wearthlondon.com` → `ziracle.com`,
plus nine more dead domains. Budget research and Hunter credits against the 42, not
the 64.

Of the 42 live: **30 have no careers page at all**, 9 have one with nothing relevant,
2 are hiring, 1 says explicitly that it is not. That is a real property of 1-10 person
startups, not a bug in the scraper — and it is the evidence for two decisions here:
`role` usually cannot be sourced (so template.txt does not claim to answer a posting),
and gating outreach on an open role would cut the tier from 64 companies to 2.

## custom.csv: what is written and what is not

24 of the 64 rows in the 1-10 tier have hand-written `hook` / `tie_back` / `why`. The other
40 are blank with `needs_research=yes`, and the placeholder guard blocks them.

That split is not laziness — it is what the source data supports. The `description` column
was capped at 10 words when the list was built, so it ranges from genuinely specific
("cryptocurrency & bitcoin market data API", "Cloud hosted Neo4j graph databases") to
content-free ("We make things happen", "Consulting company", "Connecting the dots since
2007"). A hook written from the second kind is filler, and filler that is formatted like
personalisation reads worse than no email at all.

Every written hook maps to something real on the CV — no claim was invented to fit a
company. Keeping an explicit mapping is the discipline that enforces it: if a hook has
no row, it is being written to flatter the company rather than to describe you.

The shape of the mapping that was used, with the CV items generalised:

| CV item | the kind of company it was used for |
|---|---|
| satellite / remote-sensing ML research | a carbon-measurement company |
| a rate-limited market-data client: cache, request budget, staleness | data-API companies |
| a financial-returns solver: bisection over Newton-Raphson | metrics and analytics companies |
| statement parsing with reconcile-or-flag on mismatch | data-cleaning and migration companies |
| an agent pipeline: draft → human approval → publish | content and marketing-automation companies |
| entity and relation extraction over a graph | identity, graph-database and contract companies |
| a DAG engine with queues and CRM/chat integrations | workflow and integration-heavy companies |
| SSE token streaming with cancellation | live-video and streaming companies |
| a 10k-concurrent-user event system, SQL tuning | booking and scheduling companies |

One row to one kind of problem. Nine CV items covered 24 companies; a tenth company
that matched nothing got a hook saying so rather than a manufactured one.

One hook draws on work that is not on the CV itself. That is allowed — a CV is a summary,
not an exhaustive list — but it changes what the email is promising. If you reference work
the reader cannot see, be ready to talk about it in detail in a first call.

## Batch 2 — the rest of the 1-10 tier (2026-09-13)

The 1-10 tier holds 118 companies; 64 clear `--min-score 4` and were scaffolded into
custom.csv. After batch 1 (16 sent) and the liveness gate (18 dead), **30 were left**.
Enrichment returned **19 verified valid** for 17 searches and 24 verifications.
Remaining Hunter budget: 8 searches, 26 verifications.

Two gates were moved *upstream* into `outreach_agent.py`, where the money is actually
spent rather than only at send time:

- `--research research.csv` skips companies whose domain no longer serves them. Without
  it this run would have bought domain-searches for 18 dead companies.
- `--skip-sent campaign.sqlite` skips anyone already emailed, cross-referenced through
  `--out`. Re-resolving a sent lead spends a credit to learn nothing.

Both default on; pass `''` to disable.

### The prose quality split is real and worth keeping visible

26 companies needed hook/tie_back/why. They do **not** divide evenly:

- **15 have a genuine technical mapping** — Parknav (satellite/SAR inference → parking
  prediction, and they have a posted *Java* Backend Engineer role, which the hook names
  honestly since the resume is Python/TypeScript), HeyTaco! (Slack/MCP integrations),
  Advocate (live metrics, staleness, SSE), MailTag.io (irreversible sends → the
  approval interrupt), Punchpass and Attractions.io (10k concurrent peak), Wild Audience
  and Forget The Funnel (the drafting agent), MotorLot and 33 Sticks (reconcile-or-flag),
  Loot Crate (DAG/queues), Cladwell (retrieval), Chromatic, Delicious Brains, Noiiz.
- **11 have none** — Parablesoft, Barefoot Coders, Marketade, Barrel Roll, owl power,
  Troop Themes, FoxAndSheep, Happy Herbivore, Sticky, The Adventure Junkies,
  ElevenYellow. These are consultancies, WordPress shops, Shopify themes and consumer
  apps with no backend/AI surface.

For the second group the hook **says so plainly** ("WordPress is not where my experience
is, so I will not pretend otherwise") rather than manufacturing a connection. That is a
deliberate choice against the alternative of a generic paragraph formatted as
personalisation, which is the failure mode this file warns about. The Adventure Junkies'
own jobs page asks for designers, copywriters and outdoor experts — its hook opens by
acknowledging the email may not fit at all.

Expect the reply rate to differ sharply between the two groups. If it does not, that is
information about how much the personalisation is worth.

### Two more greeting corrections

- `supplier.relations@` rendered "Hi Supplier". Compound desks arrive dotted, so
  `ROLE_LOCALPARTS` now carries the head token of the common ones.
- A first+last-initial mailbox — `danaw@` against a `ceo` of Dana Whitfield — rendered
  "Hi Danaw". That pattern was missing from `mailbox_matches_person`.

## agent_prompt.md

`agent_prompt.md` holds the user-authored OutreachFlow-Engine spec verbatim, plus a
reconciliation section listing where it diverges from this repo. Do not silently "implement"
that spec — five of its steps cannot execute as written (StealthScrape cannot extract
emails, no Hunter mailbox is connected, the credit budget covers 49 of 311 companies, its
50/day ceiling contradicts `DAILY_CAP = 25`, and auto-generated bodies from a 10-word
description are filler for 40 of 64 rows). Each is documented there with the check that
established it.

Note the spec describes a *fully autonomous* sender. The pipeline as built is deliberately
not that: dry-run by default, hand-written per-company prose, placeholder guard, 25/day cap.
Moving to autonomous dispatch is a deliberate decision, not a refactor.

## outreach_agent.py — the wired cascade

Implements agent_prompt.md against the real tools. Emits the spec's JSON log per company
and writes `outreach.csv`.

```powershell
$py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
$env:HUNTER_API_KEY = "<key from .env>"

& $py outreach_agent.py --limit 25                      # dry plan, spends nothing
& $py outreach_agent.py --limit 25 --run --out outreach.csv
& $py send_campaign.py outreach.csv --template template.txt --custom custom.csv
& $py send_campaign.py outreach.csv --template template.txt --custom custom.csv --send
```

**Scrape-first is the default, inverting the spec's order, and it matters.** The free plan
carries ~2x the verifications of domain-searches (100 vs 50). The contact-page scrape is
free and hits ~50%, so scrape → verify spends the plentiful credit and reserves the scarce
one. `--strategy hunter-first` restores the literal spec order.

**Hunter's verifier runs a real SMTP check server-side** — the probe this machine cannot do
because home ISPs block port 25. That is what makes `find_emails.py`'s SMTP path redundant
here: a `valid` verdict from the verifier is genuine verification, not a guess.

`Budget` mirrors the Hunter quota locally and refuses to overspend rather than discovering
the ceiling by collecting 403s. A 401/403/451 zeroes the local budget so the run degrades to
scrape-only instead of hammering a dead key. 429 → pause 60s → one retry, per the spec.

The credit is charged **after** the call and only when the request actually reached
Hunter — `_get()` returns `(data, charged)`. A `ConnectionError` (DNS, refused, connect
timeout) spends nothing server-side, so charging it locally made the mirror believe
credits were gone and dropped the run to scrape-only early. A `ReadTimeout` does charge:
Hunter may have processed it, and over-counting wastes one credit while under-counting
walks into the 403 the mirror exists to avoid.

Companies with no written prose in `custom.csv` are skipped with `SKIPPED_NO_PROSE` rather
than processed — spending a verification on a lead you cannot write a real email to is
waste. `--any-prose` overrides.

`send_campaign.py` gates on `verification_status == "valid"` when that column is present,
falling back to `SEND_STATES` for `find_emails.py` output.

### Measured, 5-company live run (2026-09-12)

4 of 5 verified valid, for 2 searches + 5 verifications:

Addresses are withheld here for the same reason they are not in the repo; the
shape of each result is what the table is for.

| company | strategy | what was found | verdict |
|---|---|---|---|
| Gradient Metrics | scrape | founder first name @ own domain | valid, 100 |
| ClickFlow | hunter | founder first name @ own domain | valid, 100 |
| Nomics | hunter | `contact@` role inbox | valid, 100 |
| Apitalks | scrape | `info@` role inbox | valid, 88 |
| Chronos Capital | scrape | `info@` role inbox | **invalid** — scrape found a dead address, verifier caught it |

That last row is the cascade working: the free scrape proposes, the cheap verifier disposes.

## Sending from personal Gmail (decided 2026-09-12)

`.env` is configured for `smtp.gmail.com:587` as a personal Gmail address. The earlier
"never send from your primary Gmail" note in `send_campaign.py`'s header was written for a
311-company bulk campaign; at 20 individually-written applications it does not apply, and a
real person's established Gmail is *more* credible to a spam filter than a new cold-outreach
domain. Keep the volume in that register — this reasoning does not survive scaling up.

`SMTP_PASS` must be a Google **App Password** (16 chars, requires 2-Step Verification).
Gmail has rejected account passwords over SMTP since 2022. Generate at
https://myaccount.google.com/apppasswords.

Gmail's own limit is ~500/day; `DAILY_CAP = 25` is the binding constraint.

### Enrichment run complete

`outreach.csv` holds 24 processed companies, **20 verified valid**, split 12 Hunter / 11
scrape / 1 skipped. Credits left after the run: 25 searches, 50 verifications.

Of the 20, **9 are named individuals** — a person's own first name or `first.last` at
their company's domain — and **11 are role inboxes** (`support@`, `info@`, `help@`,
`admin@`).
The role inboxes verify fine but land in ticket queues where a job application is closed
unread. Treat the 9 as the real list.

## Send-path hardening (2026-09-12)

Three defects found auditing the send path before first use:

1. **Single SMTP connection held across the whole run.** The 90-400s pacing sleep makes a
   20-email run ~78 minutes (median, modelled). Gmail drops idle SMTP sessions in ~10
   minutes, so the connection died mid-campaign *after* some mail had gone out. Now
   `smtp_connect()` opens a fresh connection per message and `send_one()` retries once on
   disconnect. Costs ~1s per message; removes the failure mode entirely.
2. **One unwritten row aborted the entire campaign.** `render()` raised `SystemExit`, so a
   missing hook at company 5 of 20 stranded companies 5-20. Now raises `PlaceholderError`,
   which `main()` catches to skip that single row; the run continues and reports
   `sent/skipped/failed`.
3. **No preflight on credentials.** A send run with `SMTP_PASS` unset failed at the first
   message. Now checked before the loop starts.

Also added: `Reply-To` (defaults to `SMTP_USER`), and a per-message pause log line.

### Test endpoint

`--test-to EMAIL` redirects every message to one inbox, prefixes the subject
`[TEST -> real@addr]`, and **never writes to the `sent` table** — so a test run cannot
consume a lead or mark a company as already-contacted. Pause drops to 5s in test mode.

```powershell
.\run.ps1 -Test -Cap 2          # two emails to SMTP_USER
.\run.ps1 -Test -TestTo other@example.com
```

The distinction that matters: `-Preview` renders to screen and sends nothing; `-Test`
actually transmits through Gmail to you, which is the only way to see real rendering,
threading, spam-folder placement and deliverability before touching a real lead.

## Code-quality pass (2026-09-16)

A read-only review of all six scripts (2,183 lines) produced 19 findings. All 19 are
applied. Nothing about the deliverability or politeness constraints was relaxed — the two
scraper fixes *tighten* budgets the code was quietly exceeding.

### The five that changed behaviour

1. **`scrape_contact.py` refetched robots.txt on every candidate path** — up to six extra
   requests per domain against companies the campaign is about to ask for a job, none of
   them counted against `max_pages`. Split into `_load_robots()` (once per domain) and
   `_robots_ok(rp, base, path)` (pure check).
2. **The CEO-name match was an unanchored substring**, so a short first name inside a role
   inbox scored 100. Replaced with token matching in `_name_score()`.
3. **`send_campaign.py` set `Message-ID` by hand but not `Date`.** RFC 5322 requires it and
   its absence is a cheap spam signal. Gmail's submission server backfills it, so this was
   not costing delivery today, but not every relay does.
4. **The `mailto:` +10 bonus was lost across pages** — `setdefault` is first-write-wins and
   `found` outlives the page loop.
5. **`outreach_agent.py` charged the Hunter credit before making the call**, so a connection
   failure shrank the local budget without anything being spent.

`research_jobs.py` had to follow (1): it imports `_robots_ok` from `scrape_contact.py`, so
the signature change would otherwise have broken it. Its `Fetcher` now caches the parser
rather than the first path's verdict, which is also more correct.

### The two that needed a judgement call

**`same_brand()` on compound TLDs.** The brand label was `parts[-2]`, which returns `co`
for every `.co.uk` domain and `com` for every `.com.au` — so two unrelated `.co.uk`
companies compared as the same brand, and a genuine `.co.uk` → `.com` rebrand compared as
different. The complete fix is the public-suffix list (`tldextract`), a new dependency.
Instead `TWO_PART_TLDS` holds the ~20 two-part suffixes a remote-startup list actually
contains, and `_brand_label()` steps back one label when it sees one. That is a stated
limit rather than a silent one: a company on a suffix outside that set still mislabels, and
the fix is to add the suffix or take the dependency.

**`Fetcher.get` caught bare `Exception`**, so a bug in this script and a dead company
looked identical in `research.sqlite`. Narrowing it to `requests.RequestException` is the
textbook fix, but `requests` raises plain `UnicodeError` on some malformed hostnames, and
an uncaught one mid-run strands a partially-cached campaign. So both are caught and kept
apart: a `RequestException` records the exception name as before, anything else records it
with a `bug:` prefix and prints a warning to stderr. The run finishes; the bug is legible
instead of being filed as evidence about a company.

### The cosmetics

None of these change behaviour: the no-op `max(args.limit * 4, args.limit)` became an
explicit `PROSE_SCAN_MULTIPLIER`; the redundant `accept_all`/`webmail`/`unknown` branch
collapsed into the catch-all it duplicated; `research_jobs.py`'s six summary blocks moved
out of `main()` into `print_summary()`; `already_sent()` became `sent_addresses()`, one
query instead of one per row; `same_site`/`same_brand` share `_bare_host()`; the response
body is sliced once; `ROLE_RE`/`TITLE_NOUN_RE` run `findall` once instead of `search` then
`findall`; `_clean(row.get("ceo_last"))` is hoisted to one `ceo_last`; `pool.submit` passes
keywords; `clean_list.py` builds both name columns in one `DataFrame` instead of a
`pd.Series` per row, and its `domain()` prints what it swallowed. The `find_emails.py` 429
comment said the call was "worth retrying" when the code deliberately does not retry
inline — the comment now says what the code does and why.

### Verification

33 behavioural checks, all passing. The ones worth naming: the token matcher still hits
`jsmith`, `koreyb`, `john.smith`, `johnsmith` and `smith`, and no longer hits `sales@` for
a CEO named Al or Sally; the mailto bonus survives either page order; robots fails open on
a missing file and closed on an explicit `Disallow`; the Hunter budget charges 0 on
refused/DNS/connect-timeout and 1 on read-timeout/200/403, with 403 still zeroing it;
`same_brand` still calls `pactly.ai` → `pactly.com` the same company and `pachama.com` →
`carbon-direct.com` not, while `alpha.co.uk` vs `beta.co.uk` is now correctly *not* a
match; `clean_list.py`'s new two-column build is row-for-row identical to the old one
across all 771 rows on pandas 3.0.5; and all 19 `valid` rows render and build with a
`Date`, text/plain, not multipart, no placeholder markers.

End-to-end, against the existing caches: `research_jobs.py --only custom.csv
--written-only` reproduces the documented summary exactly (42 live, 30 `no_careers_page`,
9 `no_relevant_roles`, 2 `ok`, 1 `not_hiring`; the same 4 drops, 3 moves and 1 blocked),
`send_campaign.py` dry-runs to `0 queued` because all 19 are already in the `sent` table,
and `outreach_agent.py --limit 3` plans 3 companies while skipping 18 dead and 19 already
emailed.
