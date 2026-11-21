"""Offline checks of candidate profiles: the settings file, the rules it switches, and the candidate list.

Usage: python tests/test_profiles.py
Nothing is sent and no site is contacted.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import company_portals as cp  # noqa: E402
import job_match_runner as core  # noqa: E402
import matrix  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


def job(title, description="", location="Pune, Maharashtra, India", **extra):
    base = {
        "job_id": "1", "source": "LinkedIn", "title": title, "company": "Acme", "location": location,
        "description": description, "job_url": "https://www.linkedin.com/jobs/view/1/",
        "workplace_type": "Not specified", "employment_type": "Full-time",
    }
    base.update(extra)
    return base


JAVA = {
    "id": "java01", "name": "Test Person", "emails": ["person@example.com"], "experience_years": 3,
    "roles": ["Java Developer", "Backend Developer"], "skills": ["Java", "Spring Boot", "MySQL", "Microservices", "Guidewire"],
    "must_have": ["java"], "avoid_stacks": ["Python", ".NET", "DevOps / infrastructure", "No such family"],
    "cities": ["pune", "Bangalore"], "remote_india": False,
    "fast_alert": {"enabled": True, "city": "bangalore", "min_match": 65},
    "career_sites": {"enabled": True, "companies": ["Infosys", "wipro"]},
}

# --- 1. the settings file ---------------------------------------------------------------------------
clean = core.normalise_profile(JAVA)
check("cities and skills are put in one spelling", clean["cities"] == ["Pune", "Bengaluru"] and clean["skills"][:2] == ["java", "spring boot"], str(clean["cities"]))
check("defaults: LinkedIn on at 60%, quiet; fast alert in its own city", clean["linkedin"] == {"enabled": True, "min_match": 60, "mail_every_run": False}
      and (clean["fast_alert"]["city"], clean["fast_alert"]["min_match"]) == ("Bengaluru", 65), str(clean["linkedin"]))
check("an unknown job family is dropped; company keys are lower-case", clean["avoid_stacks"] == ["Python", ".NET", "DevOps / infrastructure"]
      and clean["career_sites"]["companies"] == ["infosys", "wipro"], str(clean["avoid_stacks"]))
for label, change, word in (
    ("no email", {"emails": ["not-an-address"]}, "email"),
    ("no role", {"roles": []}, "role"),
    ("no skill", {"skills": ""}, "skill"),
    ("nowhere to work", {"cities": [], "remote_india": False}, "city"),
    ("no years", {"experience_years": "three"}, "experience_years"),
    ("a bad id", {"id": "has-dash"}, "id"),
):
    try:
        core.normalise_profile({**JAVA, **change})
        message = ""
    except ValueError as exc:
        message = str(exc)
    check(f"refused: {label}", word in message, message)
remote_only = core.normalise_profile({**JAVA, "cities": [], "remote_india": True, "fast_alert": {"enabled": True}})
check("remote only: allowed, and the fast alert switches off for want of a city", remote_only["fast_alert"]["enabled"] is False
      and core.linkedin_places(remote_only) == ["Remote"], str(remote_only["fast_alert"]))
check("search places: the proven three bare, any other city with its country",
      core.linkedin_places(clean) == ["Pune, India", "Bengaluru"], str(core.linkedin_places(clean)))

# --- 2. the general rules, for a Java developer in Pune and Bengaluru ---------------------------------
applied = core.apply_profile(JAVA)
TITLES = [
    ("Java Developer", True), ("Senior Java Backend Engineer", True), ("Backend Developer (Java/Kotlin)", True),
    ("Software Engineer - Java", True), ("Python Developer", False), ("Java Developer Intern", False),
    ("Principal Java Engineer", False), ("QA Engineer", False), ("Java Architect", False),
]
wrong = [(t, core.title_is_target_role(job(t)), want) for t, want in TITLES if core.title_is_target_role(job(t)) != want]
check("titles: every word of a role, any order; interns and over-senior titles out", not wrong, str(wrong))
check("a family the candidate avoids is dropped", core.title_other_stack("Backend Developer - Python") == "Python"
      and core.title_other_stack("Backend Developer (.NET)") == ".NET", core.title_other_stack("Backend Developer - Python"))
check("...unless the title names a must-have skill", core.title_other_stack("Backend Developer (Java / .NET)") == ""
      and core.title_other_stack("Java DevOps Engineer") == "DevOps / infrastructure", core.title_other_stack("Java DevOps Engineer"))
check("a family the candidate did not tick is kept", core.title_other_stack("Backend Developer - Node.js") == "")

fits = job("Java Developer", "3-5 years of experience in Java, Spring Boot, MySQL and microservices. Guidewire is a plus.")
check("a fitting posting passes every filter", core.rejection_reason(fits, candidate_yoe=3.0) == "", core.rejection_reason(fits, candidate_yoe=3.0))
core.score_job(fits)
check("score: the candidate's own skills, a hand-typed one among them", fits["skill_match_pct"] >= 80 and "guidewire" in fits["matched_skills"]
      and "spring boot" in fits["matched_skills"], f"{fits['skill_match_pct']} {fits['matched_skills']} {fits['missing_skills']}")
no_java = job("Backend Developer", "3 years of experience building REST services in Go and PostgreSQL.")
check("a posting with none of the must-have skills is dropped", core.rejection_reason(no_java, candidate_yoe=3.0) == core.NO_MUST_HAVE
      and core.is_role_mismatch_reason(core.NO_MUST_HAVE), core.rejection_reason(no_java, candidate_yoe=3.0))
check("experience follows the candidate's years", core.rejection_reason(job("Java Developer", "6+ years of experience in Java."), candidate_yoe=3.0) == "Experience")
check("places: the candidate's cities under any spelling", core.location_is_eligible(job("Java Developer", location="Bangalore Urban, Karnataka, India"))
      and not core.location_is_eligible(job("Java Developer", location="Hyderabad, Telangana, India")))
check("remote jobs are refused when the candidate does not want them",
      not core.location_is_eligible(job("Java Developer", "Remote role, India.", location="India", workplace_type="Remote")))
check("senior titles with no years stated stay out below five years", not core.experience_is_eligible(job("Senior Java Developer", "Java."), 3.0))
check("the fast alert watches the candidate's city", core.FAST_ALERT_LOCATION == "Bengaluru"
      and core.is_hyderabad_job(job("x", location="Bangalore, Karnataka, India")) and not core.is_hyderabad_job(job("x", location="Pune, India"))
      and core.fast_alert_name([{"workplace_type": "Hybrid"}]) == "Bengaluru (Hybrid) jobs apply fast")
check("the mail goes to the candidate, and only when there is something new",
      os.environ.get("JOB_ALERT_TO") == "person@example.com" and "NTFY_TOPIC" not in os.environ and core.MAIL_EVERY_RUN is False)
check("the mail says whose rules were used", "Java Developer" in core.ROLE_NOTE and "java" in core.ROLE_NOTE
      and core.PLACES_NOTE == "Pune or Bengaluru", f"{core.ROLE_NOTE} | {core.PLACES_NOTE}")
os.environ.pop("GMAIL_ADDRESS", None)
check("with no sending mailbox a candidate's mail counts as not sent", core.send_gmail("subject", "<p>body</p>") is False)

# --- 3. an experienced candidate ---------------------------------------------------------------------
core.apply_profile({**JAVA, "id": "java02", "experience_years": 9, "exclude_title_words": ["support"], "ntfy_topic": "topic-123",
                    "linkedin": {"mail_every_run": True}})
check("nine years: architect and senior titles are in; the candidate's own excluded word is out",
      core.title_is_target_role(job("Java Architect - Backend Developer")) and core.experience_is_eligible(job("Senior Java Developer", "Java."), 9.0)
      and not core.title_is_target_role(job("Java Developer - Production Support")))
check("push topic and the hourly status mail follow the file", os.environ.get("NTFY_TOPIC") == "topic-123" and core.MAIL_EVERY_RUN is True)

# --- 4. the Python developer rules -------------------------------------------------------------------
PYTHON = {
    "id": "py001", "emails": "a@example.com, b@example.com", "experience_years": 2, "roles": ["Python Developer", "AI Engineer"],
    "skills": ["python", "django", "fastapi", "postgresql", "rest api", "docker"], "must_have": ["python"], "python_rules": True,
    "cities": ["Hyderabad", "Bengaluru", "Chennai"],
}
core.apply_profile(PYTHON)
check("Python rules: another stack that only mentions Python is still removed",
      core.rejection_reason(job("Software Engineer", "2 years of experience. Strong C# and .NET Core. Python scripting is a plus.", location="Hyderabad, India"),
                            candidate_yoe=2.0).startswith(core.DIFFERENT_STACK))
check("Python rules: the built-in titles stay, the usual three cities and remote India are searched",
      core.title_is_eligible(job("Backend Engineer")) and not core.title_is_eligible(job("QA Engineer"))
      and core.SEARCH_LOCATIONS == ["Hyderabad", "Bengaluru", "Chennai", "Remote"] and core.PLACES_NOTE == "Hyderabad, Bengaluru or Chennai (or remote in India)",
      str(core.SEARCH_LOCATIONS))
check("two addresses in one field", os.environ.get("JOB_ALERT_TO") == "a@example.com,b@example.com")

# --- 5. the career-site scan and the candidate list --------------------------------------------------
with tempfile.TemporaryDirectory() as folder:
    people = Path(folder) / "candidates"
    people.mkdir()
    (people / "java01.json").write_text(json.dumps(JAVA), encoding="utf-8")
    (people / "py001.json").write_text(json.dumps({**PYTHON, "fast_alert": {"enabled": False}}), encoding="utf-8")
    (people / "paused1.json").write_text(json.dumps({**PYTHON, "id": "paused1", "enabled": False}), encoding="utf-8")
    (people / "broken1.json").write_text("{not json", encoding="utf-8")
    (people / "other.json").write_text(json.dumps({**PYTHON, "id": "different"}), encoding="utf-8")

    profile = cp.use_profile(people / "java01.json")
    check("career sites: the candidate's roles are the search words, the cities go to the sites under every spelling",
          cp.SEARCH_KEYWORDS == ["Java Developer", "Backend Developer"] and cp.SEARCH_CITIES == ["Pune", "Bengaluru"]
          and cp._FEED_PLACES == "Pune OR Pimpri OR Bengaluru OR Bangalore" and cp._city_spellings() == ["Pune", "Pimpri", "Bengaluru", "Bangalore"]
          and "in Pune or Bengaluru, full-time, one of your roles" in cp.rule_text(3.0), f"{cp._FEED_PLACES} | {cp.rule_text(3.0)}")
    check("career sites: the candidate's own list of companies", profile["career_sites"]["companies"] == ["infosys", "wipro"])

    found = matrix.matrices(matrix.read_candidates(Path(folder)))
    check("candidate list: paused, unreadable and misnamed files are left out", [e["id"] for e in found["sites"]] == ["java01", "py001"], str(found["sites"]))
    check("candidate list: one LinkedIn search per candidate and place",
          [(e["id"], e["place"], e["slug"]) for e in found["linkedin_searches"]] == [
              ("java01", "Pune, India", "pune-india"), ("java01", "Bengaluru", "bengaluru"),
              ("py001", "Hyderabad", "hyderabad"), ("py001", "Bengaluru", "bengaluru"), ("py001", "Chennai", "chennai"), ("py001", "Remote", "remote")],
          str(found["linkedin_searches"]))
    check("candidate list: the fast alert only where it is on", [e["id"] for e in found["fast"]] == ["java01"], str(found["fast"]))
    only = matrix.matrices(matrix.read_candidates(Path(folder), only=["py001"]))
    check("candidate list: 'only' narrows it", [e["id"] for e in only["linkedin"]] == ["py001"])
    printed = subprocess.run([sys.executable, str(ROOT / "tools" / "matrix.py"), folder], capture_output=True, text=True)
    lines = dict(line.split("=", 1) for line in printed.stdout.splitlines() if "=" in line)
    check("candidate list: printed for the workflow, with no name or address in it",
          printed.returncode == 0 and lines.get("sites_count") == "2" and json.loads(lines.get("fast", "[]")) == [{"id": "java01"}]
          and "example.com" not in printed.stdout + printed.stderr and "Test Person" not in printed.stdout + printed.stderr, printed.stdout + printed.stderr)

checked = subprocess.run([sys.executable, str(ROOT / "tools" / "build_page_data.py"), "--check"], capture_output=True, text=True)
check("the settings page's data file matches the code", checked.returncode == 0, checked.stdout)
example = core.load_profile(ROOT / "candidates.example" / "example1.json")
check("the example candidate file is valid", example["id"] == "example1" and example["linkedin"]["enabled"])

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("ALL PASS")
