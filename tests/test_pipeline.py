"""Offline smoke test of the runner's decision and notification paths.

Usage: python tests/test_pipeline.py [<repo_dir>]   (default: the folder above tests/)
Nothing is sent: mail, push, issue creation and the seen-job file are replaced by recorders.
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("jmr", os.path.join(repo_dir, "job_match_runner.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


# --- 1. the user's table -------------------------------------------------------------------------
TABLE = [
    ("0 - 2 yrs", True), ("1 - 2 yrs", True), ("2+", True), ("2 - 4 yrs", True), ("2 - 10 yrs", True),
    ("0 - 3 yrs", True), ("1 - 3 yrs", True), ("0 - 1 yr", False), ("3+", False), ("4+", False), ("5+", False),
    ("3 - 5 yrs", False),
]
for text, expected in TABLE:
    for where in ("title", "description", "label"):
        if where == "title":
            job = {"title": f"Python Developer ({text if 'yr' in text else text + ' yrs'})", "description": "Django, REST APIs."}
        elif where == "description":
            job = {"title": "Python Developer", "description": f"We need {text if 'yr' in text else text + ' years'} of experience in Python."}
        else:
            job = {"title": "Python Developer", "description": f"Experience: {text}\nLocation: Hyderabad"}
        got = m.experience_is_eligible(job, candidate_yoe=2.0)
        check(f"table {text!r} in {where}", got == expected, f"expected {expected}, got {got}, parsed {m.parse_experience_requirement(job)}")

check("nothing stated -> eligible", m.experience_is_eligible({"title": "Python Developer", "description": "Django and REST."}))
check("director seniority rejected", not m.experience_is_eligible({"title": "x", "description": "2+ years", "seniority_level": "Director"}))


# --- 2. filters and reasons ----------------------------------------------------------------------
def job(job_id, title, description, location="Hyderabad, Telangana, India", **extra):
    base = {
        "job_id": str(job_id), "source": "LinkedIn", "title": title, "company": "Acme", "location": location,
        "description": description, "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "workplace_type": "On-site", "employment_type": "Full-time", "age_hours": 0.4, "freshness_badge": "<1h ago (24m)",
    }
    base.update(extra)
    return base


good = job(1, "Python Developer", "2-4 years of experience in Python, Django, React, PostgreSQL, REST APIs, Docker.")
senior = job(2, "Python Developer", "Minimum 5 years of experience in Python and Django.")
fresher = job(3, "Python Developer", "Experience: 0-1 year. Python basics.")
pune = job(4, "Python Developer", "2+ years of Python.", location="Pune, Maharashtra, India")
no_jd = job(5, "Python Developer", "")
no_jd_title = job(6, "Python Developer (2-4 yrs)", "")
qa = job(7, "QA Test Engineer", "2+ years of testing.")
flexible = job(8, "Software Engineer", "Bachelor's degree or equivalent practical experience. 5 years of experience with software development.")

check("good job passes", m.rejection_reason(dict(good)) == "", m.rejection_reason(dict(good)))
check("5+ rejected for experience", m.rejection_reason(dict(senior)) == "Experience", m.rejection_reason(dict(senior)))
check("0-1 rejected for experience", m.rejection_reason(dict(fresher)) == "Experience", m.rejection_reason(dict(fresher)))
check("Pune rejected for location", m.rejection_reason(dict(pune)) == "Location", m.rejection_reason(dict(pune)))
check("no description -> not checked", m.rejection_reason(dict(no_jd)) == m.DESCRIPTION_UNAVAILABLE, m.rejection_reason(dict(no_jd)))
check("no description but title states 2-4 -> passes", m.rejection_reason(dict(no_jd_title)) == "", m.rejection_reason(dict(no_jd_title)))
check("QA title rejected", m.rejection_reason(dict(qa)) == "Job title", m.rejection_reason(dict(qa)))
check("'equivalent practical experience' no longer rescues a 5-year role", m.rejection_reason(dict(flexible)) == "Experience", m.rejection_reason(dict(flexible)))


# --- 2a. senior titles, skill score, search defaults ----------------------------------------------
senior_no_years = job(20, "Senior Software Engineer", "Build Python services with Django and PostgreSQL.")
senior_with_years = job(21, "Senior Python Developer", "2-4 years of experience in Python and Django.")
lead_no_years = job(22, "Lead Backend Developer", "Python, FastAPI, Redis.")
check("Senior title, no years stated -> rejected", m.rejection_reason(dict(senior_no_years)) == "Experience", m.rejection_reason(dict(senior_no_years)))
check("Lead title, no years stated -> rejected", m.rejection_reason(dict(lead_no_years)) == "Experience", m.rejection_reason(dict(lead_no_years)))
check("Senior title that states 2-4 years -> accepted", m.rejection_reason(dict(senior_with_years)) == "", m.rejection_reason(dict(senior_with_years)))
java_job = m.score_job({"title": "Software Engineer", "description": "Core Java, Spring Boot, Angular, Oracle database, Jenkins, microservices. Python scripting is a plus."})
python_job = m.score_job({"title": "AI Engineer", "description": "Python, Django, FastAPI, React, PostgreSQL, Redis, Docker, RAG pipelines with LangChain and Azure OpenAI, LLM applications."})
check("Java / Spring / Angular role scores below 60%", java_job["skill_match_pct"] < 60, f"{java_job['skill_match_pct']} {java_job['missing_skills']}")
check("Python / Django / AI role scores 60% or more", python_job["skill_match_pct"] >= 60, f"{python_job['skill_match_pct']} {python_job['missing_skills']}")
check("'Azure OpenAI' and 'LLM' are not counted as missing skills", not {"azure", "llm"} & set(python_job["missing_skills"]), str(python_job["missing_skills"]))
check("javascript is not mistaken for java", "java" not in m.canonical_skill_hits("JavaScript, TypeScript and React"), str(m.canonical_skill_hits("JavaScript, TypeScript and React")))
check("search covers the three cities and India-remote", m.SEARCH_LOCATIONS == ["Hyderabad", "Bengaluru", "Chennai", "Remote"], str(m.SEARCH_LOCATIONS))


# --- 2b. who gets assigned on the notification issue ---------------------------------------------
import subprocess  # noqa: E402

for var in ("ISSUE_OWNER", "GITHUB_REPOSITORY_OWNER", "GITHUB_ACTOR"):
    os.environ.pop(var, None)
os.environ["GITHUB_ACTOR"] = "github-actions[bot]"
check("issue owner: a bot actor is never used", m.issue_owner() == "", m.issue_owner())
os.environ["GITHUB_REPOSITORY_OWNER"] = "octo-owner"
check("issue owner: falls back to the repository owner", m.issue_owner() == "octo-owner", m.issue_owner())
os.environ["ISSUE_OWNER"] = "explicit-owner"
check("issue owner: explicit variable wins", m.issue_owner() == "explicit-owner", m.issue_owner())
check("issue body mentions the owner, not the bot", "cc @explicit-owner" in m.build_github_issue_body([], []) and "[bot]" not in m.build_github_issue_body([], []))
captured_cmd = {}


def fake_run(cmd, **kwargs):
    captured_cmd["cmd"] = cmd

    class Result:
        stdout = "https://example.invalid/issues/1\n"
    return Result()


real_subprocess_run = subprocess.run
subprocess.run = fake_run
real_report_dir = m.REPORT_DIR
m.REPORT_DIR = Path(tempfile.mkdtemp(prefix="jmr_issue_"))
os.environ.update({"GITHUB_TOKEN": "x", "GITHUB_REPOSITORY": "octo-owner/repo"})
url = m.create_github_issue("title", "body")
subprocess.run = real_subprocess_run
m.REPORT_DIR = real_report_dir
cmd = captured_cmd.get("cmd", [])
check("issue is assigned to the owner on a timer-started run", "--assignee" in cmd and cmd[cmd.index("--assignee") + 1] == "explicit-owner", str(cmd))
for var in ("ISSUE_OWNER", "GITHUB_REPOSITORY_OWNER", "GITHUB_ACTOR", "GITHUB_TOKEN", "GITHUB_REPOSITORY"):
    os.environ.pop(var, None)


# --- 2c. search-result cards: posting age and job id ------------------------------------------------
AGES = [
    ("35 minutes ago", 35 / 60), ("2 hours ago", 2.0), ("1 day ago", 24.0), ("2 weeks ago", 336.0),
    ("1 month ago", 730.0), ("3 months ago", 2190.0), ("Reposted 1 month ago", 730.0), ("1 year ago", 8760.0),
    ("30+ days ago", 720.0), ("5m", 5 / 60), ("2mo", 1460.0), ("just now", 0.0),
]
for label, expected in AGES:
    got = m._parse_linkedin_age(label)
    check(f"age label {label!r}", got is not None and abs(got - expected) < 0.01, f"expected {expected}, got {got}")
check("age label that says nothing -> unknown", m._parse_linkedin_age("Be an early applicant") is None)


def card_html(job_id, title, slug, posted, urn=True):
    urn_attr = f' data-entity-urn="urn:li:jobPosting:{job_id}"' if urn else ""
    return (
        f'<li><div class="base-card job-search-card"{urn_attr}>'
        f'<a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/{slug}-{job_id}?position=1&amp;pageNum=0"></a>'
        f'<h3 class="base-search-card__title">{title}</h3><h4 class="base-search-card__subtitle">Acme</h4>'
        f'<span class="job-search-card__location">Hyderabad, Telangana, India</span>'
        f'<time class="job-search-card__listdate" datetime="2020-01-01">{posted}</time></div></li>'
    )


def first_card(markup):
    return m.BeautifulSoup(markup, "html.parser").find("li")


with_urn = first_card(card_html("4471386121", "Software Engineer - Req 31001234", "software-engineer-req-31001234-at-acme", "5 minutes ago"))
without_urn = first_card(card_html("4471386122", "Python Developer JR10012345", "python-developer-jr10012345-at-acme", "5 minutes ago", urn=False))
check("job id: taken from the card, not from a number in the title", m._card_job_id(with_urn, with_urn.find("a")["href"]) == "4471386121",
      m._card_job_id(with_urn, with_urn.find("a")["href"]))
check("job id: without the card attribute, the number that ends the link", m._card_job_id(without_urn, without_urn.find("a")["href"]) == "4471386122",
      m._card_job_id(without_urn, without_urn.find("a")["href"]))
check("job id: canonical link", m._card_job_id(without_urn, "https://www.linkedin.com/jobs/view/4471386123/?trk=x") == "4471386123")

PAGE = (
    card_html("4471386121", "Software Engineer - Req 31001234", "software-engineer-req-31001234-at-acme", "12 minutes ago")
    + card_html("4471386122", "Python Developer JR10012345", "python-developer-jr10012345-at-acme", "40 minutes ago", urn=False)
    + card_html("4471386123", "Python Developer", "python-developer-at-acme", "1 month ago")
    + card_html("4471386124", "Backend Developer", "backend-developer-at-acme", "30+ days ago")
)


class FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, text):
        self.text = text


class FakeSession:
    proxies = {}

    def get(self, url, **kwargs):
        # Cards on the first page of the "Normal Apply" pass only.
        return FakeResponse(PAGE if "f_AL=true" not in url and url.endswith("start=0") else "")


real_session, real_sleep = m.requests.Session, m.time.sleep
m.requests.Session, m.time.sleep = FakeSession, lambda seconds: None
try:
    scraped = m.scrape_linkedin_guest_jobs(hours=1, target_count=0, delay=0, keywords_list=["Python Developer"],
                                           locations_list=["Hyderabad"], empty_pages_to_stop=1)
finally:
    m.requests.Session, m.time.sleep = real_session, real_sleep
check("search page: both fresh cards kept under their own ids", [j["job_id"] for j in scraped] == ["4471386121", "4471386122"],
      str([j["job_id"] for j in scraped]))
check("search page: a month-old card is not taken for a new posting", not any(j["job_id"] in ("4471386123", "4471386124") for j in scraped),
      str([(j["job_id"], j["freshness_badge"]) for j in scraped]))
check("search page: link goes to the card's own job", scraped and scraped[0]["job_url"] == "https://www.linkedin.com/jobs/view/4471386121/",
      str(scraped[:1] and scraped[0]["job_url"]))


# --- 3. main(): which notifications fire ---------------------------------------------------------
real_send_gmail = m.send_gmail
real_send_push = m.send_push_notification


def run_main(collected, crash=False, mail_ok=True, seen=(), extra_args=()):
    sent = {"mail": [], "push": [], "issue": []}
    m.send_gmail = lambda subject, body, attachment=None: (sent["mail"].append((subject, body, attachment)), mail_ok)[1]
    m.send_push_notification = lambda *a, **k: sent["push"].append(a) or True
    m.create_github_issue = lambda title, body: sent["issue"].append(title) or "https://example.invalid/issue/1"
    m.load_seen_ids = lambda: set(seen)
    m.save_seen_ids = lambda ids: None
    m.REPORT_DIR = Path(tempfile.mkdtemp(prefix="jmr_reports_"))
    if crash:
        def boom(**kwargs):
            raise RuntimeError("LinkedIn exploded")
        m.collect_jobs = boom
    else:
        m.collect_jobs = lambda **kwargs: [dict(j) for j in collected]
    sys.argv = ["job_match_runner.py", "--hours", "1", *extra_args]
    try:
        code = m.run()
    except RuntimeError as exc:
        code = f"raised {exc}"
    return code, sent


code, sent = run_main([])
check("zero listings: exit 0", code == 0, str(code))
check("zero listings: exactly one mail", len(sent["mail"]) == 1, str(len(sent["mail"])))
check("zero listings: subject says no eligible jobs", sent["mail"] and "no eligible jobs" in sent["mail"][0][0], str(sent["mail"][:1]))
check("zero listings: body explains empty scan", sent["mail"] and "returned no job listings" in sent["mail"][0][1])
check("zero listings: no issue", not sent["issue"])

code, sent = run_main([senior, fresher, pune, no_jd, qa])
check("all filtered: exactly one mail", len(sent["mail"]) == 1, str(len(sent["mail"])))
body = sent["mail"][0][1] if sent["mail"] else ""
check("all filtered: body lists reasons", all(word in body for word in ("Experience", "Location", "Job title", "Description not available")), body[:300])
check("all filtered: scanned count shown", "Scanned <b>5</b>" in body)
check("all filtered: no issue", not sent["issue"])

code, sent = run_main([good, senior, no_jd])
check("one eligible: exactly one mail", len(sent["mail"]) == 1, str(len(sent["mail"])))
subject, body, attachment = sent["mail"][0] if sent["mail"] else ("", "", None)
check("one eligible: subject is the match report", subject.startswith("LinkedIn job matches:"), subject)
check("one eligible: senior job is not in the report", "linkedin.com/jobs/view/2/" not in body)
check("one eligible: unchecked job listed separately", "3. Not checked (1)" in body and "linkedin.com/jobs/view/5/" in body)
check("one eligible: Excel attached", attachment is not None and Path(attachment).exists(), str(attachment))
print(f"   (match score of the eligible job: {[s for s in sent['issue']] or 'no issue'}; subject: {subject})")

check("one eligible: exit 0 when the mail went out", code == 0, str(code))

m_load = m.load_seen_ids
code, sent = run_main([good], seen={"1"})
check("nothing new: exactly one mail", len(sent["mail"]) == 1, str(len(sent["mail"])))
check("nothing new: subject says so", sent["mail"] and "nothing new" in sent["mail"][0][0], str([s[0] for s in sent["mail"]]))
check("nothing new: no duplicate issue", not sent["issue"], str(sent["issue"]))

code, sent = run_main([], crash=True)
check("crash: error is re-raised", str(code).startswith("raised"), str(code))
check("crash: one failure mail", len(sent["mail"]) == 1 and "FAILED" in sent["mail"][0][0], str([s[0] for s in sent["mail"]]))

code, sent = run_main([good], mail_ok=False)
check("mail undeliverable (report): run exits 1", code == 1, str(code))
code, sent = run_main([], mail_ok=False)
check("mail undeliverable (status): run exits 1", code == 1, str(code))

# The numbers in the report text are the ones the run was started with.
code, sent = run_main([good], extra_args=["--min-match", "40", "--hours", "3"])
subject, body, attachment = sent["mail"][0] if sent["mail"] else ("", "", None)
check("--min-match 40: subject, mail, push and issue title say 40%, not 60%",
      "at 40%+" in subject and "matching 40% or more" in body and "60%" not in subject + body
      and all("60%" not in str(text) for text in sent["push"] + sent["issue"]),
      f"{subject} | {sent['push']} | {sent['issue']}")
issue_text = m.build_github_issue_body([], [], 75)
check("issue body follows the threshold", "75% or higher" in issue_text and "matching 75% or more" in issue_text and "60%" not in issue_text)
check("defaults still read 60%", "matching 60% or more" in m.build_email_html([], []) and "matching 60% or more" in m.build_github_issue_body([], []))
code, sent = run_main([senior], extra_args=["--hours", "3"])
check("--hours 3: the push says 3 hours, not 1h", sent["push"] and "last 3 hour(s)" in str(sent["push"][0]) and "1h" not in str(sent["push"][0]), str(sent["push"]))

HOURLY = (Path(repo_dir) / ".github" / "workflows" / "linkedin-hourly.yml").read_text(encoding="utf-8")
save_step = HOURLY.split("- name: Save seen-job history", 1)[1].split("- name:", 1)[0]
check("hourly workflow: history is saved after a failed run too", "!cancelled()" in save_step and "success()" not in save_step, save_step.strip()[:200])


# --- 4. send_gmail itself, against a fake SMTP server ---------------------------------------------
class FakeSMTP:
    fail = False
    sent = []
    attempts = 0

    def __init__(self, *args, **kwargs):
        FakeSMTP.attempts += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        if FakeSMTP.fail:
            raise RuntimeError("535 authentication failed")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


m.send_gmail = real_send_gmail
m.smtplib.SMTP = FakeSMTP
m.time.sleep = lambda seconds: None
os.environ.update({"GMAIL_ADDRESS": "sender@example.com", "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop", "JOB_ALERT_TO": "a@example.com; b@example.com"})
ok = m.send_gmail("subject <1>", "<p>body</p>")
check("send_gmail: delivered -> True, one attempt", ok is True and FakeSMTP.attempts == 1 and len(FakeSMTP.sent) == 1, f"{ok} {FakeSMTP.attempts}")
check("send_gmail: both recipients addressed", FakeSMTP.sent and FakeSMTP.sent[0]["To"] == "a@example.com, b@example.com", str(FakeSMTP.sent and FakeSMTP.sent[0]["To"]))
FakeSMTP.fail, FakeSMTP.attempts = True, 0
ok = m.send_gmail("subject", "<p>body</p>")
check("send_gmail: SMTP keeps failing -> False after 3 attempts", ok is False and FakeSMTP.attempts == 3, f"{ok} {FakeSMTP.attempts}")
kept_recipients = os.environ.pop("JOB_ALERT_TO")
FakeSMTP.fail, FakeSMTP.attempts = False, 0
ok = m.send_gmail("subject", "<p>body</p>")
check("send_gmail: no JOB_ALERT_TO secret -> a failure, and no built-in address is mailed", ok is False and FakeSMTP.attempts == 0, f"{ok} {FakeSMTP.attempts}")
os.environ["JOB_ALERT_TO"] = kept_recipients
os.environ.pop("GMAIL_APP_PASSWORD")
FakeSMTP.attempts = 0
ok = m.send_gmail("subject", "<p>body</p>")
check("send_gmail: secrets missing -> skipped, not a failure", ok is True and FakeSMTP.attempts == 0, f"{ok} {FakeSMTP.attempts}")


# --- the addresses and the push topic live in secrets, not in the repository ----------------------------
class FakePost:
    calls = []
    status_code = 200


def fake_post(url, **kwargs):
    FakePost.calls.append(url)
    return FakePost()


real_post = m.requests.post
m.requests.post = fake_post
os.environ.pop("NTFY_TOPIC", None)
check("push: no NTFY_TOPIC secret -> nothing is sent, there is no built-in topic", real_send_push("t", "m") is False and FakePost.calls == [], str(FakePost.calls))
os.environ["NTFY_TOPIC"] = "a-private-topic"
check("push: goes to the topic in the secret", real_send_push("t", "m") is True and FakePost.calls == ["https://ntfy.sh/a-private-topic"], str(FakePost.calls))
os.environ.pop("NTFY_TOPIC")
m.requests.post = real_post

import re  # noqa: E402

kept_files = [f for pattern in ("*.py", "*.md", "*.txt", ".github/workflows/*.yml", "tests/*.py") for f in Path(repo_dir).glob(pattern)]
leaks = []
for f in kept_files:
    text = f.read_text(encoding="utf-8", errors="replace")
    for found in re.findall(r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|outlook|hotmail|yahoo|icloud|proton|protonmail)\.[a-z.]{2,}", text, re.I):
        if found.lower() != "yourname@gmail.com":
            leaks.append(f"{f.name}: a personal mail address")
    if re.search(r"ntfy\.sh/[A-Za-z0-9_]", text) and f.name != "test_pipeline.py":
        leaks.append(f"{f.name}: a push topic")
    if re.search(r"secrets\.JOB_ALERT_TO\s*\|\|", text) and f.name != "test_pipeline.py":
        leaks.append(f"{f.name}: a fallback address list")
check("no mail address or push topic is written in the repository files", len(kept_files) > 10 and not leaks, str(leaks[:6]))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
