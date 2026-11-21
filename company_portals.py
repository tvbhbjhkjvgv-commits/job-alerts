#!/usr/bin/env python3
"""Company career sites: new openings at a fixed list of employers, strictly for the candidate's experience.

This runs beside the LinkedIn monitor (job_match_runner.py) and uses its filters and its score. It
reads each employer's own career site, and mails once per new opening that is all of:
  - in Hyderabad, Bengaluru or Chennai, or remote in India,
  - one of the candidate's roles (the same title and Python role-fit rules as the hourly report),
  - full-time,
  - asking for experience that includes the candidate's years (table 1 of the mail).
A posting whose stated experience leaves the candidate out (3+ years, 5-8 years, 0-1 year) is never
listed. A posting that states no years at all cannot be checked, so it goes into a separate table 2,
and only when its skill match is --min-match or more.

A career site shows the day a job was posted, not the hour, so there is no "last hour" window here.
Instead the scan remembers every posting it has judged and reports each opening once.

Usage:
  python company_portals.py                    scan, mail what is new, remember it
  python company_portals.py --dry-run          scan and print; send nothing, remember nothing
  python company_portals.py --companies amazon,jpmc
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ElementTree
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Sequence, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import job_match_runner as core

LOG = logging.getLogger("company_portals")

STATE_FILE = core.DATA_DIR / "company_portal_state.json"
# What each site is asked for. Every site is also filtered to India, and the title, location,
# experience and role-fit rules decide afterwards, so a broad word here costs requests, not accuracy.
SEARCH_KEYWORDS = ["Python", "AI Engineer", "Generative AI", "Software Engineer", "Full Stack Developer", "Backend Developer"]
SEARCH_CITIES = ["Hyderabad", "Bengaluru", "Chennai"]
# Newest postings read per search. The scan runs every hour, so new postings are always inside this.
NEWEST_PER_SEARCH = 100
# Job pages opened per site in one run; the rest wait for the next run.
MAX_DETAIL_PAGES = 40
# Longest one site may take in one run.
SITE_BUDGET_SECONDS = 240.0
REQUEST_PAUSE_SECONDS = 0.5
# A posting waits this long for a mail that could not be delivered, then it is dropped.
HELD_DAYS = 7.0
# A judged posting is forgotten this long after it was last seen on the site.
DECIDED_DAYS = 21.0
REPORTED_DAYS = 180.0

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class PortalError(Exception):
    """A career site could not be read."""


class Site:
    """Requests to one career site: retries, a pause after each request, and a time budget."""

    def __init__(self, keywords: Sequence[str], candidate_yoe: float = 2.0):
        self.session = core.requests.Session()
        self.keywords = list(keywords)
        self.candidate_yoe = candidate_yoe  # for a site whose search can be narrowed by years of experience
        self.deadline = time.monotonic() + SITE_BUDGET_SECONDS
        self.requests_made = 0

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    def fetch(self, url: str, params: Dict[str, Any] | None = None, accept: str = "application/json",
              data: Dict[str, Any] | None = None) -> Any:
        """One page of the site. With data, the request is a form post (a search form, a "view job" button)."""
        if self.out_of_time():
            raise PortalError("time limit for this site reached")
        problem = "no answer"
        for attempt in range(3):
            self.requests_made += 1
            try:
                headers = {**BROWSER_HEADERS, "Accept": accept}
                if data is None:
                    response = self.session.get(url, params=params, headers=headers, timeout=25)
                else:
                    response = self.session.post(url, params=params, data=data, headers=headers, timeout=25)
            except core.requests.RequestException as exc:
                problem = type(exc).__name__
            else:
                if response.status_code == 200:
                    time.sleep(REQUEST_PAUSE_SECONDS)
                    return response
                problem = f"HTTP {response.status_code}"
                if response.status_code not in (429, 500, 502, 503, 504):
                    break
            time.sleep(2.0 * (attempt + 1))
        raise PortalError(f"{problem} from {url.split('?')[0]}")

    def json(self, url: str, params: Dict[str, Any] | None = None, data: Dict[str, Any] | None = None) -> Any:
        try:
            return self.fetch(url, params, data=data).json()
        except ValueError as exc:
            raise PortalError(f"unreadable answer from {url.split('?')[0]}") from exc

    def soup(self, url: str, params: Dict[str, Any] | None = None, data: Dict[str, Any] | None = None) -> Any:
        return core.BeautifulSoup(self.fetch(url, params, accept="text/html,application/xhtml+xml", data=data).text, "html.parser")

    def feed(self, url: str, params: Dict[str, Any] | None = None) -> Any:
        """The root element of an RSS job feed."""
        try:
            return ElementTree.fromstring(self.fetch(url, params, accept="application/rss+xml,application/xml").content)
        except ElementTree.ParseError as exc:
            raise PortalError(f"unreadable feed from {url.split('?')[0]}") from exc


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def _text(markup: Any) -> str:
    """Text of a description, with line breaks where the page has them."""
    if not markup:
        return ""
    return core._html_to_text(core.BeautifulSoup(str(markup), "html.parser"))


def _sections(*parts: Tuple[str, Any]) -> str:
    """Join (heading, html) parts into one description; a heading is on a line of its own."""
    out = []
    for heading, markup in parts:
        body = _text(markup)
        if body:
            out.append(f"{heading}:\n{body}" if heading else body)
    return "\n\n".join(out)


def _record(
    key: str,
    company: str,
    native_id: Any,
    title: Any,
    location: Any,
    url: str,
    description: str = "",
    posted: Any = "",
    workplace: Any = "",
    employment: Any = "",
) -> Dict[str, Any]:
    posted_text = _one_line(posted)
    return {
        "job_id": f"{key}:{native_id}",
        "portal": key,
        "source": f"{company} careers",
        "company": company,
        "title": _one_line(title),
        "location": _one_line(location),
        "description": description,
        "job_url": url,
        "apply_url": url,
        "workplace_type": _one_line(workplace) or "Not specified",
        "employment_type": _one_line(employment),
        "is_easy_apply": False,
        "posted_time": posted_text,
        "freshness_badge": posted_text or "Date not shown",
    }


# ---------------------------------------------------------------------------
# One reader per career site. Each returns the site's India postings as records; a record whose
# description needs a second request carries it as record["_detail"](site, record).
# ---------------------------------------------------------------------------
def read_amazon(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        data = site.json("https://www.amazon.jobs/en/search.json", {
            "base_query": keyword, "loc_query": "India", "country": "IND",
            "result_limit": NEWEST_PER_SEARCH, "sort": "recent", "offset": 0,
        })
        for item in data.get("jobs") or []:
            native_id = str(item.get("id_icims") or item.get("id") or "")
            if not native_id or not item.get("job_path"):
                continue
            jobs[native_id] = _record(
                "amazon", "Amazon", native_id, item.get("title"),
                item.get("normalized_location") or item.get("location"),
                "https://www.amazon.jobs" + str(item["job_path"]),
                description=_sections(
                    ("", item.get("description")),
                    ("Basic Qualifications", item.get("basic_qualifications")),
                    ("Preferred Qualifications", item.get("preferred_qualifications")),
                ),
                posted=item.get("posted_date"), employment=item.get("job_schedule_type"),
            )
    return list(jobs.values())


def read_capgemini(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        for page in (1, 2):
            data = site.json("https://cg-job-search-microservices.azurewebsites.net/api/job-search", {
                "page": page, "size": 50, "country_code": "in-en", "search": keyword,
            })
            items = data.get("data") or []
            for item in items:
                native_id = str(item.get("id") or item.get("_id") or "")
                url = str(item.get("apply_job_url") or "")
                if not native_id or not url.startswith("https://"):
                    continue
                contract = _one_line(item.get("contract_type"))
                jobs[native_id] = _record(
                    "capgemini", "Capgemini", native_id, item.get("title"), f"{_one_line(item.get('location'))}, India", url,
                    description=_text(item.get("description")),
                    posted=str(item.get("updated_at") or item.get("indexed_at") or "")[:10],
                    employment="" if contract.lower() in ("", "permanent") else f"Contract ({contract})",
                )
            if len(items) < 50 or page * 50 >= int(data.get("count") or 0):
                break
    return list(jobs.values())


_JPMC_API = "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/"
_JPMC_INDIA = "300000000289360"


def _jpmc_detail(site: Site, job: Dict[str, Any]) -> None:
    native_id = job["job_id"].split(":", 1)[1]
    data = site.json(
        f"{_JPMC_API}recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
        f"&finder=ById;Id=%22{quote(native_id)}%22,siteNumber=CX_1001"
    )
    item = (data.get("items") or [{}])[0]
    job["description"] = _sections(
        ("", item.get("ExternalDescriptionStr")),
        ("Responsibilities", item.get("ExternalResponsibilitiesStr")),
        ("Qualifications", item.get("ExternalQualificationsStr")),
    )
    if item.get("WorkplaceType"):
        job["workplace_type"] = _one_line(item["WorkplaceType"])


def read_jpmc(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        data = site.json(
            f"{_JPMC_API}recruitingCEJobRequisitions?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber=CX_1001,limit={NEWEST_PER_SEARCH},keyword=%22{quote(keyword)}%22,"
            f"locationId={_JPMC_INDIA},sortBy=POSTING_DATES_DESC"
        )
        for item in ((data.get("items") or [{}])[0].get("requisitionList")) or []:
            native_id = str(item.get("Id") or "")
            if not native_id:
                continue
            places = [_one_line(item.get("PrimaryLocation"))]
            places += [_one_line(other.get("Name")) for other in item.get("secondaryLocations") or [] if isinstance(other, dict)]
            record = _record(
                "jpmc", "JPMorgan Chase", native_id, item.get("Title"), " / ".join(p for p in places if p),
                f"https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/{native_id}",
                posted=item.get("PostedDate"), workplace=item.get("WorkplaceType"),
            )
            record["_detail"] = _jpmc_detail
            jobs[native_id] = record
    return list(jobs.values())


def read_tiger_analytics(site: Site) -> List[Dict[str, Any]]:
    data = site.json("https://apply.workable.com/api/v1/widget/accounts/tiger-analytics", {"details": "true"})
    jobs = []
    for item in data.get("jobs") or []:
        places = item.get("locations") or [{"country": item.get("country"), "city": item.get("city"), "region": item.get("state")}]
        india = [p for p in places if isinstance(p, dict) and str(p.get("country") or "").lower() == "india"]
        if not india or not item.get("shortcode"):
            continue
        location = " / ".join(", ".join(_one_line(p.get(k)) for k in ("city", "region", "country") if p.get(k)) for p in india)
        jobs.append(_record(
            "tiger", "Tiger Analytics", item["shortcode"], item.get("title"), location,
            str(item.get("url") or f"https://apply.workable.com/j/{item['shortcode']}"),
            description=_text(item.get("description")), posted=item.get("published_on"),
            workplace="Remote" if item.get("telecommuting") else "", employment=item.get("employment_type"),
        ))
    return jobs


def read_infor(site: Site) -> List[Dict[str, Any]]:
    data = site.json("https://careers.infor.com/postings.json")
    jobs = []
    for item in data.get("data") or []:
        country = ((((item.get("job") or {}).get("structure_custom_group_one")) or {}).get("name")) or ""
        url = str(item.get("url") or "")
        if str(country).lower() != "india" or not item.get("id") or not url.startswith("https://"):
            continue
        place = item.get("location") or {}
        jobs.append(_record(
            "infor", "Infor", item["id"], item.get("title"), f"{_one_line(place.get('city') or place.get('name'))}, India", url,
            description=_sections(
                ("", item.get("description")),
                (_one_line(item.get("key_responsibilities_header")).rstrip(":") or "Responsibilities", item.get("key_responsibilities")),
                (_one_line(item.get("skills_knowledge_expertise_header")).rstrip(":") or "Qualifications", item.get("skills_knowledge_expertise")),
                (_one_line(item.get("benefits_header")).rstrip(":") or "Preferred Qualifications", item.get("benefits")),
            ),
            workplace=item.get("workplace_type_text"), employment=item.get("employment_type_text"),
        ))
    return jobs


_NTT_SITE = "https://careers-inc.nttdata.com"


def _ntt_detail(site: Site, job: Dict[str, Any]) -> None:
    page = site.soup(job["job_url"])
    body = page.select_one("span.jobdescription") or page.select_one('[itemprop="description"]')
    job["description"] = core._html_to_text(body) if body else ""


def read_ntt_data(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        for start in range(0, NEWEST_PER_SEARCH, 25):
            page = site.soup(f"{_NTT_SITE}/search/", {
                "q": keyword, "locationsearch": "India", "sortColumn": "referencedate", "sortDirection": "desc", "startrow": start,
            })
            rows = page.select("tr.data-row")
            for row in rows:
                link = row.select_one("a.jobTitle-link")
                number = re.search(r"/(\d{5,})/?$", str(link.get("href") if link else ""))
                if not link or not number:
                    continue
                place = row.select_one("span.jobLocation")
                posted = row.select_one("span.jobDate")
                # "Bangalore, KA, IN": spell the country out, which is what the location rule looks for.
                location = re.sub(r",\s*IN$", ", India", _one_line(place.get_text() if place else ""))
                record = _record(
                    "ntt", "NTT DATA", number.group(1), link.get_text(), location, _NTT_SITE + str(link["href"]),
                    posted=posted.get_text() if posted else "",
                )
                record["_detail"] = _ntt_detail
                jobs[number.group(1)] = record
            if len(rows) < 25:
                break
    return list(jobs.values())


_COGNIZANT_SITE = "https://careers.cognizant.com"
_WORK_MODE_WORDS = re.compile(r"\b(?:remote|work from home|hybrid|on-?site|office)\b", re.I)


def _cognizant_detail(site: Site, job: Dict[str, Any]) -> None:
    page = site.soup(job["job_url"])
    body = page.select_one(".cms-content")
    job["description"] = core._html_to_text(body) if body else ""
    for item in page.select(".job-meta-item"):
        label = _one_line(item.get_text())
        if label.lower().startswith("date published"):
            job["posted_time"] = job["freshness_badge"] = label[len("date published"):].strip()
        elif label.lower().startswith("work model"):
            mode = label[len("work model"):].strip()
            job["workplace_type"] = "Remote" if re.search(r"remote|work from home", mode, re.I) else mode


def read_cognizant(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for city in SEARCH_CITIES:
        for keyword in site.keywords:
            page = site.soup(f"{_COGNIZANT_SITE}/global-en/jobs/", {"keyword": keyword, "location": city, "pagesize": 50})
            for card in page.select("div.card-job"):
                link = card.select_one("a.js-view-job") or card.select_one("h2 a")
                native_id = str(card.get("data-id") or "")
                if not link or not native_id or not link.get("href"):
                    continue
                details = [_one_line(item.get_text()) for item in card.select("ul.job-meta li")]
                mode = next((d for d in details[1:] if _WORK_MODE_WORDS.search(d)), "")
                record = _record(
                    "cognizant", "Cognizant", native_id, link.get_text(), details[0] if details else city,
                    _COGNIZANT_SITE + str(link["href"]),
                    workplace="Remote" if re.search(r"remote|work from home", mode, re.I) else mode,
                )
                record["_detail"] = _cognizant_detail
                jobs[native_id] = record
    return list(jobs.values())


# Wipro and EY run the same career-site software, which publishes each search as a job feed: the
# newest postings first, each with its full text, so no job page has to be opened.
_FEED_PLACES = "Hyderabad OR Secunderabad OR Bengaluru OR Bangalore OR Chennai"
_FEED_TITLE = re.compile(r"^(.*\S)\s*\(([^()]*)\)\s*$", re.S)


def _feed_location(place: str) -> str:
    """'Bengaluru, IND-29, IN, 560035' -> 'Bengaluru, India'; a place outside India is kept as written."""
    parts = [part.strip() for part in place.split(",") if part.strip()]
    return f"{parts[0]}, India" if "IN" in parts[1:] else ", ".join(parts)


def _read_job_feed(site: Site, key: str, company: str, base: str) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        root = site.feed(f"{base}/services/rss/job/", {
            "locale": "en_US", "rows": NEWEST_PER_SEARCH,
            "keywords": f"({keyword}) AND locationSearch:({_FEED_PLACES})" if _FEED_PLACES else f"({keyword})",
        })
        for item in root.iter("item"):
            url = str(item.findtext("link") or "").split("?")[0]
            number = re.search(r"/(\d{5,})/?$", url)
            named = _FEED_TITLE.match(_one_line(item.findtext("title")))
            if not number or not named or not url.startswith("https://"):
                continue
            title = named.group(1)
            posted = re.search(r"\d{1,2} [A-Za-z]{3} \d{4}", str(item.findtext("pubDate") or ""))
            jobs[number.group(1)] = _record(
                key, company, number.group(1), title, _feed_location(named.group(2)), url,
                description=_text(item.findtext("description")), posted=posted.group(0) if posted else "",
                # Wipro marks a contract post in its title: "DEVELOPER L3(CONTRACT)".
                employment="Contract" if re.search(r"\(\s*contract\s*\)", title, re.I) else "",
            )
    return list(jobs.values())


def read_wipro(site: Site) -> List[Dict[str, Any]]:
    return _read_job_feed(site, "wipro", "Wipro", "https://careers.wipro.com")


def read_ey(site: Site) -> List[Dict[str, Any]]:
    return _read_job_feed(site, "ey", "EY", "https://careers.ey.com")


# LTIMindtree (now trading as LTM) lists its jobs at ltm.ripplehire.com. The token is the public one
# in the "India" link on ltm.com/careers.
_LTIM_SITE = "https://ltm.ripplehire.com/candidate/"
_LTIM_TOKEN = "xviyQvbnyYZdGtozXoNm"
_LTIM_PAGE = 100
_LTIM_PAGES = 5


def _ltim_detail(site: Site, job: Dict[str, Any]) -> None:
    native_id = job["job_id"].split(":", 1)[1]
    data = site.json(_LTIM_SITE + "candidatejobdetail", {"token": _LTIM_TOKEN, "jobSeq": native_id, "source": "CAREERSITE", "lang": "en"})
    item = data.get("jobVO") or {}
    job["description"] = _sections(("", item.get("jobDesc")), ("Skills", item.get("jobSkills")))
    if item.get("jobPostingDate"):
        job["posted_time"] = job["freshness_badge"] = _one_line(item["jobPostingDate"])
    kind = _one_line(item.get("jobTypeCustom3"))
    if kind and kind.lower() != "permanent":
        job["employment_type"] = kind


def read_ltimindtree(site: Site) -> List[Dict[str, Any]]:
    jobs: Dict[str, Dict[str, Any]] = {}
    # The site spells Bengaluru both ways. A candidate with no city (remote only) gets all of India.
    for city in [*SEARCH_CITIES, *(["Bangalore"] if "Bengaluru" in SEARCH_CITIES else [])] or [""]:
        for page in range(_LTIM_PAGES):
            query = {"page": page, "search": "*:*", "token": _LTIM_TOKEN, "source": "CAREERSITE",
                     "pagesize": _LTIM_PAGE, "geo": "India", **({"location": city} if city else {})}
            data = site.json(_LTIM_SITE + "candidatejobsearch", data={"careerSiteUrlParams": json.dumps(query), "lang": "en"})
            items = data.get("jobVoList") or []
            for item in items:
                native_id = str(item.get("jobSeq") or "")
                if not native_id:
                    continue
                record = _record(
                    "ltim", "LTIMindtree", native_id, item.get("jobTitle"), f"{_one_line(item.get('locations')) or city or 'India'}, India",
                    f"{_LTIM_SITE}?token={_LTIM_TOKEN}&lang=en&source=CAREERSITE#detail/job/{native_id}",
                )
                record["experience"] = _one_line(item.get("jobReqExp"))  # "5 - 7 Years", listed beside the title
                record["_detail"] = _ltim_detail
                jobs[native_id] = record
            if len(items) < _LTIM_PAGE or (page + 1) * _LTIM_PAGE >= int(data.get("totalJobCount") or 0):
                break
    return list(jobs.values())


# Tech Mahindra's site is one page: a search posts its form back, and a job's own page opens only
# through that job's button, in the same visit. A job has no link of its own, so the mail links to
# the openings page and gives the job reference number to search for there.
_TECHM_SITE = "https://careers.techmahindra.com/"
_TECHM_PAGE = _TECHM_SITE + "CurrentOpportunity.aspx"
_TECHM_FIELD = "ctl00$ContentPlaceHolder1$"
_TECHM_CARD = re.compile(r"Skill Set\s*:\s*(.*?)\s*Experience\s*:\s*(.*?)\s*Location\s*:\s*(.*)$", re.S)
_TECHM_OPENS_JOB = re.compile(r"window\.open\(\s*['\"]JobDetails\.aspx")


def _form_fields(page: Any) -> Dict[str, str]:
    """What a browser would send back for the page's form, before any button is pressed."""
    fields = {box["name"]: str(box.get("value") or "") for box in page.select("input[type=hidden], input[type=text]") if box.get("name")}
    for menu in page.select("select"):
        chosen = menu.select_one("option[selected]") or menu.select_one("option")
        if menu.get("name"):
            fields[menu["name"]] = str(chosen.get("value") or "") if chosen else ""
    return fields


