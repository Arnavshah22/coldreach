#!/usr/bin/env python3
"""
scrape_contact.py — last-resort email finder: read the company's own contact page.

Used by find_emails.py when Hunter has nothing. This is deliberately the opposite of
a "stealth" scraper. It fetches at most ~6 pages per domain, one at a time, with a
real identifying User-Agent, and it obeys robots.txt. You are about to ask these
people for a job; do not hammer their website first.

An address found here is *published* — the company put it on their own site for
people to contact them. That makes it stronger evidence than any name-permutation
guess, and arguably stronger than a low-confidence Hunter hit.
"""
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests

UA = "Mozilla/5.0 (compatible; job-application-research/1.0; contact-page lookup)"

# Ordered by how likely the page is to carry a human-reachable address.
PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/team", "/"]

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Things that match the email regex but are not addresses: asset filenames
# (logo@2x.png), tracking vendors, and template placeholders left in the HTML.
ASSET_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js", ".ico")
NOISE_DOMAINS = (
    "sentry.io", "wixpress.com", "example.com", "example.org", "domain.com",
    "yourdomain.com", "email.com", "squarespace.com", "godaddy.com", "shopify.com",
    "wordpress.com", "sentry-next.wixpress.com", "test.com", "company.com",
)
NOISE_LOCAL = ("noreply", "no-reply", "donotreply", "postmaster", "abuse",
               "webmaster", "privacy", "legal", "dmca", "unsubscribe")

# A person beats a shared inbox; a shared inbox beats a support queue.
ROLE_RANK = {
    "founders": 55, "founder": 55, "ceo": 55, "jobs": 50, "careers": 50,
    "hello": 45, "hi": 45, "team": 40, "contact": 35, "info": 30,
    "sales": 15, "support": 10, "help": 10, "billing": 5,
}


def _load_robots(base, timeout):
    """Fetch and parse robots.txt once per domain, or None if there is nothing
    enforceable — a missing or broken file fails open.

    Fetched once and reused for every candidate path. robots.txt is site-wide, so
    refetching it per path doubled the real request count against a company's
    server, and none of those extra fetches counted against max_pages.
    """
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        resp = requests.get(urljoin(base, "/robots.txt"), timeout=timeout,
                            headers={"User-Agent": UA})
        if resp.status_code != 200:
            return None
        rp.parse(resp.text.splitlines())
    except Exception:
        return None
    return rp


def _robots_ok(rp, base, path):
    """Closed on an explicit Disallow, open on anything unparseable."""
    if rp is None:
        return True
    try:
        return rp.can_fetch(UA, urljoin(base, path))
    except Exception:
        return True


def _plausible(addr, domain):
    addr = addr.lower().rstrip(".")
    local, _, host = addr.partition("@")
    if not local or not host:
        return False
    if addr.endswith(ASSET_EXT) or host.endswith(ASSET_EXT):
        return False
    if any(host.endswith(n) for n in NOISE_DOMAINS):
        return False
    if any(local.startswith(n) for n in NOISE_LOCAL):
        return False
    if re.fullmatch(r"[0-9x@.]+", local):     # logo@2x and friends
        return False
    # Only keep addresses on the company's own domain (or a subdomain of it).
    root = domain.lower()
    return host == root or host.endswith("." + root) or root.endswith("." + host)


def _name_score(local, ceo_first, ceo_last):
    """100 for the CEO's first name, 90 for the last, 0 for no match.

    Matching is on name *tokens*, not substrings. A bare `first in local` scored
    a role inbox as a named human whenever the name was short enough to hide in
    one: "al" is inside "sales". Token matching plus the squashed patterns real
    mailboxes actually use (jsmith, john.smith, johnsmith, koreyb) keeps the true
    hits and drops the accidents. Mirrors mailbox_matches_person() in
    send_campaign.py, which had to learn the same lesson.
    """
    first = (ceo_first or "").lower()
    last = (ceo_last or "").lower()
    tokens = [t for t in re.split(r"[^a-z]+", local) if t]
    squashed = re.sub(r"[^a-z]", "", local)
    combos = ({first + last, last + first, first[0] + last, first + last[0]}
              if first and last else set())
    if len(first) > 2 and (first in tokens or squashed == first or squashed in combos):
        return 100
    if len(last) > 2 and (last in tokens or squashed in combos):
        return 90
    return 0


def _score(addr, ceo_first, ceo_last):
    """Higher is better. A named human is the goal; a role inbox is the fallback."""
    local = addr.split("@", 1)[0].lower()
    named = _name_score(local, ceo_first, ceo_last)
    if named:
        return named
    for role, pts in ROLE_RANK.items():
        if local == role or local.startswith(role):
            return pts
    # Unrecognised and not a role word — probably a real person's name.
    return 60 if "." in local or len(local) > 3 else 20


def _harvest(html, domain, ceo_first, ceo_last):
    """Yield (address, score) for every plausible address on one page.

    A mailto: link is intentional publication, so it carries +10 over the same
    address found loose in body text.
    """
    for m in re.findall(r'mailto:([^"\'>?\s]+)', html, flags=re.I):
        addr = m.strip().lower().rstrip(".")
        if _plausible(addr, domain):
            yield addr, _score(addr, ceo_first, ceo_last) + 10
    for m in EMAIL_RE.findall(html):
        addr = m.strip().lower().rstrip(".")
        if _plausible(addr, domain):
            yield addr, _score(addr, ceo_first, ceo_last)


def scrape_emails(domain, ceo_first=None, ceo_last=None, timeout=10, delay=1.5,
                  respect_robots=True, max_pages=6):
    """Return [(email, score, source_url)] best-first. Never raises."""
    base = f"https://{domain}"
    found = {}
    fetched = 0
    robots = _load_robots(base, timeout) if respect_robots else None

    for path in PATHS:
        if fetched >= max_pages:
            break
        if respect_robots and not _robots_ok(robots, base, path):
            continue
        url = urljoin(base, path)
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": UA},
                                allow_redirects=True)
            fetched += 1
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                continue
            html = resp.text[:400_000]
        except Exception:
            fetched += 1
            continue
        finally:
            time.sleep(delay)          # one page at a time, never a burst

        # Keep the best score seen for an address across *all* pages. `found`
        # outlives the loop body, so first-write-wins lost the mailto: bonus
        # whenever an address appeared as body text on an earlier page and as a
        # mailto: link on a later one.
        for addr, score in _harvest(html, domain, ceo_first, ceo_last):
            if score > found.get(addr, (-1, None))[0]:
                found[addr] = (score, url)

        if any(s >= 90 for s, _ in found.values()):
            break                      # got a named human, stop bothering the site

    return sorted(((a, s, u) for a, (s, u) in found.items()),
                  key=lambda t: t[1], reverse=True)


if __name__ == "__main__":
    import sys
    for addr, score, src in scrape_emails(sys.argv[1],
                                          sys.argv[2] if len(sys.argv) > 2 else None):
        print(f"{score:>4}  {addr:<40} {src}")
