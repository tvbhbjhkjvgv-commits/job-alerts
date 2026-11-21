#!/usr/bin/env python3
"""Measure, with a few dozen requests, whether LinkedIn can be watched per city instead of per person.

Today every candidate's roles are searched separately in every one of their cities, so the number of
requests grows with the number of candidates. This measures the alternative: ask once per city for
everything new, and match every candidate against that one list. It answers four questions:

  1. With no search word, does a city's search return its new postings, and how many in an hour?
  2. Does one search for several roles joined by OR return what the separate searches return?
  3. Does the "posted in the last N seconds" filter work for a short window (ten minutes)?
  4. Does LinkedIn refuse any of this from this address?

It stops at the first refusal and never makes more than MAX_REQUESTS requests.

    python tools/linkedin_probe.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import job_match_runner as core  # noqa: E402

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
CITIES = ["Bengaluru", "Hyderabad", "Pune"]
ROLES = ["Python Developer", "Java Developer", "Data Analyst", "Software Engineer", "QA Engineer", "DevOps Engineer"]
MAX_REQUESTS = 90
PAUSE = 1.5
IT_TITLE = re.compile(
    r"developer|engineer|programmer|analyst|architect|devops|data|software|sdet|tester|qa\b|cloud|full.?stack|backend|frontend|"
    r"\bai\b|\bml\b|machine learning|python|java|\.net|react|angular|node|sap|salesforce|consultant|administrator|support", re.I)

made = 0
refused = False


def page(keywords: str, city: str, seconds: int, start: int):
    """One result page: (status, [(job id, title, place, age text)])."""
    global made, refused
    if made >= MAX_REQUESTS or refused:
        return 0, []
    url = f"{SEARCH}?keywords={quote_plus(keywords)}&location={quote_plus(city)}&f_TPR=r{seconds}&f_JT=F&sortBy=DD&start={start}"
    made += 1
    try:
        response = core.requests.get(url, headers=core._get_browser_headers(), timeout=25)
    except core.requests.RequestException as exc:
        print(f"      request failed: {type(exc).__name__}")
        return -1, []
    time.sleep(PAUSE)
    if response.status_code != 200:
        refused = response.status_code in (429, 999, 403)
        return response.status_code, []
    soup = core.BeautifulSoup(response.text, "html.parser")
    cards = []
    for card in soup.find_all("li"):
        found = core._JOB_URN_RE.search(str(card))
        title = card.select_one(".base-search-card__title")
        if not found or not title:
            continue
        place = card.select_one(".job-search-card__location")
        age = card.select_one("time")
        cards.append((found.group(1), " ".join(title.get_text().split()), " ".join(place.get_text().split()) if place else "",
                      " ".join(age.get_text().split()) if age else ""))
    return 200, cards


def listing(keywords: str, city: str, seconds: int, max_pages: int):
    """Page through one search. Returns ({job id: card}, pages asked, statuses, cards on the first page)."""
    jobs, statuses, first = {}, [], 0
    start = 0
    for number in range(max_pages):
        status, cards = page(keywords, city, seconds, start)
        statuses.append(status)
        if number == 0:
            first = len(cards)
        new = [card for card in cards if card[0] not in jobs]
        for card in new:
            jobs[card[0]] = card
        if status != 200 or not cards or not new:
            break
        start += len(cards)
    return jobs, len(statuses), statuses, first


def main() -> int:
    print(f"LinkedIn probe: {len(CITIES)} cities, at most {MAX_REQUESTS} requests, {PAUSE}s apart.\n")
    or_query = " OR ".join(f'"{role}"' for role in ROLES)
    for city in CITIES:
        if refused:
            break
        print(f"== {city}")
        everything, pages, statuses, first = listing("", city, 3600, 12)
        it = [card for card in everything.values() if IT_TITLE.search(card[1])]
        ages = [card[3] for card in list(everything.values())[-3:]]
        print(f"   1. no search word, last hour : {len(everything)} postings in {pages} page(s) of {first}; statuses {sorted(set(statuses))}; "
              f"{len(it)} look like IT jobs; oldest shown: {ages}")
        merged, pages, statuses, _ = listing(or_query, city, 3600, 8)
        print(f"   2. six roles joined by OR     : {len(merged)} postings in {pages} page(s); statuses {sorted(set(statuses))}")
        separate = {}
        asked = 0
        for role in ROLES[:3]:
            found, used, _, _ = listing(role, city, 3600, 2)
            asked += used
            separate.update(found)
        print(f"      three of those roles alone : {len(separate)} postings in {asked} page(s); "
              f"{len(set(separate) & set(merged))} of them are in the OR search, {len(set(separate) & set(everything))} in the no-word search")
        short, pages, statuses, _ = listing("", city, 600, 4)
        print(f"   3. no search word, 10 minutes : {len(short)} postings in {pages} page(s); statuses {sorted(set(statuses))}; "
              f"ages {sorted({card[3] for card in short.values()})[:6]}")
        print("      sample titles:", "; ".join(card[1][:38] for card in list(everything.values())[:6]))
    print(f"\n4. Requests made: {made}. Refused by LinkedIn: {'YES' if refused else 'no'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