def _techm_detail(form: Dict[str, str], button: str) -> Callable[[Site, Dict[str, Any]], None]:
    def fill(site: Site, job: Dict[str, Any]) -> None:
        # The button answers with the same page plus a script that opens JobDetails.aspx, which then
        # shows the job this visit pressed last. Both are checked, so one job's text never lands on another.
        pressed = site.soup(_TECHM_PAGE, data={**form, button: "Apply/Shortlist"})
        if not _TECHM_OPENS_JOB.search(str(pressed)):
            raise PortalError("the site did not open the job page")
        page = site.soup(_TECHM_SITE + "JobDetails.aspx")
        heading = page.select_one("[id$=lblDesignationName]")
        if not heading or _one_line(heading.get_text()).lower() != job["title"].lower():
            raise PortalError("the job page shown was not this job's")
        facts = {}
        for row in page.select(".job-details li"):
            label, value = row.select_one("b"), row.select_one("span")
            if label and value:
                facts[_one_line(label.get_text()).rstrip(": ").lower()] = _one_line(value.get_text())
        summary = next((h.parent for h in page.select("h3") if _one_line(h.get_text()).lower() == "job summary"), None)
        job["description"] = "\n\n".join(part for part in (
            f"Skill set: {facts['skill set']}" if facts.get("skill set") else "",
            f"Total experience: {facts['total experience']}" if facts.get("total experience") else "",
            core._html_to_text(summary) if summary else "",
        ) if part)
        place = re.match(r"^(.*?)\s*\[(.+)\]$", facts.get("location", ""))  # "HYDERABAD [India]"
        if place:
            job["location"] = f"{place.group(1).title()}, {place.group(2)}"
        job["posted_time"] = facts.get("job post date", "")
        reference = facts.get("job reference no", "")
        job["freshness_badge"] = "; ".join(part for part in (
            job["posted_time"], f"search job reference {reference} on the page" if reference else "") if part) or "Date not shown"
    return fill


