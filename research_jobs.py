#!/usr/bin/env python3
"""
research_jobs.py — read each company's own careers page and extract what the outreach
actually needs: whether a relevant role is open, whether they can hire someone outside
their own country, and the exact words they use for their requirements.

Research only. Nothing here sends mail or spends a Hunter credit. Writes research.csv
keyed on `domain`, which joins onto custom.csv the same way custom.csv joins onto
outreach.csv, so re-running never touches hand-written prose.

Why this exists
---------------
The `description` column in startups_clean.csv was capped when the list was built —
median 7 words across the 311 eligible rows, 64 of them 4 words or fewer. That is the
reason 40 of the 64 scaffolded rows in custom.csv are still blank: you cannot write a
real hook from "We make things happen". A careers page is several hundred words of a
company describing its own engineering problems in its own language, which is exactly
the raw material a hook is supposed to mirror.

It also settles two things the pipeline currently guesses at:

  * `role`. All 64 rows in custom.csv say "Backend / AI Engineer" because that value
    was scaffolded, not sourced. A real posted title belongs in the subject line.
  * whether they can hire you at all. 195 of the 311 eligible rows are USA-based and
    you are in India. A page that says "remote (US only)" is a company worth not
    spending a Hunter credit on.

The `jobpage` column is a *candidate*, not an answer
----------------------------------------------------
Sampling it: `demio.com/compare-demio` is a marketing page, `https://www.clerky.com`
is a homepage, `remoteok.io/remote-startups/graphenedb` is a third-party aggregator,
`sensiblecode.io` is a bare domain and `import2.com`'s cell reads "angel list". So the
column is tried first, then standard careers paths on the domain, and every fetched
page must still look like a careers page before it is believed.

Politeness budget — same as scrape_contact.py's, for the same reason
--------------------------------------------------------------------
These are companies you are about to ask for a job.
  * at most --max-pages (default 5) fetches per domain, robots.txt included
  * one request at a time, --delay (default 1.5s) between them
  * robots.txt honoured, failing open only when missing or unparseable
  * identifying User-Agent, no proxies, no retries, early exit once roles are found
  * results cached in research.sqlite, so a re-run costs nobody anything

Usage
-----
    $py research_jobs.py startups_clean.csv --size 1-10 --limit 20 --out research.csv
    $py research_jobs.py startups_clean.csv --domains baremetrics.com,conveyal.com
    $py research_jobs.py startups_clean.csv --only custom.csv --written-only
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests

from find_emails import TIERS, _clean, size_tier
from scrape_contact import UA, _load_robots, _robots_ok

DB = "research.sqlite"

# Standard careers paths, ordered by how often they are the real one.
CAREERS_PATHS = ["/careers", "/jobs", "/careers/", "/jobs/", "/join-us",
                 "/company/careers", "/about/careers", "/work-with-us", "/hiring"]

# Job boards that are not the company's own words. A posting here is still a hiring
# signal, but it cannot be quoted back at them and it is usually stale — clean_list.py
# already penalises angel.co and stackoverflow for exactly that reason.
AGGREGATORS = ("remoteok", "angel.co", "angellist", "wellfound", "indeed.",
               "linkedin.", "stackoverflow", "glassdoor", "ziprecruiter", "monster.",
               "dice.com", "builtin", "crunchbase", "f6s.", "themuse", "jobvite")

# Applicant tracking systems with a public JSON endpoint. Only 8 of the 144 eligible
# rows sit on one, so this is a convenience, not the main path.
ATS_JSON = {
    "lever.co":    "https://api.lever.co/v0/postings/{slug}?mode=json",
    "recruitee.com": "https://{slug}.recruitee.com/api/offers/",
    "workable.com": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
}

# ---------- what counts as a role worth applying for ----------
# Word-boundaried on purpose: a bare "ai" matches "email", "chair" and "detail".
ROLE_RE = re.compile(r"""(
      back[\s\-]?end | \bfull[\s\-]?stack
    | \bsoftware\s+(engineer|developer) | \bplatform\s+engineer
    | \binfrastructure\s+engineer | \bsite\s+reliability | \bdevops\b
    | \bpython\b | \bgolang\b | \bnode\.?js\b | \btypescript\b
    | \bml\s+engineer | \bmachine\s+learning | \bdata\s+engineer
    | \bai\s*/?\s*ml\b | \bai\s+engineer | \bapplied\s+(ai|ml|scientist)
    | \bllm\b | \bagentic\b | \bapi\s+engineer | \bbackend\b
    | \b(senior|staff|lead|principal)\s+(engineer|developer)
)""", re.I | re.X)

# ROLE_RE alone matches any prose that mentions a technology — "You have 4+ years of
# experience with Python and PostgreSQL" is not a job title. A title also names the job
# itself, so require both. Precision matters more than recall here: `posted_title` ends
# up in a subject line.
# Deliberately excludes "devops" and other words that ROLE_RE already matches: if one
# word satisfies both checks the pair stops being two checks. Skycrapers' product
# "DevOps-as-a-Service" and a nav link reading "Azure DevOps" both came through as job
# titles until this was split. A listing says what the *job* is, not just its subject.
TITLE_NOUN_RE = re.compile(r"""\b(
      engineer(ing)? | developer | programmer | scientist | architect
    | analyst | specialist | consultant
    | (tech(nical)?\s+)?lead | head\s+of | manager | director | intern(ship)?
)\b""", re.I | re.X)

# Links a site uses to point at its own careers page. Following these beats guessing
# paths: the company already knows where the page is, and it costs one fetch to ask.
CAREERS_LINK_RE = re.compile(r"(careers?|jobs?|hiring|join[\-_\s]?us|work[\-_\s]?with[\-_\s]?us|"
                             r"open[\-_\s]?roles?|vacanc|employment|we[\-'\s]?re[\-\s]hiring)",
                             re.I)

# A page that is actually about hiring says at least one of these.
CAREERS_CUES = ("open position", "open role", "current opening", "job opening",
                "we're hiring", "we are hiring", "join our team", "join the team",
                "apply now", "view job", "see open", "career", "vacanc",
                "no open position", "no current opening", "work with us")

# ---------- can they hire someone in India? ----------
ANYWHERE_RE = re.compile(r"""(
      work\s+from\s+anywhere | anywhere\s+in\s+the\s+world | fully\s+remote
    | 100%\s+remote | remote[\s\-]first | globally\s+distributed
    | fully\s+distributed | any\s+time\s?zone | worldwide | remote,?\s+anywhere
)""", re.I | re.X)

REGION_RE = re.compile(r"""(
      must\s+(be\s+)?(based|located|reside|living|live)\s+in
    | (authori[sz]ed|eligible|permitted|legally\s+able)\s+to\s+work\s+in
    | work\s+authori[sz]ation | visa\s+sponsorship\s+is\s+not
    | (do\s+not|does\s+not|don't|cannot|can't|unable\s+to|no)\s+(\w+\s+){0,2}sponsor
    | (europe|canada|australia|united\s+states|germany|netherlands)[\s\-]?only
    | only\s+(hiring|considering|accept\w*)\s+(candidates\s+)?(who|in|from|based)
    | overlap\s+with\s+(our\s+)?(team|core\s+hours|pacific|eastern)
    | requires?\s+(a\s+)?(work\s+)?(permit|visa)
)""", re.I | re.X)

# Case-SENSITIVE on purpose. Two-letter country codes are also ordinary English words,
# and under re.I "join us only to find out" on Gradient Metrics' homepage was read as a
# location requirement. "US only" is a restriction; "us only" is a pronoun.
REGION_ABBR_RE = re.compile(r"""(
      \b(US|U\.S\.|USA|UK|EU|EEA|EMEA|APAC|LATAM)\b[\s\-]*only\b
    | \bbased\s+in\s+the\s+(US|U\.S\.|USA|UK|EU)\b
    | \bwithin\s+(the\s+)?(US|USA|UK|EU|EEA)\b
    | \b(CET|EST|PST|PDT|GMT|UTC)\s*[+\-]?\d*\s*(time\s?zone|hours?)\b
    | \b[Rr]emote\s*[\(\-–,]\s*(US|USA|UK|EU|EMEA|Europe|Americas)\b
)""", re.X)

ONSITE_RE = re.compile(r"(on[\s\-]?site|in[\s\-]our[\s\-]office|in\s+person|"
                       r"hybrid|\d+\s+days?\s+(a|per)\s+week\s+in)", re.I)

# ---------- requirement language worth mirroring ----------
REQ_CUE_RE = re.compile(r"""(
      you(\s+\w+){0,2}\s+(have|bring|know|are|will|'ll|ll\b)
    | experience\s+(with|in|building|working)
    | \d+\+?\s+years? | we(\s+\w+){0,2}\s+(looking\s+for|want|need)
    | (strong|solid|deep|proven)\s+(experience|background|knowledge|understanding)
    | proficien | familiar\s+with | comfortable\s+with | must\s+have
    | bonus\s+points | nice\s+to\s+have | you\s+should
)""", re.I | re.X)

STACK_RE = re.compile(r"""\b(
      python|django|flask|fastapi|node\.?js|typescript|javascript|golang|go\b|rust
    | java\b|kotlin|scala|ruby|rails|elixir|php|c\+\+
    | postgres(ql)?|mysql|mongodb|redis|elasticsearch|clickhouse|snowflake|bigquery
    | kafka|rabbitmq|celery|airflow|dbt|spark|neo4j|qdrant|pinecone|weaviate
    | aws|gcp|azure|kubernetes|k8s|docker|terraform|ansible
    | pytorch|tensorflow|scikit|langchain|langgraph|llm|openai|anthropic
    | graphql|grpc|rest\s+api|websocket|react|next\.?js|vue
)\b""", re.I | re.X)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- cache ----------
def db_init():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS research (
        domain TEXT PRIMARY KEY, payload TEXT, checked_at TEXT)""")
    con.commit()
    return con


def db_get(con, domain):
    row = con.execute("SELECT payload FROM research WHERE domain=?", (domain,)).fetchone()
    return json.loads(row[0]) if row else None


def db_put(con, domain, payload):
    con.execute("INSERT OR REPLACE INTO research VALUES (?,?,?)",
                (domain, json.dumps(payload), now()))
    con.commit()


# ---------- html ----------
def to_text(html):
    """Tag-strip to readable lines. No bs4 — the repo's deps stay as they are."""
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<(br|hr)\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|td|section|article)>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = unescape(html)
    html = re.sub(r"[ \t\xa0]+", " ", html)
    lines = [ln.strip() for ln in html.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def links(html, base):
    """[(anchor_text, absolute_url)] — used to find role titles that are links."""
    out = []
    for m in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        text = re.sub(r"\s+", " ", unescape(text)).strip()
        if text and not href.lower().startswith(("mailto:", "javascript:", "#")):
            out.append((text[:140], urljoin(base, href)))
    return out


def is_careers_page(text):
    low = text.lower()
    return any(cue in low for cue in CAREERS_CUES)


# Two-part public suffixes common enough to matter here. Without them
# _brand_label() returns "co" for every .co.uk domain and "com" for every
# .com.au, so two unrelated companies on the same TLD compare as the same brand.
# The complete answer is the public-suffix list (tldextract); this covers what a
# remote-startup list actually contains without taking the dependency.
TWO_PART_TLDS = {
    "co.uk", "org.uk", "me.uk", "ac.uk", "co.nz", "co.za", "co.il", "co.in",
    "co.jp", "co.kr", "com.au", "net.au", "org.au", "com.br", "com.mx",
    "com.sg", "com.tr", "com.ar", "com.cn", "com.hk",
}


def _bare_host(u):
    """netloc (or a bare host), lowercased, port and leading www. stripped."""
    h = (urlparse(u).netloc or u).lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _brand_label(h):
    """The company-identifying label: pactly.ai -> pactly, bbc.co.uk -> bbc."""
    parts = [p for p in _bare_host(h).split(".") if p]
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_PART_TLDS:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def same_site(a, b):
    """Is `a` still the same company's site as `b`? www and subdomains don't count
    as a move; a different registrable domain does."""
    a, b = _bare_host(a), _bare_host(b)
    return bool(a) and bool(b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def same_brand(domain, final_url):
    """pactly.ai -> pactly.com is the same company changing TLD; pachama.com ->
    carbon-direct.com is an acquisition. Both are redirects and they mean opposite
    things for whether to keep the lead, so compare the brand label, not the host."""
    a, b = _brand_label(domain), _brand_label(final_url)
    if not a or not b:
        return False
    # Tolerate dropped hyphens/suffixes: "rare-technologies" vs "raretech".
    na, nb = a.replace("-", ""), b.replace("-", "")
    return na == nb or na.startswith(nb) or nb.startswith(na)


def page_title(html):
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", m.group(1)))).strip()[:120] if m else ""


def careers_links(html, base, domain):
    """Careers URLs the homepage itself points at, on the company's own site."""
    out = []
    for text, url in links(html, base):
        if not same_site(url, domain):
            continue
        path = urlparse(url).path or "/"
        if path in ("/", "") or url in out:
            continue
        if CAREERS_LINK_RE.search(path) or CAREERS_LINK_RE.search(text):
            out.append(url)
    return out[:3]


# ---------- extraction ----------
def find_roles(text, anchors):
    """-> [(title, url_or_empty)] deduped, best-first.

    A job title is short. Anchor text is the strongest source because a listing is
    almost always a link; standalone short lines catch the boards that render titles
    as headings.
    """
    seen, roles = set(), []

    def add(title, url=""):
        title = re.sub(r"\s+", " ", title).strip(" -–—|·•\t")
        if not (3 < len(title) <= 90):
            return
        role_hits, noun_hits = ROLE_RE.findall(title), TITLE_NOUN_RE.findall(title)
        if not (role_hits and noun_hits):
            return
        if title.endswith((".", ":", "!")) or title.lower().startswith("you "):
            return                                   # a sentence, not a listing
        if len(role_hits) > 1 or len(noun_hits) > 1:
            # Several listings run together — inline <a> tags all on one line of
            # markup collapse into one text line. A real title names one job once.
            return
        key = re.sub(r"[^a-z0-9]", "", title.lower())
        if key and key not in seen:
            seen.add(key)
            roles.append((title, url))

    for text_, url in anchors:
        add(text_, url)
    for line in text.split("\n"):
        if len(line) <= 90:
            add(line)
    return roles


def classify_remote(text):
    """-> (policy, evidence). A stated restriction beats a 'remote' claim.

    "Remote (US only)" contains the word remote and is still a no for someone in
    India, so the region check runs first and wins ties.
    """
    region = REGION_RE.search(text) or REGION_ABBR_RE.search(text)
    anywhere = ANYWHERE_RE.search(text)
    onsite = ONSITE_RE.search(text)
    if region:
        return "region_limited", _snippet(text, region)
    if anywhere:
        return "anywhere", _snippet(text, anywhere)
    if onsite:
        return "onsite", _snippet(text, onsite)
    if re.search(r"\bremote\b", text, re.I):
        return "remote_unqualified", _snippet(text, re.search(r"\bremote\b", text, re.I))
    return "unstated", ""


def _snippet(text, match, width=160):
    a = max(0, match.start() - width // 2)
    return re.sub(r"\s+", " ", text[a:a + width]).strip()


def find_requirements(text, limit=4):
    """The sentences a hook should mirror, in their words."""
    out, seen = [], set()
    chunks = re.split(r"(?<=[.!?])\s+|\n", text)
    for c in chunks:
        c = re.sub(r"\s+", " ", c).strip(" -–—|·•*\t")
        if not (40 <= len(c) <= 260) or not REQ_CUE_RE.search(c):
            continue
        key = c.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def find_stack(text, limit=12):
    hits, seen = [], set()
    for m in STACK_RE.finditer(text):
        t = m.group(0).lower()
        if t not in seen:
            seen.add(t)
            hits.append(t)
        if len(hits) >= limit:
            break
    return hits


# ---------- fetching ----------
class Fetcher:
    """Serial, budgeted, robots-aware. Counts every request against max_pages."""

    def __init__(self, delay=1.5, timeout=12, max_pages=5, respect_robots=True):
        self.delay, self.timeout = delay, timeout
        self.max_pages, self.respect_robots = max_pages, respect_robots
        self.used = 0
        self.blocked = False
        # One robots.txt fetch per host, cached as a parser rather than as a
        # verdict: five candidate paths on one domain would otherwise mean five
        # identical requests for the same file, and caching the first path's
        # answer would apply it to paths it was never asked about.
        self._robots = {}
        # A caller needs to tell "the server said no" from "there was no server".
        self.last_status = None          # HTTP code, or None for a transport failure
        self.last_url = ""               # final URL after redirects, even on an error
        self.last_error = ""

    @property
    def spent(self):
        return self.used >= self.max_pages

    def get(self, url):
        if self.spent:
            return None
        parsed = urlparse(url)
        base = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        if self.respect_robots:
            if base not in self._robots:
                self._robots[base] = _load_robots(base, self.timeout)
            if not _robots_ok(self._robots[base], base, parsed.path or "/"):
                self.blocked = True
                return None
        self.last_status, self.last_url, self.last_error = None, "", ""
        try:
            r = requests.get(url, timeout=self.timeout, allow_redirects=True,
                             headers={"User-Agent": UA,
                                      "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
            self.used += 1
            self.last_status, self.last_url = r.status_code, r.url
            if r.status_code != 200:
                return None
            return r
        except requests.RequestException as e:
            self.used += 1
            self.last_error = type(e).__name__
            return None
        except Exception as e:
            # Anything that is not a transport failure is a bug in this script, not
            # evidence about the company. Swallowing it silently made the two look
            # identical in research.sqlite; say so loudly and keep the run going,
            # because aborting halfway strands a partially-cached campaign.
            self.used += 1
            self.last_error = "bug:" + type(e).__name__
            print(f"   !! {type(e).__name__} fetching {url} — not a dead domain, "
                  f"a bug worth reading", file=sys.stderr)
            return None
        finally:
            time.sleep(self.delay)      # one page at a time, never a burst


def ats_slug(url):
    """-> (api_url, host_key) when the jobpage is on a known ATS."""
    host = (urlparse(url).netloc or "").lower()
    for key, tmpl in ATS_JSON.items():
        if not host.endswith(key) and key not in host:
            continue
        if key == "lever.co":
            parts = [p for p in urlparse(url).path.split("/") if p]
            slug = parts[0] if parts else ""
        else:                                     # <slug>.workable.com / .recruitee.com
            slug = host.split(".")[0]
        if slug:
            return tmpl.format(slug=slug), key
    return None, None


def from_ats(payload, key):
    """Normalise the three ATS shapes into [(title, url, location, text)]."""
    rows = []
    if key == "lever.co" and isinstance(payload, list):
        for j in payload:
            rows.append((j.get("text", ""), j.get("hostedUrl", ""),
                         (j.get("categories") or {}).get("location", ""),
                         j.get("descriptionPlain", "")))
    elif key == "recruitee.com":
        for j in (payload or {}).get("offers", []):
            rows.append((j.get("title", ""), j.get("careers_url", ""),
                         j.get("location", ""), j.get("description", "")))
    elif key == "workable.com":
        for j in (payload or {}).get("jobs", []):
            rows.append((j.get("title", ""), j.get("url", ""),
                         j.get("location", "") or j.get("city", ""),
                         j.get("description", "")))
    return [r for r in rows if r[0]]


def candidate_urls(domain, jobpage):
    """jobpage first if it is usable, then the standard paths on their own domain."""
    urls, notes = [], []
    jp = _clean(jobpage)
    if jp:
        if not re.match(r"^https?://", jp):
            jp = "https://" + jp if "." in jp and " " not in jp else ""
        if not jp:
            notes.append("jobpage cell is not a URL")
        elif any(a in jp.lower() for a in AGGREGATORS):
            notes.append(f"jobpage is a third-party board ({urlparse(jp).netloc})")
            urls.append(jp)                    # still worth one look for a hiring signal
        else:
            urls.append(jp)
    base = f"https://{domain}"
    for p in CAREERS_PATHS:
        u = urljoin(base, p)
        if u not in urls:
            urls.append(u)
    return urls, notes


def check_site(domain, fetcher):
    """Is this still a live company at this domain? -> (site_status, info).

    Runs before anything else, because the answer changes what the rest means. This
    list is a Remotive export several years old and a material share of it has rotted:
    pachama.com 301s to carbon-direct.com (acquired), baremetrics.com/jobs redirects
    to the homepage, datacite.org 403s our user-agent. Reporting any of those as "no
    careers page" would be wrong in a way that costs a Hunter credit and a hand-written
    hook. One fetch settles it, and a dead domain then costs nothing further.
    """
    info = {"redirect_to": "", "homepage_title": "", "homepage_html": "",
            "homepage_url": ""}
    r = fetcher.get(f"https://{domain}/")

    # Where it ended up matters even when the response was an error: sensiblecode.io
    # 403s *and* redirects to cantabular.com, and the rebrand is the more useful fact.
    final = (r.url if r is not None else fetcher.last_url) or ""
    if final and not same_site(final, domain):
        info["redirect_to"] = (urlparse(final).netloc or "").lower()
        return ("moved_domain" if same_brand(domain, final) else "redirected_offsite"), info

    if r is None:
        if fetcher.blocked:
            return "blocked", info
        code = fetcher.last_status
        if code in (401, 403, 405, 406, 429) or (code and 500 <= code < 600):
            # The server answered, it just refused us. That is not evidence the
            # company is gone — datacite.org 403s our user-agent and is very much
            # alive. Never let this become a reason to skip a lead.
            return "blocked", info
        if code == 404:
            return "dead", info
        return "unreachable", info                  # DNS, TLS or connection failure

    info["homepage_url"] = r.url
    if "html" not in r.headers.get("content-type", "").lower():
        return "not_html", info
    info["homepage_html"] = r.text[:600_000]
    info["homepage_title"] = page_title(info["homepage_html"])
    # A parked or expired domain serves 200s full of registrar boilerplate.
    low = to_text(info["homepage_html"])[:2000].lower()
    if any(p in low for p in ("this domain is for sale", "buy this domain",
                              "domain is parked", "godaddy.com/domainsearch",
                              "the domain name you", "renew this domain")):
        return "parked", info
    return "live", info


def research_one(domain, company, jobpage, fetcher):
    """Never raises. -> dict of research.csv fields."""
    out = {"domain": domain, "company": company, "site_status": "", "redirect_to": "",
           "homepage_title": "", "careers_url": "",
           "page_status": "no_careers_page", "roles_found": 0, "relevant_roles": "",
           "role_urls": "", "posted_title": "", "remote_policy": "unstated",
           "remote_evidence": "", "requirements": "", "tech_mentions": "",
           "pages_fetched": 0, "notes": "", "checked_at": now()}

    site_status, info = check_site(domain, fetcher)
    out["site_status"] = site_status
    out["redirect_to"] = info["redirect_to"]
    out["homepage_title"] = info["homepage_title"]
    if site_status != "live":
        # Don't spend four more requests on a domain that isn't the company any more.
        out["page_status"] = "site_" + site_status
        out["pages_fetched"] = fetcher.used
        out["notes"] = {
            "redirected_offsite": f"now serves {info['redirect_to']} — acquired, merged "
                                  f"or rebranded; the company in the list is gone",
            "moved_domain": f"same company, moved to {info['redirect_to']} — update the "
                            f"domain in the source list and re-run",
            "parked": "domain is parked",
            "blocked": "server refused our user-agent (still alive — judge nothing "
                       "from this)",
            "dead": "homepage 404s",
            "unreachable": "DNS/TLS/connection failure",
            "not_html": "domain does not serve a web page",
        }.get(site_status, "")
        return out

    urls, notes = candidate_urls(domain, jobpage)
    # The homepage already knows where its careers page is — ask it before guessing.
    discovered = careers_links(info["homepage_html"], info["homepage_url"], domain)
    if discovered:
        notes.append(f"{len(discovered)} careers link(s) on the homepage")
        urls = discovered + [u for u in urls if u not in discovered]
    else:
        notes.append("no careers link on the homepage")

    # A known ATS answers cleanly in one JSON request; don't crawl the HTML for it.
    for u in urls:
        api, key = ats_slug(u)
        if not api:
            continue
        r = fetcher.get(api)
        if r is not None:
            try:
                rows = from_ats(r.json(), key)
            except Exception:
                rows = []
            if rows:
                rel = [(t, url) for t, url, _, _ in rows if ROLE_RE.search(t)]
                blob = "\n".join(f"{t} {loc}\n{desc}" for t, _, loc, desc in rows)
                notes.append(f"{key} API, {len(rows)} postings")
                out.update(_pack(u, rel, blob, notes, fetcher))
                out["page_status"] = "ok" if rel else "no_relevant_roles"
                return out

    for url in urls:
        if fetcher.spent:
            notes.append("page budget spent")
            break
        r = fetcher.get(url)
        if r is None:
            continue
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype.lower():
            continue
        html = r.text[:600_000]
        text = to_text(html)
        if not is_careers_page(text):
            continue
        anchors = links(html, r.url)
        roles = find_roles(text, anchors)
        out.update(_pack(r.url, roles, text, notes, fetcher))
        if roles:
            out["page_status"] = "ok"
            return out
        # A real careers page saying nothing relevant is still an answer.
        out["page_status"] = "no_relevant_roles"
        low = text.lower()
        if any(c in low for c in ("no open position", "no current opening",
                                  "not hiring", "no vacanc")):
            out["page_status"] = "not_hiring"
            return out

    if out["page_status"] == "no_careers_page":
        if fetcher.blocked:
            out["page_status"] = "blocked_by_robots"
        # The site is live and we just couldn't find a careers page — say so, and use
        # the homepage for the remote/stack signal rather than returning nothing. A
        # company with no careers page is still a company you can write to.
        home = to_text(info["homepage_html"])
        policy, evidence = classify_remote(home)
        out["remote_policy"], out["remote_evidence"] = policy, evidence
        out["tech_mentions"] = ", ".join(find_stack(home))
        if re.search(r"\b(we[\s']re hiring|join (our|the) team|open roles?)\b", home, re.I):
            notes.append("homepage mentions hiring but no page found")
    out["notes"] = "; ".join(notes)
    out["pages_fetched"] = fetcher.used
    return out


def _pack(url, roles, text, notes, fetcher):
    policy, evidence = classify_remote(text)
    return {
        "careers_url": url,
        "roles_found": len(roles),
        "relevant_roles": " | ".join(t for t, _ in roles[:6]),
        "role_urls": " | ".join(u for _, u in roles[:6] if u),
        "posted_title": roles[0][0] if roles else "",
        "remote_policy": policy,
        "remote_evidence": evidence,
        "requirements": " | ".join(find_requirements(text)),
        "tech_mentions": ", ".join(find_stack(text)),
        "pages_fetched": fetcher.used,
        "notes": "; ".join(notes),
    }


# ---------- queue ----------
def build_queue(df, args):
    df = df.copy()
    df["score"] = pd.to_numeric(df.get("score"), errors="coerce").fillna(0)
    if args.domains:
        want = {d.strip().lower() for d in args.domains.split(",") if d.strip()}
        df = df[df.domain.str.lower().isin(want)]
    else:
        df = df[df.score >= args.min_score]
        if args.only:
            only = pd.read_csv(args.only)
            keep = only[only.hook.notna()] if args.written_only else only
            df = df[df.domain.isin(set(keep.domain))]
        tiers = df["size"].map(size_tier)
        df["_rank"] = [t[0] for t in tiers]
        df["_tier"] = [t[1] for t in tiers]
        if args.size:
            wanted = {s.strip() for s in args.size.split(",")}
            bad = wanted - {label for _, label in TIERS} - {"unknown"}
            if bad:
                raise SystemExit(f"unknown size tier(s): {sorted(bad)}")
            df = df[df._tier.isin(wanted)]
        df = df.sort_values(["_rank", "score"], ascending=[True, False])
    return df.head(args.limit) if args.limit else df


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("infile", nargs="?", default="startups_clean.csv")
    ap.add_argument("--out", default="research.csv")
    ap.add_argument("--limit", type=int, default=0, help="0 = no limit")
    ap.add_argument("--min-score", type=int, default=4)
    ap.add_argument("--size", default=None, help="comma-separated tiers, e.g. 1-10,11-50")
    ap.add_argument("--domains", default=None, help="comma-separated domains, ignores filters")
    ap.add_argument("--only", default=None, metavar="CSV",
                    help="restrict to domains present in this CSV (e.g. custom.csv)")
    ap.add_argument("--written-only", action="store_true",
                    help="with --only, just the rows that already have a hook")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--timeout", type=float, default=12)
    ap.add_argument("--max-pages", type=int, default=5, help="fetches per domain")
    ap.add_argument("--ignore-robots", action="store_true",
                    help="don't. present only so the default is a choice.")
    ap.add_argument("--refresh", action="store_true", help="ignore the cache")
    args = ap.parse_args()

    df = pd.read_csv(args.infile)
    queue = build_queue(df, args)
    if queue.empty:
        raise SystemExit("nothing matched those filters")

    con = db_init()
    rows, fresh, cached = [], 0, 0
    print(f"{len(queue)} companies queued, <={args.max_pages} pages each, "
          f"{args.delay}s apart\n", file=sys.stderr)

    for i, r in enumerate(queue.to_dict("records"), 1):
        domain, company = _clean(r.get("domain")), _clean(r.get("company"))
        if not domain:
            continue
        hit = None if args.refresh else db_get(con, domain)
        if hit:
            rows.append(hit)
            cached += 1
            print(f"[{i}/{len(queue)}] {company:<26} cached  "
                  f"{hit.get('page_status')}", file=sys.stderr)
            continue
        f = Fetcher(args.delay, args.timeout, args.max_pages, not args.ignore_robots)
        res = research_one(domain, company, r.get("jobpage"), f)
        db_put(con, domain, res)
        rows.append(res)
        fresh += 1
        tail = (f"-> {res['redirect_to']}" if res["redirect_to"]
                else f"roles={res['roles_found']} remote={res['remote_policy']}")
        print(f"[{i}/{len(queue)}] {company:<26} {res['site_status']:<18} "
              f"{res['page_status']:<20} {tail}", file=sys.stderr)

    out = pd.DataFrame(rows)
    out.to_csv(args.out, index=False, encoding="utf-8")
    print_summary(out, fresh, cached, args.out)


def print_summary(out, fresh, cached, out_path):
    """Everything the run wants to say once the CSV is written."""
    print(f"\nwrote {out_path}: {len(out)} rows ({fresh} fetched, {cached} cached)\n",
          file=sys.stderr)

    # Liveness first — it decides whether the rest of the row means anything.
    live = out[out.site_status == "live"]
    print("site:  " + ", ".join(f"{k}={v}" for k, v in out.site_status.value_counts().items()),
          file=sys.stderr)
    gone = out[out.site_status.isin(["redirected_offsite", "parked", "unreachable", "dead"])]
    if not gone.empty:
        print(f"\nDROP ({len(gone)}) — not a company at that domain any more. Don't spend "
              f"a Hunter credit or a hand-written hook here:", file=sys.stderr)
        for _, r in gone.iterrows():
            print(f"   {r['company']:<26} {r['domain']:<24} {r['site_status']}"
                  + (f" -> {r['redirect_to']}" if r["redirect_to"] else ""), file=sys.stderr)

    moved = out[out.site_status == "moved_domain"]
    if not moved.empty:
        print(f"\nFIX ({len(moved)}) — same company, new domain. Still a good lead; update "
              f"the source list and re-run:", file=sys.stderr)
        for _, r in moved.iterrows():
            print(f"   {r['company']:<26} {r['domain']:<24} -> {r['redirect_to']}",
                  file=sys.stderr)

    stopped = out[out.site_status == "blocked"]
    if not stopped.empty:
        print(f"\nUNKNOWN ({len(stopped)}) — the server refused us. Alive, just not "
              f"readable here; check by hand rather than dropping:", file=sys.stderr)
        for _, r in stopped.iterrows():
            print(f"   {r['company']:<26} {r['domain']}", file=sys.stderr)

    print(f"\nof the {len(live)} live: "
          + ", ".join(f"{k}={v}" for k, v in live.page_status.value_counts().items()),
          file=sys.stderr)
    hiring = live[live.roles_found > 0]
    print(f"{len(hiring)} have a relevant open role "
          f"({int((hiring.remote_policy == 'anywhere').sum())} remote-anywhere, "
          f"{int((hiring.remote_policy == 'region_limited').sum())} region-locked)",
          file=sys.stderr)
    blocked = out[out.remote_policy == "region_limited"]
    if not blocked.empty:
        print(f"\n{len(blocked)} state a location requirement you may not meet:",
              file=sys.stderr)
        for _, r in blocked.iterrows():
            print(f"   {r['company']:<26} {r['remote_evidence'][:90]}", file=sys.stderr)


if __name__ == "__main__":
    main()
