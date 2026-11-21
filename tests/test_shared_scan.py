"""Offline checks of the career scan run for every candidate at once (company_portals.py --all).

Usage: python tests/test_shared_scan.py
No site is contacted and nothing is sent: the sites, the mail and the push are replaced by recorders.
"""
import argparse
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_portals as cp  # noqa: E402
import job_match_runner as core  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


work = tempfile.TemporaryDirectory()
core.DATA_DIR = Path(work.name) / "data"
core.REPORT_DIR = Path(work.name) / "reports"
for name in ("GEMINI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(name, None)

reads = {"acme": 0, "globex": 0, "broken": 0}
pages = []
asked = {}


def record(key, company, number, title, place, text):
    return cp._record(key, company, number, title, place, f"https://example.com/{key}/{number}", description=text, posted="2026-11-20")


def read_acme(site):
    reads["acme"] += 1
    asked["keywords"], asked["years"] = list(site.keywords), list(site.candidate_years)
    return [
        record("acme", "Acme", 1, "Python Developer", "Hyderabad, India", "2-4 years of experience in Python, Django, PostgreSQL, Docker and REST APIs."),
        record("acme", "Acme", 2, "Java Developer", "Pune, India", "3-5 years of experience in Java, Spring Boot, MySQL and microservices."),
        record("acme", "Acme", 3, "Python Developer", "Mumbai, India", "2-4 years of experience in Python."),
    ]


def read_globex(site):
    reads["globex"] += 1
    job = record("globex", "Globex", 9, "Java Developer", "Pune, India", "")

    def fill(site, job):
        pages.append(job["job_id"])
        job["description"] = "2-4 years of experience in Java and Spring Boot."

    job["_detail"] = fill
    return [job]


def read_broken(site):
    reads["broken"] += 1
    raise cp.PortalError("HTTP 500 from https://example.com/broken")


class QuietSite:
    def __init__(self, keywords, candidate_yoe=2.0):
        self.keywords, self.candidate_yoe, self.candidate_years, self.requests_made = list(keywords), candidate_yoe, [candidate_yoe], 0

    def out_of_time(self):
        return False

    def more_time(self, seconds):
        pass


cp.Site = QuietSite
PORTALS = {"acme": ("Acme", read_acme), "globex": ("Globex", read_globex), "broken": ("Broken Co", read_broken)}

sent = []
core.send_gmail = lambda subject, body, attachment=None: sent.append((os.environ.get("JOB_ALERT_TO"), subject, body)) or True
core.send_push_notification = lambda *a, **k: False

folder = Path(work.name) / "settings"
(folder / "candidates").mkdir(parents=True)


def candidate(name, **settings):
    base = {"id": name, "emails": [f"{name}@example.com"], "experience_years": 3, "cities": ["Pune"], "remote_india": False,
            "roles": ["Java Developer"], "skills": ["java", "spring boot", "mysql", "microservices"]}
    (folder / "candidates" / f"{name}.json").write_text(json.dumps({**base, **settings}), encoding="utf-8")


candidate("pyone", roles=["Python Developer"], skills=["python", "django", "postgresql", "docker", "rest api"], cities=["Hyderabad"],
          experience_years=2, python_rules=True, must_have=["python"])
candidate("javaone")
candidate("javatwo", experience_years=4)
candidate("paused", enabled=False)
candidate("noscan", career_sites={"enabled": False})
candidate("ended", plan="trial", trial_started="2026-01-01T00:00:00Z")
(folder / "candidates" / "broken1.json").write_text("{", encoding="utf-8")


def run(dry_run=False, only=(), companies=()):
    sent.clear()
    args = argparse.Namespace(all=str(folder), only=list(only), company_list=list(companies), dry_run=dry_run)
    return cp.run_all(args, PORTALS)


code = run()
check("each site is read once, however many candidates there are", reads == {"acme": 1, "globex": 1, "broken": 1}, str(reads))
check("the sites are asked for every candidate's roles and years together",
      asked["keywords"] == ["Java Developer", "Python Developer"] and asked["years"] == [2.0, 3.0, 4.0], str(asked))
check("a job page two candidates both need is opened once", pages == ["globex:9"], str(pages))
check("only active candidates with the scan on and a running trial are scanned",
      sorted(to for to, _, _ in sent) == ["javaone@example.com", "javatwo@example.com", "pyone@example.com"], str([to for to, _, _ in sent]))
by_person = {to: body for to, _, body in sent}
check("each candidate is judged by their own roles, city and years",
      "Python Developer" in by_person.get("pyone@example.com", "") and "Java Developer" not in by_person.get("pyone@example.com", "")
      and by_person.get("javaone@example.com", "").count("Java Developer") == 2 and "Python Developer" not in by_person.get("javaone@example.com", "")
      and by_person.get("javatwo@example.com", "").count("Java Developer") == 2)
check("one site failing is not a failed run, and one candidate's mail holds nobody else's job", code == 0 and "Mumbai" not in "".join(by_person.values()))
states = sorted(p.name for p in (core.DATA_DIR / "portal_state").glob("*.json"))
check("each candidate has their own history", states == ["javaone.json", "javatwo.json", "pyone.json"], str(states))

code = run()
check("a second run reads each site once more and mails nothing that was already sent",
      reads == {"acme": 2, "globex": 2, "broken": 2} and sent == [] and code == 0, f"{reads} {len(sent)}")
check("...and opens no job page again for a posting everyone has already been judged against", pages == ["globex:9"], str(pages))

candidate("javanew", experience_years=3)
run()
check("a candidate added later gets what is open now, and nobody else is mailed again",
      [to for to, _, _ in sent] == ["javanew@example.com"] and sent[0][2].count("Java Developer") == 2, str([to for to, _, _ in sent]))

core.send_gmail = lambda subject, body, attachment=None: (_ for _ in ()).throw(RuntimeError("mail server down")) if os.environ.get("JOB_ALERT_TO") == "aaa1@example.com" \
    else sent.append((os.environ.get("JOB_ALERT_TO"), subject, body)) or True
candidate("aaa1")
candidate("zzz9")
code = run()
check("one candidate's failure does not stop the candidates after them, and the run is marked failed",
      code == 1 and [to for to, _, _ in sent] == ["zzz9@example.com"], f"{code} {[to for to, _, _ in sent]}")

before = dict(reads)
run(dry_run=True, only=["pyone"], companies=["acme"])
check("'only' and 'companies' narrow a run", reads["acme"] == before["acme"] + 1 and reads["globex"] == before["globex"] and sent == [])

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("ALL PASS")