def read_tech_mahindra(site: Site) -> List[Dict[str, Any]]:
    blank = _form_fields(site.soup(_TECHM_PAGE))
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        page = site.soup(_TECHM_PAGE, data={**blank, _TECHM_FIELD + "txtAdvanceSearch": keyword, _TECHM_FIELD + "btnFreeSearch": "Search"})
        form = _form_fields(page)
        for code in page.select("input[name$=HdnJobCode]"):
            card = code.parent
            title, details = card.find("div"), card.find("p")
            listed = _TECHM_CARD.search(_one_line(details.get_text(" "))) if details else None
            native_id = str(code.get("value") or "")
            if not native_id or not title or not listed or native_id in jobs:
                continue
            record = _record("techm", "Tech Mahindra", native_id, title.get_text(), listed.group(3).title(), _TECHM_PAGE)
            record["experience"] = listed.group(2)  # "4.00-7.00 Years"
            record["_detail"] = _techm_detail(form, code["name"].replace("HdnJobCode", "btn_jobs"))
            jobs[native_id] = record
    return list(jobs.values())


# Infosys's career page (career.infosys.com) refuses a script, but the job-search service the page
# reads answers one: every open India posting in a single answer, each with its text and its years.
_INFOSYS_JOBS = "https://intapgateway.infosysapps.com/careersci/search/intapjbsrch/getCareerSearchJobs"
_INFOSYS_JOB_PAGE = "https://career.infosys.com/jobdesc?jobReferenceCode="


