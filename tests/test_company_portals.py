"""Offline test of the company career-site scan (company_portals.py).

Usage: python tests/test_company_portals.py [<repo_dir>]   (default: the folder above tests/)
Nothing is sent and no career site is contacted: the sites, mail and push are replaced by stand-ins.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo_dir)
import company_portals as cp  # noqa: E402
import job_match_runner as core  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


STACK = "Python, Django, React, PostgreSQL, REST APIs, Docker."


def posting(number, title="Python Developer", description=f"2-4 years of experience in {STACK}", location="Hyderabad, Telangana, India",
            key="acme", company="Acme", **extra):
    record = cp._record(key, company, number, title, location, f"https://careers.acme.example/job/{number}", description=description)
    record.update(extra)
    return record


# --- 1. a career-site record in the shared filters ---------------------------------------------------
job = posting(1)
check("career-site link is kept as the job link", core.clean_job_url(job) == "https://careers.acme.example/job/1", core.clean_job_url(job))
check("career-site record is an allowed source", core.source_is_allowed(job))
check("career-site record without an https link is refused", not core.source_is_allowed(dict(job, job_url="http://careers.acme.example/job/1", apply_url="")))
linkedin = {"source": "LinkedIn", "job_id": "4471386121", "job_url": "https://www.linkedin.com/jobs/view/4471386121/"}
check("LinkedIn records are judged as before", core.source_is_allowed(linkedin) and not core.source_is_allowed({"source": "x", "job_url": "https://evil.example/1"}))
check("the mail table links to the career site", 'href="https://careers.acme.example/job/1"' in core.html_table([core.score_job(dict(job))]))

# --- 2. strictly the candidate's experience -----------------------------------------------------------
NO_YEARS = f"We build products with {STACK}"
JUDGED = [
    ("2-4 years", posting(2), "match"),
    ("0-2 years includes 2", posting(2, description=f"Experience: 0-2 years\n{STACK}"), "match"),
    ("2+ years", posting(2, description=f"2+ years of experience in {STACK}"), "match"),
    ("years only in the title", posting(2, title="Python Developer (1-3 yrs)", description=NO_YEARS), "match"),
    ("stated experience, low skill match, still table 1", posting(2, description="1+ years of experience. Python, AWS, Kafka, Linux, Elasticsearch, Cassandra, GraphQL."), "match"),
    ("3+ years", posting(2, description=f"3+ years of experience in {STACK}"), "Experience"),
    ("5-8 years", posting(2, description=f"5-8 years of experience in {STACK}"), "Experience"),
    ("0-1 year", posting(2, description=f"Experience: 0-1 year\n{STACK}"), "Experience"),
    ("Senior title, no years", posting(2, title="Senior Python Developer", description=NO_YEARS), "Experience"),
    ("no years, good skill match -> table 2", posting(2, description=NO_YEARS), "no_yoe"),
    ("no years, weak skill match -> dropped", posting(2, description="Python scripting, AWS, Kafka, Terraform, Jenkins, Snowflake."), cp.BELOW_MATCH),
    ("Pune", posting(2, location="Pune, Maharashtra, India"), "Location"),
    ("remote in India", posting(2, location="Remote, India", workplace_type="Remote"), "match"),
    ("Java title", posting(2, title="Java Full Stack Developer"), "Different stack: Java"),
    ("internship", posting(2, employment_type="Internship"), "Not full-time"),
]
for name, record, expected in JUDGED:
    kind, reason = cp.judge(record, 2.0, 60)
    check(f"judge: {name}", (kind or reason) == expected, f"expected {expected!r}, got {(kind or reason)!r} ({record.get('skill_match_pct')}%)")


# --- 3. the readers, against answers shaped like each site's ------------------------------------------
class FakeSite:
    """Stands in for cp.Site: answers from a table of url fragment -> payload."""

    def __init__(self, answers, keywords=("Python",), candidate_yoe=2.0):
        self.answers, self.keywords, self.requests_made, self.asked, self.posted = answers, list(keywords), 0, [], []
        self.candidate_yoe = candidate_yoe

    def out_of_time(self):
        return False

    def _find(self, url, data=None):
        self.requests_made += 1
        self.asked.append(url)
        if data is not None:
            self.posted.append(data)
        for fragment, payload in self.answers.items():
            if fragment in url:
                if callable(payload):  # an answer that depends on the form sent
                    payload = payload(data)
                if isinstance(payload, Exception):
                    raise payload
                return payload
        raise cp.PortalError(f"HTTP 404 from {url}")

    def json(self, url, params=None, data=None):
        return self._find(url, data)

    def soup(self, url, params=None, data=None):
        return core.BeautifulSoup(self._find(url, data), "html.parser")

    def feed(self, url, params=None):
        self.asked.append(str(params))
        return cp.ElementTree.fromstring(self._find(url))


def one(jobs):
    return jobs[0] if jobs else {}


amazon = one(cp.read_amazon(FakeSite({"amazon.jobs": {"jobs": [{
    "id_icims": "10594150", "title": "Software Development Engineer", "normalized_location": "Hyderabad, Telangana, IND",
    "job_path": "/en/jobs/10594150/sde", "posted_date": "November 19, 2026", "job_schedule_type": "full-time",
    "description": "Build services.", "basic_qualifications": "- 1+ years of experience<br/>- Python", "preferred_qualifications": "- 5+ years of Java",
}]}})))
check("Amazon: id, link and place", (amazon.get("job_id"), amazon.get("job_url"), amazon.get("location")) ==
      ("amazon:10594150", "https://www.amazon.jobs/en/jobs/10594150/sde", "Hyderabad, Telangana, IND"), str(amazon))
check("Amazon: preferred qualifications do not set the experience", core.parse_experience_requirement(amazon) == (1.0, None), str(core.parse_experience_requirement(amazon)))

infor = cp.read_infor(FakeSite({"infor.com": {"data": [
    {"id": "1", "title": "Software Engineer", "url": "https://careers.infor.com/en/postings/a", "description": "<div>Python</div>",
     "skills_knowledge_expertise_header": "Basic Qualifications:", "skills_knowledge_expertise": "<ul><li>2-4 years of experience</li></ul>",
     "workplace_type_text": "Onsite", "employment_type_text": "Full Time", "location": {"city": "Hyderabad"},
     "job": {"structure_custom_group_one": {"name": "India"}}},
    {"id": "2", "title": "Software Engineer", "url": "https://careers.infor.com/en/postings/b", "description": "x", "location": {"city": "Barneveld"},
     "job": {"structure_custom_group_one": {"name": "Netherlands"}}},
]}}))
check("Infor: only the India posting, with its headings", len(infor) == 1 and infor[0]["location"] == "Hyderabad, India"
      and "Basic Qualifications:\n2-4 years of experience" in infor[0]["description"], str(infor))

tiger = cp.read_tiger_analytics(FakeSite({"workable.com": {"jobs": [
    {"shortcode": "AB12", "title": "AI Engineer", "url": "https://apply.workable.com/j/AB12", "description": "<p>Python</p>", "published_on": "2026-11-01",
     "telecommuting": False, "locations": [{"country": "India", "city": "Chennai", "region": "Tamil Nadu"}]},
    {"shortcode": "CD34", "title": "AI Engineer", "url": "https://apply.workable.com/j/CD34", "description": "x", "locations": [{"country": "Canada", "city": ""}]},
]}}))
check("Tiger Analytics: only the India posting", [j["job_id"] for j in tiger] == ["tiger:AB12"] and tiger[0]["location"] == "Chennai, Tamil Nadu, India", str(tiger))

capgemini = one(cp.read_capgemini(FakeSite({"job-search": {"count": 1, "data": [{
    "id": "547166-en_GB", "title": "Python Developer", "location": "Bangalore", "contract_type": "Permanent", "updated_at": "2026-11-16T11:14:22.000Z",
    "apply_job_url": "https://careers.capgemini.com/job/x/1/", "description": "<p>Experience: 2-4 years</p><ul><li>Python</li></ul>",
}]}})))
check("Capgemini: place, date and text", (capgemini.get("location"), capgemini.get("posted_time")) == ("Bangalore, India", "2026-11-16")
      and core.location_is_eligible(capgemini) and "Experience: 2-4 years" in capgemini.get("description", ""), str(capgemini))

jpmc_site = FakeSite({
    "recruitingCEJobRequisitions?": {"items": [{"requisitionList": [{
        "Id": "210810804", "Title": "Software Engineer II - Python", "PostedDate": "2026-11-20", "PrimaryLocation": "Hyderabad, Telangana, India",
        "secondaryLocations": [{"Name": "Bengaluru, Karnataka, India"}]}]}]},
    "recruitingCEJobRequisitionDetails": {"items": [{"ExternalDescriptionStr": "<p>Build services in Python.</p>",
                                                    "ExternalQualificationsStr": "<ul><li>2+ years applied experience</li></ul>"}]},
})
jpmc = one(cp.read_jpmc(jpmc_site))
fill = jpmc.pop("_detail", None)
check("JPMorgan Chase: listed without text, both places", jpmc.get("description") == "" and jpmc.get("location") == "Hyderabad, Telangana, India / Bengaluru, Karnataka, India" and fill is not None, str(jpmc))
if fill:
    fill(jpmc_site, jpmc)
check("JPMorgan Chase: the job page supplies the experience", core.parse_experience_requirement(jpmc) == (2.0, None), jpmc.get("description", ""))

NTT_LIST = ('<table><tr class="data-row"><td><span class="jobTitle"><a class="jobTitle-link" href="/job/Bangalore-Python-Developer-KA/1403960000/">'
            'Python Developer</a></span></td><td><span class="jobLocation"> Bangalore, KA, IN </span></td><td><span class="jobDate">Nov 20, 2026 </span></td></tr></table>')
ntt_site = FakeSite({"/search/": NTT_LIST, "/job/": '<span class="jobdescription"><p>Experience: 1-3 years</p><p>Python and Django</p></span>'})
ntt = one(cp.read_ntt_data(ntt_site))
fill = ntt.pop("_detail", None)
check("NTT DATA: id, place spelled out, date", (ntt.get("job_id"), ntt.get("location"), ntt.get("posted_time")) ==
      ("ntt:1403960000", "Bangalore, KA, India", "Nov 20, 2026") and core.location_is_eligible(ntt), str(ntt))
if fill:
    fill(ntt_site, ntt)
check("NTT DATA: the job page supplies the experience", core.parse_experience_requirement(ntt) == (1.0, 3.0), ntt.get("description", ""))

COGNIZANT_LIST = ('<div class="card card-job" data-id="00070617601"><h2 class="card-title"><a class="stretched-link js-view-job" '
                  'href="/global-en/jobs/00070617601/ai-engineer/">AI Engineer</a></h2><ul class="list-inline job-meta">'
                  '<li class="list-inline-item"> Hyderabad, Telangana, India </li><li class="list-inline-item"> Work from Office </li></ul></div>')
COGNIZANT_JOB = ('<div class="job-meta-item">Date published Oct 26 2026</div><div class="job-meta-item">Work model Hybrid</div>'
                 '<div class="cms-content"><p>2 to 4 years of experience</p><p>Python, LangChain</p></div>')
cognizant_site = FakeSite({"/global-en/jobs/?": COGNIZANT_LIST, "/global-en/jobs/0": COGNIZANT_JOB, "/global-en/jobs/": COGNIZANT_LIST})
cognizant = one(cp.read_cognizant(cognizant_site))
fill = cognizant.pop("_detail", None)
check("Cognizant: one posting from the three city searches", cognizant.get("job_id") == "cognizant:00070617601" and cognizant.get("location") == "Hyderabad, Telangana, India", str(cognizant))
if fill:
    fill(FakeSite({"/global-en/jobs/": COGNIZANT_JOB}), cognizant)
check("Cognizant: the job page supplies experience, date and work model", core.parse_experience_requirement(cognizant) == (2.0, 4.0)
      and cognizant.get("posted_time") == "Oct 26 2026" and cognizant.get("workplace_type") == "Hybrid", str(cognizant))
# Wipro and EY: a job feed, newest first, each item with its full text.
FEED = """<?xml version="1.0" encoding="UTF-8" ?><rss version="2.0"><channel><title>Custom Search</title>
<item><title><![CDATA[Engineer - Python Full Stack Development (Bengaluru, IND-29, IN, 560035)]]></title>
<description><![CDATA[<p>Experience: 1-3 Years</p><ul><li>Python, Django, React</li></ul>]]></description>
<pubDate>Fri, 20 Nov 2026 2:00:00 GMT</pubDate>
<link>https://careers.wipro.com/job/Bengaluru-Engineer-Python-Full-Stack-Development-IND-560035/1369349855/?feedId=null&amp;utm_source=J2WRSS</link></item>
<item><title><![CDATA[DEVELOPER L3(CONTRACT) (Hyderabad, IND-36, IN, 500032)]]></title><description><![CDATA[<p>Python</p>]]></description>
<pubDate>Sat, 21 Nov 2026 2:00:00 GMT</pubDate><link>https://careers.wipro.com/job/Hyderabad-DEVELOPER-L3%28CONTRACT%29-IND-500032/1340549955/?feedId=null</link></item>
<item><title><![CDATA[Python Developer (Warsaw, PL, 00-001)]]></title><description><![CDATA[<p>Python</p>]]></description>
<pubDate>Sat, 21 Nov 2026 2:00:00 GMT</pubDate><link>https://careers.wipro.com/job/Warsaw-Python-Developer/1340549956/</link></item>
<item><title><![CDATA[A title with no place]]></title><description>x</description><link>https://careers.wipro.com/job/x/1340549957/</link></item>
</channel></rss>"""
wipro_site = FakeSite({"careers.wipro.com/services/rss/job/": FEED}, keywords=("Python", "AI Engineer"))
wipro = cp.read_wipro(wipro_site)
first = one(wipro)
check("Wipro: one search per keyword, limited to the three cities", wipro_site.requests_made == 2
      and "(AI Engineer) AND locationSearch:(Hyderabad OR Secunderabad OR Bengaluru OR Bangalore OR Chennai)" in wipro_site.asked[-2], str(wipro_site.asked))
check("Wipro: id, link without tracking, place, date", (first.get("job_id"), first.get("job_url"), first.get("location"), first.get("posted_time")) ==
      ("wipro:1369349855", "https://careers.wipro.com/job/Bengaluru-Engineer-Python-Full-Stack-Development-IND-560035/1369349855/", "Bengaluru, India", "20 Nov 2026")
      and first.get("title") == "Engineer - Python Full Stack Development" and core.location_is_eligible(first), str(first))
check("Wipro: the feed supplies the text, so no job page is opened", "_detail" not in first and core.parse_experience_requirement(first) == (1.0, 3.0), first.get("description", ""))
check("Wipro: a contract post is not full-time", len(wipro) == 3 and wipro[1]["title"] == "DEVELOPER L3(CONTRACT)" and not core.employment_is_eligible(wipro[1])
      and core.employment_is_eligible(first), str([j["title"] for j in wipro]))
check("Wipro: a place outside India stays as written and is refused", wipro[2]["location"] == "Warsaw, PL, 00-001" and not core.location_is_eligible(wipro[2]), wipro[2]["location"])
ey = one(cp.read_ey(FakeSite({"careers.ey.com/services/rss/job/": FEED.replace("careers.wipro.com/job/", "careers.ey.com/ey/job/").replace("IND-29, IN", "KA, IN")})))
check("EY: same feed, its own id and link", (ey.get("job_id"), ey.get("company"), ey.get("location")) == ("ey:1369349855", "EY", "Bengaluru, India")
      and ey.get("job_url", "").startswith("https://careers.ey.com/ey/job/"), str(ey))
class BrokenFeed:
    """Stands in for cp.Site where the site answers a feed request with a web page."""

    def fetch(self, url, params=None, accept="", data=None):
        return argparse.Namespace(content=b"<html><body>Service unavailable<br></body></html>")


try:
    cp.Site.feed(BrokenFeed(), "https://careers.ey.com/services/rss/job/")
    unreadable = ""
except cp.PortalError as exc:
    unreadable = str(exc)
check("a feed that cannot be read is a site failure, not a crash", unreadable == "unreadable feed from https://careers.ey.com/services/rss/job/", unreadable)

# LTIMindtree: a list that states the experience, and a job page for the text.
ltim_site = FakeSite({
    "candidatejobsearch": {"totalJobCount": 2, "jobVoList": [
        {"jobSeq": "900911", "jobTitle": "Software Engineer", "jobReqExp": "1 - 3 Years", "locations": "Hyderabad, Kolkata"},
        {"jobSeq": "904067", "jobTitle": "Software Engineer", "jobReqExp": "5 - 7 Years", "locations": "Hyderabad"}]},
    "candidatejobdetail": {"jobVO": {"jobDesc": "<p>Build services in Python and Django.</p>", "jobSkills": "<b>Mandatory Skills : </b>Python, FastAPI<br/>",
                                     "jobPostingDate": "07-Oct-2026", "jobTypeCustom3": "Permanent"}},
})
ltim = cp.read_ltimindtree(ltim_site)
junior, senior = (ltim + [{}, {}])[:2]
fill = junior.pop("_detail", None)
check("LTIMindtree: one search per city spelling, one posting each", ltim_site.requests_made == 4 and len(ltim) == 2
      and '"location": "Bangalore"' in ltim_site.posted[-1]["careerSiteUrlParams"], f"{ltim_site.requests_made} {len(ltim)}")
check("LTIMindtree: id, places, link to the job", (junior.get("job_id"), junior.get("location")) == ("ltim:900911", "Hyderabad, Kolkata, India")
      and junior.get("job_url", "").endswith("#detail/job/900911") and core.location_is_eligible(junior), str(junior))
check("LTIMindtree: listed experience decides before the job page is opened", cp.listed_experience_excludes(senior, 2.0)
      and not cp.listed_experience_excludes(junior, 2.0) and not cp.listed_experience_excludes(dict(junior, experience=""), 2.0))
if fill:
    fill(ltim_site, junior)
check("LTIMindtree: the job page supplies text and date; the list supplies the experience", "Skills:\nMandatory Skills : Python, FastAPI" in junior.get("description", "")
      and junior.get("posted_time") == "07-Oct-2026" and core.parse_experience_requirement(junior) == (1.0, 3.0) and core.employment_is_eligible(junior), str(junior))

# Tech Mahindra: a search form, and a job page that opens only through the job's button.
TECHM_FORM = '<form><input type="hidden" name="__VIEWSTATE" value="{state}"/><input type="text" name="ctl00$ContentPlaceHolder1$txtAdvanceSearch" value=""/>{cards}</form>'
TECHM_CARDS = "".join(
    f'<div><input type="hidden" name="ctl00$ContentPlaceHolder1$DataListRecommended$ctl0{n}$HdnJobCode" value="{code}"/><span>IT</span>'
    f'<div>{title}</div><p><b>Skill Set</b> : Python<br/><b>Experience</b> : {years} Years<br/><b>Location</b> : {place}</p>'
    f'<input type="submit" name="ctl00$ContentPlaceHolder1$DataListRecommended$ctl0{n}$btn_jobs" value="Apply/Shortlist"/></div>'
    for n, (code, title, years, place) in enumerate([("102122", "Software Engineer", "2.00-4.00", "HYDERABAD"), ("101988", "Tech Lead", "7.00-10.00", "SAN JOSE")]))
TECHM_JOB = ('<label id="ctl00_ContentPlaceHolder1_lblDesignationName">{title}</label><div class="job-details"><ul>'
             '<li><b>Skill Set :</b><span>Python, Django</span></li><li><b>Total Experience :</b><span>2.00 to 4.00 Years</span></li>'
             '<li><b>Job Post Date :</b><span>16/11/2026</span></li><li><b>Location :</b><span>HYDERABAD [India]</span></li>'
             '<li><b>Job Reference No :</b><span>4118173</span></li></ul></div><div><h3>Job Summary</h3><p>Build REST APIs in Python.</p></div>')
techm_pressed = []


def techm_page(data):
    if data is None:
        return TECHM_FORM.format(state="home", cards="")
    button = [name for name in data if name.endswith("btn_jobs")]
    if button:
        techm_pressed.append((button[0], data.get("__VIEWSTATE")))
        return TECHM_FORM.format(state="results", cards=TECHM_CARDS) + "<script>window.open('JobDetails.aspx', '_blank');</script>"
    return TECHM_FORM.format(state="results", cards=TECHM_CARDS)


techm_site = FakeSite({"CurrentOpportunity.aspx": techm_page, "JobDetails.aspx": TECHM_JOB.format(title="Software Engineer")})
techm = cp.read_tech_mahindra(techm_site)
opening, abroad = (techm + [{}, {}])[:2]
fill = opening.pop("_detail", None)
check("Tech Mahindra: the search form is sent with the keyword", techm_site.posted and techm_site.posted[0].get("ctl00$ContentPlaceHolder1$txtAdvanceSearch") == "Python"
      and techm_site.posted[0].get("__VIEWSTATE") == "home", str(techm_site.posted[:1]))
check("Tech Mahindra: id, place and listed experience", (opening.get("job_id"), opening.get("location"), opening.get("experience")) ==
      ("techm:102122", "Hyderabad", "2.00-4.00 Years") and core.location_is_eligible(opening) and not core.location_is_eligible(abroad)
      and cp.listed_experience_excludes(abroad, 2.0) and not cp.listed_experience_excludes(opening, 2.0), str(opening))
if fill:
    fill(techm_site, opening)
check("Tech Mahindra: the job's own button is pressed on its results page", techm_pressed == [("ctl00$ContentPlaceHolder1$DataListRecommended$ctl00$btn_jobs", "results")], str(techm_pressed))
check("Tech Mahindra: the job page supplies text, place, date and the reference to search for", "Build REST APIs in Python." in opening.get("description", "")
      and opening.get("location") == "Hyderabad, India" and opening.get("posted_time") == "16/11/2026"
      and opening.get("freshness_badge") == "16/11/2026; search job reference 4118173 on the page"
      and core.parse_experience_requirement(opening) == (2.0, 4.0), str(opening))
other = dict(opening, title="Tech Lead", description="")
try:
    cp._techm_detail({}, "x$btn_jobs")(techm_site, other)
    refused = False
except cp.PortalError:
    refused = True
check("Tech Mahindra: a job page that is another job's is not used", refused and other["description"] == "")
try:
    cp._techm_detail({}, "x$btn_jobs")(FakeSite({"CurrentOpportunity.aspx": TECHM_FORM.format(state="results", cards=""), "JobDetails.aspx": TECHM_JOB.format(title="Tech Lead")}), other)
    refused = False
except cp.PortalError:
    refused = True
check("Tech Mahindra: no job page is read when the button did not open one", refused and other["description"] == "")

infosys_site = FakeSite({"getCareerSearchJobs": [
    {"referenceCode": "INFSYS-EXTERNAL-256133", "requisitionId": 256133, "postingTitle": "Python ML_GenAI Engineers", "location": "CHENNAI", "country": "India",
     "minExperienceLevel": 1, "maxExperienceLevel": 2, "createdOn": "2026-11-04T10:15:00.12", "postingDescription": "",
     "rolesResponsibilities": "Build GenAI services.\n• Own the API layer", "technicalRequirement": "Python, SQL, FastAPI",
     "preferredSkills": "Technology->Machine Learning->Python", "additionalResponsibility": "", "educationalRequirement": "Bachelor of Engineering"},
    {"referenceCode": "INFSYS-EXTERNAL-256200", "postingTitle": "Technology Lead", "location": "Kolkata(WB)", "country": "India",
     "minExperienceLevel": 5, "maxExperienceLevel": 8, "rolesResponsibilities": "Lead the team."},
    {"referenceCode": "INFSYS-EXTERNAL-256201", "postingTitle": "Python Developer", "location": "PUNE                     ", "country": "India",
     "minExperienceLevel": 0, "maxExperienceLevel": 0, "rolesResponsibilities": "Python."},
    {"postingTitle": "A posting with no reference"},
]})
infosys = cp.read_infosys(infosys_site)
junior = one(infosys)
check("Infosys: one request for the whole list; a posting with no reference is skipped", infosys_site.requests_made == 1 and len(infosys) == 3, str(len(infosys)))
check("Infosys: id, link, place and date", (junior.get("job_id"), junior.get("job_url"), junior.get("location"), junior.get("posted_time")) ==
      ("infosys:INFSYS-EXTERNAL-256133", "https://career.infosys.com/jobdesc?jobReferenceCode=INFSYS-EXTERNAL-256133", "Chennai, India", "2026-11-04")
      and core.location_is_eligible(junior), str(junior))
check("Infosys: the text keeps its headings and lines", "Responsibilities:\nBuild GenAI services.\n• Own the API layer" in junior.get("description", "")
      and "Preferred skills:\nTechnology->Machine Learning->Python" in junior.get("description", ""), junior.get("description", ""))
check("Infosys: the listed years decide before the text is judged", junior.get("experience") == "1 - 2 Years" and not cp.listed_experience_excludes(junior, 2.0)
      and len(infosys) == 3 and infosys[1]["location"] == "Kolkata(WB), India" and cp.listed_experience_excludes(infosys[1], 2.0)
      and "experience" not in infosys[2] and infosys[2]["location"] == "Pune, India", str([j.get("experience") for j in infosys]))
try:
    cp.read_infosys(FakeSite({"getCareerSearchJobs": {"error": "down"}}))
    refused = False
except cp.PortalError:
    refused = True
check("Infosys: an answer that is not a job list is a site failure", refused)

ACCENTURE_JOB = {
    "requisitionId": "ATCI-5729291-S2065618", "title": "Custom Software Engineer", "location": ["Bengaluru", "Chennai"], "country": "India",
    "jobDetailUrl": "https://www.accenture.com/{0}/careers/jobdetails?id=ATCI-5729291-S2065618_en&title=Custom+Software+Engineer",
    "jobDescription": "<b>Project Role : </b>Custom Software Engineer<br><b>Must have skills : </b>Python (Programming Language)<br>Minimum 2 year(s) of experience is required",
    "updateDate": "2026-11-20T08:47:02.450-08:00", "employeeType": "Full-time", "remoteType": "", "yearsOfExperience": "Experience: 2-5 years",
}
accenture_site = FakeSite({"elastic/findjobs": {"data": [
    ACCENTURE_JOB, {**ACCENTURE_JOB, "requisitionId": ""}, {**ACCENTURE_JOB, "requisitionId": "X-1", "jobDetailUrl": "javascript:void(0)"},
]}}, keywords=("Python", "AI Engineer"))
accenture = cp.read_accenture(accenture_site)
job = one(accenture)
sent = accenture_site.posted[-1] if accenture_site.posted else {}
check("Accenture: one search per keyword; a posting with no id or no link is skipped", accenture_site.requests_made == 2 and len(accenture) == 1, str(len(accenture)))
check("Accenture: newest first, the three cities, the bands that hold 2 years", (sent.get("jobKeyword"), sent.get("sortBy"), sent.get("jobCountry")) == ("AI Engineer", 1, "India")
      and json.loads(sent.get("jobFilters", "[]")) == [
          {"fieldName": "location.keyword", "items": ["Hyderabad", "Bengaluru", "Chennai"], "multiSelect": False},
          {"fieldName": "yearsOfExperience.keyword", "items": ["Experience: 0-2 years", "Experience: 2-5 years"], "multiSelect": False}], str(sent))
check("Accenture: id, link, places and date", (job.get("job_id"), job.get("job_url"), job.get("location"), job.get("posted_time")) ==
      ("accenture:ATCI-5729291-S2065618", "https://www.accenture.com/in-en/careers/jobdetails?id=ATCI-5729291-S2065618_en&title=Custom+Software+Engineer",
       "Bengaluru / Chennai, India", "2026-11-20") and core.location_is_eligible(job) and core.employment_is_eligible(job), str(job))
check("Accenture: the text states the years; the band is not taken as the posting's own figure",
      core.parse_experience_requirement(job) == (2.0, None) and "experience" not in job, str(core.parse_experience_requirement(job)))
senior_site = FakeSite({"elastic/findjobs": {"data": []}}, candidate_yoe=6.0)
cp.read_accenture(senior_site)
check("Accenture: the bands follow the candidate's years", json.loads(senior_site.posted[-1]["jobFilters"])[1]["items"] == ["Experience: 5-10 years"], str(senior_site.posted[-1]))

# Job boards: a company's whole list from its hiring system's public API.
greenhouse = cp._read_greenhouse(FakeSite({"boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [
    {"id": 71, "title": "Software Engineer", "location": {"name": "Bengaluru"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/71",
     "content": "&lt;p&gt;2-4 years of experience.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; Django&lt;/li&gt;&lt;/ul&gt;", "first_published": "2026-11-18T06:21:15-04:00",
     "metadata": [{"name": "Employment Type", "value": "Contract"}]},
    {"id": 72, "title": "Software Engineer", "location": {"name": "Indianapolis, IN, United States"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/72", "content": "x"},
    {"id": 73, "title": "Software Engineer", "location": {"name": "Remote - India"}, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/73", "content": "x", "metadata": None},
]}}), "acme", "Acme", "acme")
check("Greenhouse: only the India postings; 'Indianapolis, IN' is not India", [j["job_id"] for j in greenhouse] == ["acme:71", "acme:73"]
      and [j["location"] for j in greenhouse] == ["Bengaluru, India", "Remote - India"], str([(j["job_id"], j["location"]) for j in greenhouse]))
check("Greenhouse: the escaped text is read, with its date and its employment type",
      greenhouse and greenhouse[0]["description"] == "2-4 years of experience.\nPython & Django" and greenhouse[0]["posted_time"] == "2026-11-18"
      and not core.employment_is_eligible(greenhouse[0]) and core.employment_is_eligible(greenhouse[1]), repr(greenhouse and greenhouse[0]["description"]))

lever = cp._read_lever(FakeSite({"api.lever.co/v0/postings/acme": [
    {"id": "aa-1", "text": "backend developer", "country": "IN", "workplaceType": "hybrid", "createdAt": 1795000000000,
     "categories": {"commitment": "part time", "location": "hyderabad", "allLocations": ["hyderabad", "chennai"]},
     "hostedUrl": "https://jobs.lever.co/acme/aa-1", "description": "<div>Build services.</div>",
     "lists": [{"text": "What you need", "content": "<li>3+ years in Python</li>"}], "additional": ""},
    {"id": "aa-2", "text": "backend developer", "country": "US", "categories": {"location": "New York"}, "hostedUrl": "https://jobs.lever.co/acme/aa-2"},
]}), "acme", "Acme", "acme")
check("Lever: India by the country code, every place named, sections kept", len(lever) == 1 and lever[0]["location"] == "hyderabad / chennai, India"
      and lever[0]["description"] == "Build services.\n\nWhat you need:\n3+ years in Python" and lever[0]["workplace_type"] == "Hybrid"
      and lever[0]["job_url"] == "https://jobs.lever.co/acme/aa-1", str(lever))
check("Lever: 'part time' written without a hyphen is still not full-time", lever and not core.employment_is_eligible(lever[0]), str(lever and lever[0]["employment_type"]))
try:
    cp._read_lever(FakeSite({"api.lever.co": {"ok": False}}), "acme", "Acme", "acme")
    refused = False
except cp.PortalError:
    refused = True
check("Lever: an answer that is not a list is a site failure", refused)

ashby = cp._read_ashby(FakeSite({"api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [
    {"id": "u-1", "title": "AI Engineer", "location": "Delhi", "secondaryLocations": [{"location": "Singapore"}, {"location": "Pune"}], "isListed": True,
     "address": {"postalAddress": {"addressCountry": "India"}}, "employmentType": "FullTime", "isRemote": False, "workplaceType": "OnSite",
     "jobUrl": "https://jobs.ashbyhq.com/acme/u-1", "descriptionHtml": "<p>1-3 years.</p><p>Python</p>", "publishedAt": "2026-04-17T11:46:39.214+00:00"},
    {"id": "u-2", "title": "AI Engineer", "location": "Bengaluru", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/u-2", "descriptionHtml": "x"},
    {"id": "u-3", "title": "AI Engineer", "location": "London", "isListed": True, "address": {"postalAddress": {"addressCountry": "United Kingdom"}},
     "jobUrl": "https://jobs.ashbyhq.com/acme/u-3", "descriptionHtml": "x"},
]}}), "acme", "Acme", "acme")
check("Ashby: India places only, an unlisted posting is left alone", len(ashby) == 1 and ashby[0]["location"] == "Delhi, India / Pune, India"
      and ashby[0]["employment_type"] == "Full-time" and ashby[0]["workplace_type"] == "On-site" and ashby[0]["posted_time"] == "2026-04-17", str(ashby))

smart_site = FakeSite({
    "/postings/744": {"postingUrl": "https://jobs.smartrecruiters.com/Acme/744-python-developer", "jobAd": {"sections": {
        "companyDescription": {"text": "<p>We have 30 years of experience in travel.</p>"}, "jobDescription": {"text": "<p>Build APIs in Python.</p>"},
        "qualifications": {"text": "<ul><li>2+ years of experience</li></ul>"}, "additionalInformation": {"text": ""}}}},
    "/companies/Acme/postings": {"totalFound": 1, "content": [{"id": "744", "name": "Python Developer", "releasedDate": "2026-11-17T05:14:27.193Z",
                                 "location": {"city": "Gurugram", "country": "in", "remote": False, "hybrid": True}, "typeOfEmployment": {"label": "Full-time"}}]},
})
smart = one(cp._read_smartrecruiters(smart_site, "acme", "Acme", "Acme"))
fill = smart.pop("_detail", None)
check("SmartRecruiters: listed without text, in India, with a link", (smart.get("job_id"), smart.get("location"), smart.get("description"), smart.get("workplace_type")) ==
      ("acme:744", "Gurugram, India", "", "Hybrid") and smart.get("job_url") == "https://jobs.smartrecruiters.com/Acme/744" and fill is not None, str(smart))
if fill:
    fill(smart_site, smart)
check("SmartRecruiters: the job page gives the text (not the company's own history) and the full link",
      smart.get("description") == "Build APIs in Python.\n\nQualifications:\n2+ years of experience"
      and smart.get("job_url") == "https://jobs.smartrecruiters.com/Acme/744-python-developer" and core.parse_experience_requirement(smart) == (2.0, None), smart.get("description", ""))
keys = [board[0] for board in cp.BOARDS]
check("job boards: every company has its own key, a known hiring system, and is scanned",
      len(keys) == len(set(keys)) and all(board[2] in cp._BOARD_READERS for board in cp.BOARDS) and set(keys) <= set(cp.PORTALS)
      and all(key.isalnum() and key.islower() for key in keys))

check("the companies asked for are either read or named as not covered",
      {"ntt", "amazon", "cognizant", "tiger", "infor", "jpmc", "capgemini", "ltim", "ey", "techm", "wipro", "infosys", "accenture"} <= set(cp.PORTALS)
      and len(cp.PORTALS) == 13 + len(cp.BOARDS)
      and set(cp.NOT_COVERED) == {"EPAM", "Cimpress", "Sify", "Hetero"})


# --- 4. a scan: what is mailed, and when ----------------------------------------------------------------
work = Path(tempfile.mkdtemp(prefix="jmr_portals_"))
cp.STATE_FILE = work / "company_portal_state.json"
core.DATA_DIR = work
core.REPORT_DIR = work / "reports"
cp.Site = lambda keywords, candidate_yoe=2.0: FakeSite({}, keywords, candidate_yoe)
sent = {"mail": [], "push": []}
outcome = {"mail": True, "push": True}
core.send_gmail = lambda subject, body, attachment=None: (sent["mail"].append((subject, body)), outcome["mail"])[1]
core.send_push_notification = lambda *a, **k: (sent["push"].append(a), outcome["push"])[1]
listings = {"acme": [], "globex": []}
opened = []


def reader(key):
    def read(site):
        if isinstance(listings[key], Exception):
            raise listings[key]
        return [dict(j) for j in listings[key]]
    return read


cp.PORTALS = {"acme": ("Acme", reader("acme")), "globex": ("Globex", reader("globex"))}


def scan(dry_run=False):
    sent["mail"].clear()
    sent["push"].clear()
    args = argparse.Namespace(candidate_yoe=2.0, min_match=60, dry_run=dry_run, company_list=list(cp.PORTALS), keyword_list=["Python"])
    return cp.run_scan(args)


fits = posting(100)
too_senior = posting(101, description=f"5+ years of experience in {STACK}")
no_years = posting(102, title="Backend Developer", description=NO_YEARS)
elsewhere = posting(103, location="Pune, Maharashtra, India")
listings["acme"] = [fits, too_senior, no_years, elsewhere]

code = scan(dry_run=True)
check("dry run: nothing sent, nothing remembered, preview written", code == 0 and not sent["mail"] and not sent["push"] and not cp.STATE_FILE.exists()
      and (core.REPORT_DIR / "company_portals_preview.html").exists(), f"{code} {sent}")

code = scan()
subject, body = sent["mail"][0] if sent["mail"] else ("", "")
check("first scan: one mail and one push", code == 0 and len(sent["mail"]) == 1 and len(sent["push"]) == 1, f"{code} {len(sent['mail'])} {len(sent['push'])}")
check("first scan: subject counts both tables", "1 new job(s) for 2 years' experience, 1 with no experience stated" in subject, subject)
check("first scan: table 1 has the 2-4 years job, table 2 the one with no years", "/job/100" in body and "/job/102" in body
      and body.index("/job/100") < body.index("2. Experience not stated") < body.index("/job/102"), body[:200])
check("first scan: the 5+ years job and the Pune job are not in the mail", "/job/101" not in body and "/job/103" not in body)
check("first scan: companies not covered are named", "EPAM is not covered" in body and "Cimpress is not covered" in body)

code = scan()
check("same postings again: no mail", code == 0 and not sent["mail"] and not sent["push"], str(sent))

listings["acme"] = [fits, too_senior, no_years, elsewhere, posting(104, title="AI Engineer", description=f"1-3 years of experience in {STACK}", company="Acme")]
code = scan()
body = sent["mail"][0][1] if sent["mail"] else ""
check("one new posting: one mail with only that one", len(sent["mail"]) == 1 and "/job/104" in body and "/job/100" not in body and "/job/102" not in body, body[:200])

listings["acme"] = listings["acme"] + [posting(105, title="AI Engineer", description=f"1-3 years of experience in {STACK}", company="Acme",
                                               location="Chennai, Tamil Nadu, India")]
code = scan()
check("the same opening re-posted for another city is not mailed again", not sent["mail"], str([s[0] for s in sent["mail"]]))

listings["acme"] = cp.PortalError("HTTP 503 from https://careers.acme.example")
listings["globex"] = [posting(200, key="globex", company="Globex", title="Full Stack Developer")]
code = scan()
body = sent["mail"][0][1] if sent["mail"] else ""
check("one site down: the other is still reported, and the mail says which failed", code == 0 and "/job/200" in body
      and "Acme could not be read this run" in body, f"{code} {body[:200]}")
check("one site down: counted", cp.load_state()["failures"].get("acme") == 1, str(cp.load_state()["failures"]))

listings["globex"] = cp.PortalError("HTTP 503 from https://careers.globex.example")
code = scan()
check("every site down: run fails, no mail", code == 1 and not sent["mail"], f"{code} {sent['mail']}")

listings["acme"] = [fits]
listings["globex"] = [posting(201, key="globex", company="Globex", description=f"2+ years of experience in {STACK}")]
outcome.update(mail=False, push=False)
code = scan()
check("mail and push both fail: run fails and the opening stays queued", code == 1 and len(cp.load_state()["queue"]) == 1, f"{code} {cp.load_state()['queue']}")
outcome.update(mail=True, push=True)
code = scan()
check("next scan delivers the queued opening once", code == 0 and len(sent["mail"]) == 1 and "/job/201" in sent["mail"][0][1] and not cp.load_state()["queue"], str(sent["mail"])[:200])
check("a site that works again resets its count", cp.load_state()["failures"] == {"acme": 0, "globex": 0}, str(cp.load_state()["failures"]))


# A posting whose text needs its own request: opened only when title and place fit, and not twice.
def with_page(record, text):
    def fill(site, job):
        opened.append(job["job_id"])
        job["description"] = text
    return dict(record, description="", _detail=fill)


listings["globex"] = [
    with_page(posting(300, key="globex", company="Globex", title="LLM Engineer"), f"2-3 years of experience in {STACK}"),
    with_page(posting(301, key="globex", company="Globex", title="QA Tester"), "anything"),
    with_page(posting(302, key="globex", company="Globex", location="Mumbai, Maharashtra, India"), "anything"),
    with_page(posting(303, key="globex", company="Globex", title="Django Developer"), ""),
]
code = scan()
check("job pages: only the posting with a fitting title and place is opened", opened == ["globex:300", "globex:303"], str(opened))
check("job pages: the opened posting is mailed", len(sent["mail"]) == 1 and "/job/300" in sent["mail"][0][1])
opened.clear()
code = scan()
check("job pages: a page that came back empty is tried again; a judged one is not", opened == ["globex:303"] and not sent["mail"], str(opened))

listings["globex"] = [
    with_page(posting(310, key="globex", company="Globex", experience="5 - 7 Years"), f"We build with {STACK}"),
    with_page(posting(311, key="globex", company="Globex", title="Backend Developer", experience="1 - 3 Years"), f"We build with {STACK}"),
]
opened.clear()
code = scan()
body = sent["mail"][0][1] if sent["mail"] else ""
check("listed experience: a posting listed at 5-7 years is dropped without opening its page", opened == ["globex:311"], str(opened))
check("listed experience: the one listed at 1-3 years is mailed in table 1", "/job/311" in body and "/job/310" not in body
      and body.index("/job/311") < body.index("2. Experience not stated"), body[:200])
opened.clear()
code = scan()
check("listed experience: the dropped posting is not looked at again", opened == [] and not sent["mail"], str(opened))

cp.MAX_DETAIL_PAGES = 2
opened.clear()
listings["acme"] = [with_page(posting(400 + n, title="Python Developer", company=f"Acme {n}"), f"2-4 years of experience in {STACK} Team {n}.") for n in range(5)]
listings["globex"] = []
code = scan()
check("job pages: at most MAX_DETAIL_PAGES per site per run; the mail says the rest are waiting", len(opened) == 2
      and sent["mail"] and "3 posting(s) are still to be opened" in sent["mail"][0][1], f"{opened} {[s[0] for s in sent['mail']]}")
code = scan()
check("job pages: the next run carries on", len(opened) == 4, str(opened))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
