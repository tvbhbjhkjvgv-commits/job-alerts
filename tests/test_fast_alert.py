"""Offline checks of the Hyderabad fast alert: who qualifies, the work mode, and when notifications fire.

Usage: python tests/test_fast_alert.py [<repo_dir>]   (default: the folder above tests/)
Nothing is sent and LinkedIn is not contacted: the search, mail, push and issue are replaced by recorders.
"""
import importlib.util
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("jmr", os.path.join(repo_dir, "job_match_runner.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


STACK = "Python, Django, React, PostgreSQL, REST APIs, Docker."


def job(job_id, title="Python Developer", description=f"2-4 years of experience in {STACK}", location="Hyderabad, Telangana, India", **extra):
    base = {
        "job_id": str(job_id), "source": "LinkedIn", "title": title, "company": "Acme", "location": location,
        "description": description, "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
        "workplace_type": "Not specified", "employment_type": "Full-time", "age_hours": 0.2, "freshness_badge": "<1h ago (12m)",
    }
    base.update(extra)
    return base


# --- 1. work mode, read from the posting's own words ------------------------------------------------
MODES = [
    ("title says remote", job(1, title="Python Developer (Remote)"), "Remote"),
    ("location says hybrid", job(1, location="Hyderabad, Telangana, India (Hybrid)"), "Hybrid"),
    ("'Work mode: Hybrid' line", job(1, description=f"Work mode: Hybrid\n2-4 years of {STACK}"), "Hybrid"),
    ("three days a week in the office", job(1, description=f"You will be 3 days a week in the office. {STACK}"), "Hybrid"),
    ("work from office", job(1, description=f"This role requires work from office. {STACK}"), "On-site"),
    ("five days a week in office", job(1, description=f"5 days a week in office. {STACK}"), "On-site"),
    ("work from home", job(1, description=f"Permanent work from home. {STACK}"), "Remote"),
    ("office days and home days", job(1, description=f"Work from office on Tuesdays, work from home otherwise. {STACK}"), "Hybrid"),
    ("'hybrid search' is not a work mode", job(1, description=f"Build RAG with hybrid search and vector databases. {STACK}"), m.WORK_MODE_UNKNOWN),
    ("'remote teams' is not a work mode", job(1, description=f"Collaborate with remote teams across time zones. {STACK}"), m.WORK_MODE_UNKNOWN),
    ("nothing stated", job(1), m.WORK_MODE_UNKNOWN),
    ("work mode supplied with the record", job(1, workplace_type="Hybrid"), "Hybrid"),
]
for name, record, expected in MODES:
    got = m.work_mode_label(record)
    check(f"work mode: {name}", got == expected, f"expected {expected!r}, got {got!r}")


# --- 2. who qualifies ---------------------------------------------------------------------------------
NO_YEARS = f"We build products with {STACK}"
CLASSES = [
    ("Hyderabad, 2-4 years, full stack match", job(10), "alert"),
    ("0-2 years includes 2", job(10, description=f"Experience: 0-2 years\n{STACK}"), "alert"),
    ("2+ years", job(10, description=f"2+ years of experience in {STACK}"), "alert"),
    ("years only in the title", job(10, title="Python Developer (1-3 yrs)", description=NO_YEARS), "alert"),
    ("on-site, hybrid and remote all count", job(10, title="Python Developer (Remote)"), "alert"),
    ("Secunderabad counts as Hyderabad", job(10, location="Secunderabad, Telangana, India"), "alert"),
    ("no experience stated -> second table only", job(10, description=NO_YEARS), "no_yoe"),
    ("3+ years -> out", job(10, description=f"3+ years of experience in {STACK}"), ""),
    ("0-1 year -> out", job(10, description=f"Experience: 0-1 year\n{STACK}"), ""),
    ("Bengaluru -> out", job(10, location="Bengaluru, Karnataka, India"), ""),
    ("India-remote, not Hyderabad -> out", job(10, location="India", workplace_type="Remote"), ""),
    ("internship -> out", job(10, employment_type="Internship"), ""),
    ("contract -> out", job(10, employment_type="Contract"), ""),
    ("below 70% -> out", job(10, description="2-4 years of experience in Python, Java, Spring, Kubernetes, AWS, Kafka, Angular."), ""),
    ("Senior title with no years -> out", job(10, title="Senior Python Developer", description=NO_YEARS), ""),
    ("Java role -> out", job(10, title="Java Developer"), ""),
]
for name, record, expected in CLASSES:
    got = m.fast_alert_class(record, candidate_yoe=2.0, min_match=70)
    check(f"class: {name}", got == expected, f"expected {expected!r}, got {got!r} ({record.get('skill_match_pct')}%, {m.rejection_reason(dict(record))!r})")
seventy = job(11, description="2-4 years of experience in Python, Django, React, PostgreSQL, Docker, Git, Redis, AWS, Terraform, Kafka.")
check("70% is an alert at --fast-min-match 70, not at 80",
      m.fast_alert_class(dict(seventy), min_match=70) == "alert" and m.fast_alert_class(dict(seventy), min_match=80) == "",
      str(m.score_job(dict(seventy))["skill_match_pct"]))
check("a job at exactly 70% alerts with the default threshold", m.fast_alert_class(dict(seventy)) == "alert", str(m.FAST_ALERT_MIN_MATCH))
# The threshold is each candidate's own: the workflow hands over the candidate's file and no number of its own.
WORKFLOW = (Path(m.__file__).resolve().parent / ".github" / "workflows" / "fast-alert.yml").read_text(encoding="utf-8")
check("the workflow leaves the threshold to the candidate's file",
      "--fast-min-match" not in WORKFLOW and "min_match" not in WORKFLOW and "CANDIDATE_PROFILE: candidates-data/candidates/${{ matrix.id }}.json" in WORKFLOW)

check("name: one mode", m.fast_alert_name([{"workplace_type": "Hybrid"}]) == "Hyderabad (Hybrid) jobs apply fast", m.fast_alert_name([{"workplace_type": "Hybrid"}]))
mixed = m.fast_alert_name([{"workplace_type": m.WORK_MODE_UNKNOWN}, {"workplace_type": "On-site"}, {"workplace_type": "Hybrid"}, {"workplace_type": "On-site"}])
check("name: several modes, unknown last", mixed == f"Hyderabad (On-site, Hybrid, {m.WORK_MODE_UNKNOWN}) jobs apply fast", mixed)


# --- 3. passes: what is sent, and when ------------------------------------------------------------------
class Args:
    hours = 1.0
    delay = 0.0
    source = "guest"
    proxy = None
    keywords = None
    candidate_yoe = 2.0
    fast_min_match = 70
    dry_run = False
    watch_minutes = 0.0
    interval_minutes = 5.0


sent = {"mail": [], "push": [], "issue": []}
outcome = {"mail": True, "push": True, "issue": "https://example.invalid/issues/9"}
listing = []
calls = []


def fake_collect(**kwargs):
    calls.append(kwargs)
    skip = kwargs.get("skip_ids") or set()
    return [dict(j) for j in listing if j["job_id"] not in skip]


m.collect_jobs = fake_collect
m.send_gmail = lambda subject, body, attachment=None: (sent["mail"].append((subject, body)), outcome["mail"])[1]
m.send_push_notification = lambda title, message, click_url="", priority="high": (sent["push"].append((title, message, click_url, priority)), outcome["push"])[1]
m.create_github_issue = lambda title, body: (sent["issue"].append((title, body)), outcome["issue"])[1]
work = Path(tempfile.mkdtemp(prefix="jmr_fast_"))
m.DATA_DIR = work
m.FAST_STATE_FILE = work / "hyderabad_alert_state.json"
m.REPORT_DIR = work / "reports"


def clear():
    for box in sent.values():
        box.clear()


no_years_job = job(100, title="Software Engineer", description=NO_YEARS, company="NoYears Ltd")
alert_job = job(101, description=f"Work mode: Hybrid\n2-4 years of experience in {STACK}", company="Fits Ltd")
too_senior = job(102, description=f"5+ years of experience in {STACK}", company="Senior Ltd")
bengaluru = job(103, location="Bengaluru, Karnataka, India", company="Blr Ltd")
no_description = job(104, description="", company="Slow Ltd")

state = m.load_fast_state()
check("state starts empty", state == {"decided": {}, "alerted": {}, "queue": [], "held": []}, str(state))

listing[:] = [no_years_job, too_senior, bengaluru, no_description]
result = m.fast_alert_pass(state, Args)
check("pass 1 searches Hyderabad only", calls[-1]["location"] == "Hyderabad", str(calls[-1].get("location")))
check("pass 1: a job without stated experience does not start an alert", not sent["mail"] and not sent["push"] and not sent["issue"], str(sent))
check("pass 1: that job is held for the next alert", [r["job_id"] for r in state["held"]] == ["100"] and not state["queue"], str(state["held"]))
check("pass 1: judged jobs are remembered", set(state["decided"]) == {"100", "102", "103"}, str(sorted(state["decided"])))
check("pass 1: a job whose description did not arrive is asked for again", "104" not in state["decided"] and result["waiting"] == 1, str(result))

listing[:] = [no_years_job, too_senior, bengaluru, dict(no_description, description=f"1-3 years of experience in {STACK}"), alert_job]
result = m.fast_alert_pass(state, Args)
check("pass 2 skips what pass 1 judged", calls[-1]["skip_ids"] == {"100", "102", "103"}, str(calls[-1].get("skip_ids")))
check("pass 2: one push, one mail, one issue", (len(sent["push"]), len(sent["mail"]), len(sent["issue"])) == (1, 1, 1), str({k: len(v) for k, v in sent.items()}))
subject, body = sent["mail"][0] if sent["mail"] else ("", "")
check("mail name is 'Hyderabad (<work mode>) jobs apply fast'", subject == f"Hyderabad (Hybrid, {m.WORK_MODE_UNKNOWN}) jobs apply fast", subject)
table_1, _, table_2 = body.partition("2. Also qualified")
check("table 1 lists both jobs whose experience fits", "jobs/view/101/" in table_1 and "jobs/view/104/" in table_1 and "1. Apply now (2)" in table_1, table_1[-400:])
check("table 2 lists the job without stated experience", "jobs/view/100/" in table_2 and "jobs/view/100/" not in table_1, table_2[:300])
check("rejected jobs are in neither table", "jobs/view/102/" not in body and "jobs/view/103/" not in body)
check("push is urgent and opens a job", sent["push"] and sent["push"][0][3] == "urgent" and "linkedin.com/jobs/view/10" in sent["push"][0][2], str(sent["push"]))
check("issue has both tables", sent["issue"] and "jobs/view/101/" in sent["issue"][0][1] and "jobs/view/100/" in sent["issue"][0][1] and sent["issue"][0][0].startswith("Hyderabad (Hybrid"), str(sent["issue"])[:200])
check("after sending, nothing is left waiting", not state["queue"] and not state["held"] and {"100", "101", "104"} <= set(state["alerted"]), str(state["alerted"].keys()))

clear()
m.save_fast_state(state)
state = m.load_fast_state()
repost = dict(alert_job, job_id="201", job_url="https://www.linkedin.com/jobs/view/201/")
listing[:] = [no_years_job, alert_job, repost]
m.fast_alert_pass(state, Args)
check("pass 3 (state reloaded from disk): nothing is sent twice, not even a re-post under a new id", not sent["mail"] and not sent["push"] and not sent["issue"], str(sent))

# Only jobs without stated experience, for a whole day: never an alert, and they drop off after FAST_HELD_HOURS.
listing[:] = [job(300, title="Backend Developer", description=NO_YEARS, company="Quiet Ltd")]
m.fast_alert_pass(state, Args)
check("held job waits", [r["job_id"] for r in state["held"]] == ["300"] and not sent["mail"])
state["held"][0]["posted_at"] = (datetime.now(timezone.utc) - timedelta(hours=m.FAST_HELD_HOURS + 1)).isoformat()
listing[:] = []
m.fast_alert_pass(state, Args)
check("held job older than the limit is dropped, still without an alert", not state["held"] and not sent["mail"] and not sent["push"])

# Delivery problems.
clear()
outcome.update(mail=False, push=False, issue="")
listing[:] = [job(400, company="Retry Ltd")]
result = m.fast_alert_pass(state, Args)
check("nothing could be delivered: the alert stays queued", [r["job_id"] for r in state["queue"]] == ["400"] and "400" not in state["alerted"] and result["mail_failed"], str(result))
outcome.update(mail=True, push=True, issue="https://example.invalid/issues/10")
clear()
listing[:] = []
m.fast_alert_pass(state, Args)
check("next pass delivers the queued alert once", len(sent["mail"]) == 1 and "jobs/view/400/" in sent["mail"][0][1] and not state["queue"], str(len(sent["mail"])))

clear()
outcome.update(mail=False)
listing[:] = [job(500, company="PushOnly Ltd")]
sys.argv = ["job_match_runner.py", "--fast-alert", "--source", "guest"]
code = m.run()
check("mail refused but push went out: not sent again, run marked failed", code == 1 and len(sent["push"]) == 1 and "500" in m.load_fast_state()["alerted"], f"{code} {len(sent['push'])}")
outcome.update(mail=True)

# The watcher as the workflow starts it.
clear()
listing[:] = [job(600, title="AI Engineer", description=f"This is an on-site role. 1-3 years of experience in {STACK}", company="Cli Ltd")]
sys.argv = ["job_match_runner.py", "--fast-alert", "--source", "guest", "--candidate-yoe", "2.0", "--fast-min-match", "70", "--watch-minutes", "0"]
code = m.run()
check("--fast-alert: exit 0 and one alert", code == 0 and len(sent["mail"]) == 1, f"{code} {len(sent['mail'])}")
check("--fast-alert: subject carries the work mode", sent["mail"] and sent["mail"][0][0] == "Hyderabad (On-site) jobs apply fast", str([s[0] for s in sent["mail"]]))
clear()
listing[:] = []
code = m.run()
check("--fast-alert with nothing new: silent (no hourly-style 'no eligible jobs' mail)", code == 0 and not sent["mail"] and not sent["push"] and not sent["issue"], str(sent))


def crash(**kwargs):
    raise RuntimeError("LinkedIn exploded")


m.collect_jobs = crash
code = m.run()
check("--fast-alert: a crashed scan exits 1 without a failure mail", code == 1 and not sent["mail"], f"{code} {sent['mail']}")
m.collect_jobs = fake_collect

# Dry run: preview on disk, nothing sent, state file untouched.
clear()
before = m.FAST_STATE_FILE.read_text(encoding="utf-8")
listing[:] = [job(700, company="Preview Ltd")]
sys.argv = ["job_match_runner.py", "--fast-alert", "--source", "guest", "--dry-run"]
code = m.run()
preview = m.REPORT_DIR / "hyderabad_alert_preview.html"
check("--dry-run: nothing sent", code == 0 and not sent["mail"] and not sent["push"] and not sent["issue"], str(sent))
check("--dry-run: preview written", preview.exists() and "jobs/view/700/" in preview.read_text(encoding="utf-8"))
check("--dry-run: state file untouched", m.FAST_STATE_FILE.read_text(encoding="utf-8") == before)

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