def read_infosys(site: Site) -> List[Dict[str, Any]]:
    data = site.json(_INFOSYS_JOBS, {"sourceId": "1,21", "searchText": "ALL"})
    if not isinstance(data, list):
        raise PortalError(f"unreadable answer from {_INFOSYS_JOBS}")
    jobs: Dict[str, Dict[str, Any]] = {}
    for item in data:
        reference = _one_line(item.get("referenceCode")) if isinstance(item, dict) else ""
        if not reference:
            continue
        city = _one_line(item.get("location"))  # "BANGALORE", "Kolkata(WB)"
        record = _record(
            "infosys", "Infosys", reference, item.get("postingTitle"),
            f"{city.title() if city.isupper() else city}, {_one_line(item.get('country')) or 'India'}",
            _INFOSYS_JOB_PAGE + quote(reference),
            description=_sections(
                ("", item.get("postingDescription")),
                ("Responsibilities", item.get("rolesResponsibilities")),
                ("Technical requirements", item.get("technicalRequirement")),
                ("Preferred skills", item.get("preferredSkills")),
                ("Additional responsibilities", item.get("additionalResponsibility")),
                ("Education", item.get("educationalRequirement")),
            ),
            posted=str(item.get("createdOn") or "")[:10],
        )
        low, high = item.get("minExperienceLevel"), item.get("maxExperienceLevel")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and high > 0:
            record["experience"] = f"{low:g} - {high:g} Years"  # the site's own fields, shown beside the title
        jobs[reference] = record
    return list(jobs.values())


# Accenture marks every posting with one of these experience bands, and its search can be narrowed
# to them. It stamps several hundred India postings a day in one batch, so the newest 100 of a search
# reach back far enough only when the search is narrowed to the cities and to the candidate's bands.
_ACCENTURE_SEARCH = "https://www.accenture.com/api/accenture/elastic/findjobs"
_ACCENTURE_BANDS = ((0, 2), (2, 5), (5, 10), (10, 12), (12, 14))


def _city_spellings() -> List[str]:
    """The search cities under every name a site may file them ("Bengaluru", "Bangalore"); short codes are left out."""
    if SEARCH_CITIES == ["Hyderabad", "Bengaluru", "Chennai"]:
        return list(SEARCH_CITIES)  # the three the scan was built on, exactly as the sites' filters name them
    return [name.title() for city in SEARCH_CITIES for name in core.city_names(city) if len(name) > 3]


def read_accenture(site: Site) -> List[Dict[str, Any]]:
    bands = [f"Experience: {low}-{high} years" for low, high in _ACCENTURE_BANDS if low <= site.candidate_yoe <= high]
    filters = [{"fieldName": "location.keyword", "items": _city_spellings(), "multiSelect": False}] if SEARCH_CITIES else []
    if bands:
        filters.append({"fieldName": "yearsOfExperience.keyword", "items": bands, "multiSelect": False})
    jobs: Dict[str, Dict[str, Any]] = {}
    for keyword in site.keywords:
        data = site.json(_ACCENTURE_SEARCH, data={
            "startIndex": 0, "maxResultSize": NEWEST_PER_SEARCH, "jobKeyword": keyword, "jobCountry": "India",
            "jobLanguage": "en", "countrySite": "in-en", "sortBy": 1,  # 1 = newest first
            # The page's own search ("vectorSearch") also returns postings that are only near the word;
            # any other value searches for the word itself.
            "searchType": "keyword", "jobFilters": json.dumps(filters),
        })
        for item in data.get("data") or []:
            native_id = _one_line(item.get("requisitionId"))
            url = str(item.get("jobDetailUrl") or "").replace("{0}", "in-en")
            if not native_id or not url.startswith("https://www.accenture.com/"):
                continue
            places = item.get("location") or []
            places = [places] if isinstance(places, str) else places
            jobs[native_id] = _record(
                "accenture", "Accenture", native_id, item.get("title"),
                f"{' / '.join(_one_line(place) for place in places if place)}, {_one_line(item.get('country')) or 'India'}",
                url, description=_text(item.get("jobDescription")), posted=str(item.get("updateDate") or "")[:10],
                workplace=item.get("remoteType"), employment=item.get("employeeType"),
            )
    return list(jobs.values())


# key: (name shown in the mail, reader). The order is the order of the scan.
PORTALS: Dict[str, Tuple[str, Callable[[Site], List[Dict[str, Any]]]]] = {
    "ntt": ("NTT DATA", read_ntt_data),
    "amazon": ("Amazon", read_amazon),
    "cognizant": ("Cognizant", read_cognizant),
    "tiger": ("Tiger Analytics", read_tiger_analytics),
    "infor": ("Infor", read_infor),
    "jpmc": ("JPMorgan Chase", read_jpmc),
    "capgemini": ("Capgemini", read_capgemini),
    "ltim": ("LTIMindtree", read_ltimindtree),
    "ey": ("EY", read_ey),
    "techm": ("Tech Mahindra", read_tech_mahindra),
    "wipro": ("Wipro", read_wipro),
    "infosys": ("Infosys", read_infosys),
    "accenture": ("Accenture", read_accenture),
}
# Asked for, but not readable by a script. Each entry is said in every mail, so the gap is not forgotten.
NOT_COVERED = {
    "EPAM": "its India career site (jobs.welcome.epam.in) hands a script only 10 of its roughly 340 jobs, in no date order",
    "Cimpress": "its career site (cimpress.ripplehire.com) answers with an empty job list",
    "Sify": "its job board (sifycareer.tallite.com) answers only requests signed and encrypted by its own page",
    "Hetero": "its job board (hetero.darwinbox.in) refuses a script with HTTP 403",
}

# The words and the state file of this scan. Another scan that reuses run_scan() brings its own.
COMPANY_SITES: Dict[str, Any] = {
    "name": "Company career sites",
    "short": "Company sites",
    "read_from": "Read from the companies' own career sites: {sites}.",
    "footer": 'Automated report from the company-site scan, which runs every hour. "Posted" is the date the career site shows.',
    "preview": "company_portals_preview.html",
    "not_covered": NOT_COVERED,
    "state_file": None,  # None = STATE_FILE
}


# ---------------------------------------------------------------------------
# State, judging and the mail
# ---------------------------------------------------------------------------
def load_state(path: Any = None) -> Dict[str, Any]:
    """decided : {job id: when last seen} - postings already judged, so they are not judged or opened again
    reported: {job id or "k:" + fingerprint: when} - openings already mailed
    queue   : openings that qualify and have not been mailed yet
    held    : openings that state no experience (table 2 of the mail), not mailed yet
    failures: {site: runs in a row it could not be read}
    """
    state: Dict[str, Any] = {"decided": {}, "reported": {}, "queue": [], "held": [], "failures": {}}
    try:
        payload = json.loads((path or STATE_FILE).read_text(encoding="utf-8"))
        for key, kind in (("decided", dict), ("reported", dict), ("queue", list), ("held", list), ("failures", dict)):
            if isinstance(payload.get(key), kind):
                state[key] = payload[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as exc:
        LOG.warning("Could not read the scan state; starting empty: %s", exc)
    return state


def save_state(state: Dict[str, Any], path: Any = None) -> None:
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    def newer_than(stamps: Dict[str, str], days: float) -> Dict[str, str]:
        cutoff = (now - timedelta(days=days)).isoformat()
        return {key: stamp for key, stamp in stamps.items() if str(stamp) >= cutoff}

    state["decided"] = newer_than(state["decided"], DECIDED_DAYS)
    state["reported"] = newer_than(state["reported"], REPORTED_DAYS)
    (path or STATE_FILE).write_text(json.dumps({**state, "updated_at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")


_RECORD_FIELDS = (
    "job_id", "portal", "source", "title", "company", "location", "also_in", "duplicate_ids", "posting_key", "workplace_type",
    "is_easy_apply", "skill_match_pct", "matched_skills", "missing_skills", "min_experience_years", "max_experience_years",
    "experience_requirement", "job_url", "apply_url", "posted_time", "freshness_badge", "found_at",
)


def _stored(job: Dict[str, Any], now_iso: str) -> Dict[str, Any]:
    record = dict(job, found_at=now_iso)
    return {key: record[key] for key in _RECORD_FIELDS if key in record}


def _ids(record: Dict[str, Any]) -> List[str]:
    ids = [str(record.get("job_id")), *[str(x) for x in record.get("duplicate_ids") or []]]
    if record.get("posting_key"):
        ids.append(core.SEEN_KEY_PREFIX + str(record["posting_key"]))
    return ids


BELOW_MATCH = "No experience stated and skill match too low"
BELOW_STATED_MATCH = "Skill match too low"


def judge(job: Dict[str, Any], candidate_yoe: float, min_match: int = 60, stated_min_match: int = 0) -> Tuple[str, str]:
    """('match' | 'no_yoe' | '', reason it was dropped) for one posting whose description has been read.

    match  : passes every filter of the hourly report, states experience that includes candidate_yoe,
             and scores stated_min_match or more (0 = any score).
    no_yoe : passes every filter and scores min_match or more, but states no experience at all.
    """
    reason = core.rejection_reason(job, candidate_yoe=candidate_yoe)
    if reason:
        return "", reason
    core.score_job(job)
    score = int(job.get("skill_match_pct", 0))
    if core.experience_is_stated(job):
        return ("match", "") if score >= stated_min_match else ("", BELOW_STATED_MATCH)
    return ("no_yoe", "") if score >= min_match else ("", BELOW_MATCH)


def listed_experience_excludes(job: Dict[str, Any], candidate_yoe: float) -> bool:
    """The experience a site lists beside the title (record["experience"]) already leaves the candidate out.

    Such a posting is dropped without opening its page: the page could only repeat the figure.
    """
    listed = {"title": job.get("title"), "experience": job.get("experience")}
    return bool(listed["experience"]) and core.experience_is_stated(listed) and not core.experience_is_eligible(listed, candidate_yoe)


def scan_site(key: str, state: Dict[str, Any], args: argparse.Namespace, now_iso: str, portals: Any = None) -> Dict[str, Any]:
    """Read one career site and judge the postings not judged before."""
    name, reader = (portals or PORTALS)[key]
    site = Site(args.keyword_list, args.candidate_yoe)
    listing = reader(site)
    summary: Dict[str, Any] = {"listed": len(listing), "new": 0, "waiting": 0, "candidates": [], "reasons": {}}
    pages_left = MAX_DETAIL_PAGES
    ready: List[Tuple[Dict[str, Any], str]] = []
    for job in listing:
        job_id = job["job_id"]
        fill = job.pop("_detail", None)
        if job_id in state["decided"]:
            state["decided"][job_id] = now_iso  # still on the site
            continue
        summary["new"] += 1
        # A description cannot rescue a posting whose title or place is already out, so those are not opened.
        early = "" if core.title_is_eligible(job) else "Job title"
        early = early or ("" if core.location_is_eligible(job) else "Location")
        early = early or ("Experience" if listed_experience_excludes(job, args.candidate_yoe) else "")
        if not early and fill and not job["description"].strip():
            if pages_left <= 0 or site.out_of_time():
                summary["waiting"] += 1
                continue
            pages_left -= 1
            try:
                fill(site, job)
            except PortalError as exc:
                LOG.info("%s: could not open %s (%s); it is tried again next run.", name, job["job_url"], exc)
        if not early and not job["description"].strip():
            summary["waiting"] += 1
            continue
        state["decided"][job_id] = now_iso
        ready.append((job, early))
    # The AI reader (when a key is set) reads the postings still in the running, all together, before they are judged.
    core.read_with_brain([job for job, early in ready if not early], args.candidate_yoe)
    for job, early in ready:
        kind, reason = ("", early) if early else judge(job, args.candidate_yoe, args.min_match, getattr(args, "stated_min_match", 0))
        summary["reasons"][reason or kind] = summary["reasons"].get(reason or kind, 0) + 1
        if kind:
            job["portal_class"] = kind
            summary["candidates"].append(job)
    LOG.info(
        "%s: %d India posting(s) listed, %d not judged before, %d still to open; %s (%d request(s)).",
        name, summary["listed"], summary["new"], summary["waiting"],
        ", ".join(f"{k} {v}" for k, v in sorted(summary["reasons"].items(), key=lambda kv: -kv[1])) or "nothing new",
        site.requests_made,
    )
    return summary


def rule_text(candidate_yoe: float) -> str:
    return (
        f"in {core.PLACES_NOTE}, full-time, {core.ROLE_SHORT}, "
        f"and asking for experience that includes your {candidate_yoe:g} years"
    )


def build_mail(
    matches: Sequence[Dict[str, Any]],
    held: Sequence[Dict[str, Any]],
    candidate_yoe: float,
    notes: Sequence[str],
    covered: Sequence[str],
    min_match: int = 60,
    report: Dict[str, Any] | None = None,
    stated_min_match: int = 0,
) -> str:
    report = report or COMPANY_SITES
    if core.PROFILE:  # a candidate's mail is the tables only
        return core.candidate_mail("New openings on company career pages", [
            ("Asking for your years of experience", matches), ("A match, but the posting states no years of experience", held)])
    floor = f", at a skill match of {stated_min_match}% or more" if stated_min_match else ""
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    none = '<p style="color:#666;margin:8px 0 18px">None new since the last mail.</p>'
    notes_html = (
        '<p style="margin-top:22px"><b>Notes on this scan</b></p><ul>' + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul>"
        if notes else ""
    )
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>{html.escape(report["name"])}: new jobs for your experience</h2>
    <p>Run time: <b>{run_time}</b></p>
    <p>{html.escape(report["read_from"].format(sites=', '.join(covered)))}</p>
    <p>Every job in table 1 is {html.escape(rule_text(candidate_yoe))}{floor}. The years are stated in the posting.</p>
    <h3>1. New openings for your experience ({len(matches)})</h3>
    <p>Sorted by skill match, highest first. Each opening is mailed once.</p>
    {core.html_table(matches) if matches else none}
    <h3 style="margin-top:28px">2. Experience not stated ({len(held)})</h3>
    <p>Same place, role and full-time rules, and a skill match of {min_match}% or more, but the posting gives no
    years of experience, so it could not be checked against yours. A posting whose stated experience leaves
    you out is never listed in either table.</p>
    {core.html_table(held) if held else none}
    <p>{core.SCORE_EXPLANATION}</p>
    {notes_html}
    <p style="font-size:11px;color:#666;margin-top:22px">{html.escape(report["footer"])}
    Verify the requirements on the employer's page.</p>
    </body></html>"""


def _order(record: Dict[str, Any]) -> Tuple[int, str, str]:
    return (-int(record.get("skill_match_pct", 0)), str(record.get("company", "")), str(record.get("title", "")).lower())


def run_scan(args: argparse.Namespace, portals: Any = None, report: Dict[str, Any] | None = None) -> int:
    """Scan the sites in args.company_list and mail what is new.

    portals / report: another scan's sites and words (default: the company career sites).
    """
    portals = portals or PORTALS
    report = report or COMPANY_SITES
    stated_min_match = int(getattr(args, "stated_min_match", 0) or 0)
    state = load_state(report["state_file"])
    core.brain.start_run()
    now_iso = datetime.now(timezone.utc).isoformat()
    notes: List[str] = []
    read_ok: List[str] = []
    candidates: List[Dict[str, Any]] = []
    for key in args.company_list:
        name = portals[key][0]
        try:
            summary = scan_site(key, state, args, now_iso, portals)
        except Exception as exc:  # one site failing must not stop the others
            runs = int(state["failures"].get(key, 0)) + 1
            state["failures"][key] = runs
            detail = str(exc) if isinstance(exc, PortalError) else f"{type(exc).__name__}: {exc}"
            LOG.warning("%s could not be read (%s); %d run(s) in a row.", name, detail, runs)
            print(f"::warning::{name} could not be read: {detail}")
            notes.append(f"{name} could not be read this run ({detail}); {runs} run(s) in a row.")
            continue
        state["failures"][key] = 0
        read_ok.append(name)
        candidates += summary["candidates"]
        if not summary["listed"]:
            notes.append(f"{name} listed no openings in India at all, which is unusual; the site may have changed.")
        if summary["waiting"]:
            notes.append(f"{name}: {summary['waiting']} posting(s) are still to be opened; they are judged in the next runs.")
    for name, why in report["not_covered"].items():
        notes.append(f"{name} is not covered: {why}.")

    known = set(state["reported"]) | {x for record in state["queue"] + state["held"] for x in _ids(record)}
    new_matches = new_held = 0
    for job in core.merge_duplicate_postings(candidates):
        record = _stored(job, now_iso)
        ids = _ids(record)
        if any(x in known for x in ids):
            continue
        known.update(ids)
        if job["portal_class"] == "match":
            state["queue"].append(record)
            new_matches += 1
        else:
            state["held"].append(record)
            new_held += 1
    held_cutoff = (datetime.now(timezone.utc) - timedelta(days=HELD_DAYS)).isoformat()
    state["held"] = [r for r in state["held"] if str(r.get("found_at") or now_iso) >= held_cutoff]

    matches = sorted(state["queue"], key=_order)
    held = sorted(state["held"], key=_order)
    LOG.info("%s: %d read, %d new opening(s) for %g years, %d new with no experience stated.",
             report["short"], len(read_ok), new_matches, args.candidate_yoe, new_held)
    for record in matches:
        LOG.info("  %3d%%  %s | %s | %s | %s | %s", int(record.get("skill_match_pct", 0)), record.get("company"), record.get("title"),
                 record.get("location"), core.experience_label(record), record.get("job_url"))

    mailed = True
    if matches or held:
        body = build_mail(matches, held, args.candidate_yoe, notes, read_ok, args.min_match, report, stated_min_match)
        stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p IST")
        subject = (
            f"{report['name']}: {len(matches)} new job(s) for {args.candidate_yoe:g} years' experience, "
            f"{len(held)} with no experience stated | {stamp}"
        )
        if args.dry_run:
            core.REPORT_DIR.mkdir(parents=True, exist_ok=True)
            (core.REPORT_DIR / report["preview"]).write_text(body, encoding="utf-8")
            LOG.info("Dry run: %r not sent; preview written to %s", subject, core.REPORT_DIR)
            for record in held:
                LOG.info("  not stated %3d%%  %s | %s | %s | %s", int(record.get("skill_match_pct", 0)), record.get("company"),
                         record.get("title"), record.get("location"), record.get("job_url"))
        else:
            top = (matches or held)[0]
            more = f" (+{len(matches) + len(held) - 1} more)" if len(matches) + len(held) > 1 else ""
            pushed = core.send_push_notification(
                f"{report['short']}: {len(matches)} new job(s) for your experience, {len(held)} not stated",
                f"{top.get('title')} at {top.get('company')}{more}: {int(top.get('skill_match_pct', 0))}% match, "
                f"experience {core.experience_label(top).lower()}, {top.get('location')}.",
                click_url=str(top.get("job_url") or ""), priority="high",
            )
            mailed = core.send_gmail(subject, body)
            if pushed or mailed:
                for record in state["queue"] + state["held"]:
                    for x in _ids(record):
                        state["reported"][x] = now_iso
                state["queue"], state["held"] = [], []
            else:
                LOG.error("The report could not be delivered by push or mail; it stays queued for the next run.")
    for note in notes:
        LOG.info("Note: %s", note)
    if not args.dry_run:
        save_state(state, report["state_file"])
    if not read_ok:
        LOG.error("%s: nothing could be read.", report["short"])
        return 1
    return core._exit_code(mailed)


def use_profile(path: Any) -> Dict[str, Any]:
    """Make this scan a scan for one candidate: the shared rules, and this scan's search words and cities."""
    global SEARCH_KEYWORDS, SEARCH_CITIES, _FEED_PLACES
    profile = core.apply_profile(core.load_profile(path))
    SEARCH_KEYWORDS = list(profile["roles"])
    SEARCH_CITIES = list(profile["cities"])
    names = [name.title() for city in SEARCH_CITIES for name in core.city_names(city) if len(name) > 3]
    _FEED_PLACES = " OR ".join(f'"{name}"' if " " in name else name for name in names)
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description="New openings on company career sites, strictly for the candidate's experience")
    parser.add_argument("--candidate-yoe", type=float, default=2.0, help="Candidate years of experience (default 2.0)")
    parser.add_argument("--min-match", type=int, default=60,
                        help="Skill match a posting needs when it states no experience (default 60)")
    parser.add_argument("--companies", type=str, default="", help=f"Comma-separated sites to scan (default all): {', '.join(PORTALS)}")
    parser.add_argument("--keywords", type=str, default="", help="Comma-separated search words (default: the built-in list)")
    parser.add_argument("--dry-run", action="store_true", help="Send nothing and keep no state; write the mail to reports/ instead")
    parser.add_argument("--profile", type=str, default=core.os.environ.get("CANDIDATE_PROFILE", ""),
                        help="One candidate's settings file (JSON); see candidates.example/")
    args = parser.parse_args()
    wanted = [c.strip().lower() for c in args.companies.split(",") if c.strip()]
    if args.profile:
        profile = use_profile(args.profile)
        if core.trial_is_over(profile):
            LOG.info("The free trial of candidate %s has ended. Nothing to do.", profile["id"])
            return 0
        if not profile["enabled"] or not profile["career_sites"]["enabled"]:
            LOG.info("The career-site scan is switched off for candidate %s. Nothing to do.", profile["id"])
            return 0
        args.candidate_yoe = profile["experience_years"]
        args.min_match = profile["career_sites"]["min_match"]
        # An unknown company in the file is skipped, so a site that is later removed does not stop the scan.
        wanted = wanted or [key for key in profile["career_sites"]["companies"] if key in PORTALS]
    unknown = [c for c in wanted if c not in PORTALS]
    if unknown:
        parser.error(f"unknown site(s): {', '.join(unknown)}. Known: {', '.join(PORTALS)}")
    args.company_list = wanted or list(PORTALS)
    args.keyword_list = [k.strip() for k in args.keywords.split(",") if k.strip()] or SEARCH_KEYWORDS
    return run_scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
