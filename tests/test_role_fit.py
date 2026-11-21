"""Offline checks for the role-fit filter, the score, duplicate merging and the search-worker hand-off.

Usage: python tests/test_role_fit.py [<repo_dir>]   (default: the folder above tests/)
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("jmr", os.path.join(repo_dir, "job_match_runner.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

failures = []


def check(name, got, want):
    ok = got == want
    if not ok:
        failures.append(name)
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   got {got!r}, want {want!r}"))


def job(title, description="", **extra):
    base = {"job_id": "4000000001", "source": "LinkedIn", "job_url": "https://www.linkedin.com/jobs/view/4000000001/",
            "title": title, "company": "Acme", "location": "Hyderabad, Telangana, India", "description": description}
    base.update(extra)
    return base


KEEP = ""
PY = "We need a Python developer. You will build REST APIs with Django and FastAPI. Strong Python skills are required."
GEN = "Build LLM applications with LangChain and RAG pipelines using Python and Azure OpenAI."

print("Title names another stack or job family")
for title, want in [
    ("Java Developer", "Different stack: Java"),
    ("Senior Java Full Stack Developer", "Different stack: Java"),
    ("Software Engineer - Kotlin", "Different stack: Kotlin"),
    (".NET Full Stack Developer", "Different stack: .NET"),
    ("Software Engineer (C#)", "Different stack: .NET"),
    ("C++ Software Engineer", "Different stack: C++"),
    ("Backend Engineer (Golang)", "Different stack: Go"),
    ("SDE I - Golang", "Different stack: Go"),
    ("Full Stack Developer (MERN)", "Different stack: Node.js"),
    ("Senior Software Engineer - Node.js | AWS", "Different stack: Node.js"),
    ("Full Stack Developer - Angular", "Different stack: Angular"),
    ("Android Software Engineer", "Different stack: Mobile"),
    ("Software Engineer - Salesforce", None),  # already excluded by the old title list; any removal is fine
    ("Software Engineer, DevOps", "Different stack: DevOps / infrastructure"),
    ("Python DevOps Engineer", "Different stack: DevOps / infrastructure"),
    ("Embedded Software Engineer", "Different stack: Embedded"),
    ("AI-First Performance Marketing Specialist", "Not a developer role"),
    ("Tech & Data - AI Governance - Associate 2", "Not a developer role"),
    ("Backend Support Engineer", "Not a developer role"),
    ("Python Trainer", "Not a developer role"),
    ("Azure AI Cloud Engineer", "Different stack: DevOps / infrastructure"),
    ("Senior Software Engineer (ERP Developer)", "Different stack: Salesforce / SAP / ERP"),
    ("Data Engineer - Python", "Different stack: Data engineering / BI"),
]:
    got = m.role_mismatch(job(title, PY))
    if want is None:
        check(f"{title} -> removed", bool(got) or not m.title_is_eligible(job(title)), True)
    else:
        check(f"{title} -> {want}", got, want)
        check(f"   and its description is not fetched (title not eligible)", m.title_is_eligible(job(title)), False)

check("C/C++ title that also lists Python -> C++",
      m.title_other_stack("Software Engineer - C/C++ programmer, Linux, Python"), "C++")
check("Java Developer with Python -> Java", m.title_other_stack("Java Developer with Python"), "Java")
check("developer job in a marketing team stays",
      m.title_other_stack("Senior Full Stack Engineer - Marketing Capabilities"), "")
check("long non-developer AI title is removed",
      m.role_mismatch(job("Cost Intelligence & AI Analytics - Capital Projects", GEN)), m.NOT_DEVELOPER_ROLE)
check("framework names give the stack away",
      m.role_mismatch(job("Software Engineer", "Expertise in server side languages such as Java and frameworks "
                          "(such as Spring, Hibernate), scripting languages (such as Python) and NodeJS.")), "Different stack: Java")
check("data-pipeline job under a Software Engineer title",
      m.role_mismatch(job("Software Engineer II", "Python and SQL.\nBuild data pipelines with PySpark on Databricks.\n"
                          "Snowflake and Airflow.")), "Different stack: Data engineering / BI")
check("legacy migration sentence is not a requirement",
      m.role_mismatch(job("Python Developer", "Strong Python and Django.\nMigrate legacy Java services to Python.")), KEEP)

print("Titles that stay")
for title in ["Python Developer", "Software Engineer", "AI Engineer", "Generative AI Engineer", "Backend Developer",
              "Full Stack Developer (Python/React)", "Software Engineer (Java/Python)", "Python Developer (Django)",
              "SDE 1", "JavaScript and Python Full Stack Engineer", "Software Engineer - Backend (Python, Go)"]:
    check(f"{title} -> title kept", m.title_other_stack(title), "")

print("Description decides")
cases = [
    ("Python job", "Software Engineer", PY, KEEP),
    ("GenAI job", "AI Engineer", GEN, KEEP),
    ("no Python, no GenAI", "Software Engineer", "Build scalable web services. Strong CS fundamentals, SQL and AWS.", m.NO_PYTHON),
    ("React + Node only", "Full Stack Developer", "React, TypeScript and Express on AWS. 2 years experience.", m.NO_PYTHON),
    ("Java required, no Python", "Software Engineer", "Strong Java and Spring Boot. Microservices with Kafka.", m.NO_PYTHON),
    ("GenAI but on Java, no Python at all", "AI Software Engineer",
     "Build LLM agents and RAG services.\nStrong Java and Spring Boot experience is required.\nJava microservices on AWS.", m.NO_PYTHON),
    ("GenAI but on Java, Python for scripts", "AI Software Engineer",
     "Build LLM agents and RAG services.\nStrong Java and Spring Boot experience is required.\nPython for scripting.", "Different stack: Java"),
    ("GenAI with no Python named anywhere", "AI Engineer",
     "Build AI agents and RAG assistants.\nMinimum 3 years of experience with Node.js and Express.js.", m.NO_PYTHON),
    ("LangChain counts as Python", "AI Engineer", "Build RAG pipelines with LangChain and vector databases.", KEEP),
    ("e.g. list is a choice", "Software Engineer (Full Stack)",
     "Frontend in React.\nOne backend stack (e.g. Node.js, Python, Java, Go).", KEEP),
    ("several languages and an 'or'", "AI Platform Engineer",
     "Programming experience using Python, Java, Go, C#, or another modern backend programming language.\nBuild LLM platforms.", KEEP),
    ("slash skills list is not a choice", "Software Engineer",
     "Skills: C/C++/STL/BOOST/Python/Java/REST API/TCL/TK", "Different stack: Java"),
    ("'(Preferred)' on its own line belongs to the line above", "AI Engineer",
     "Strong proficiency in Python for AI workflows and LLM agents.\nPython\nC#/.NET\n(Preferred)", KEEP),
    ("both asked together but also offered as a choice elsewhere", "Software Engineer",
     "Strong knowledge of scripting languages, including Java and Python.\nProficiency in one or more languages such as Java, Python or Go.", KEEP),
    ("C and Pro*C", "AI Engineer", "Python and LLM work.\nPractical hands-on experience with C and Pro*C in Linux environments.", "Different stack: C++"),
    ("one ERP platform all over the posting", "Software Development Engineer",
     "Python scripting.\nOracle Fusion Cloud applications.\nOracle Fusion HCM.\nOracle Fusion ERP.\nOracle Fusion integrations.\nOracle Fusion reports.",
     "Different stack: Salesforce / SAP / ERP"),
    ("platforms as integration targets", "AI Engineer",
     "Python and LLM agents.\nIntegrate with Salesforce.\nIntegrate with SAP.\nConnect ServiceNow.", KEEP),
    ("the description says it is a product manager job", "Senior AI Engineer",
     "WSP is seeking a technically credible Product Manager to lead AI product delivery. Python and LLM knowledge.", m.NOT_DEVELOPER_ROLE),
    ("the description says it is a data engineer job", "Software Engineer",
     "Join us as a Data Engineer Expert to build pipelines in Python.", "Different stack: Data engineering / BI"),
    # Patterns found on hand-labelled postings the filter had not seen before.
    ("'most of the following' is a choice list", "Applied AI Engineer",
     "5+ years of experience with most of the following: Angular, React, NodeJS, Python, C#, .NET, Java, SQL, LangChain.", KEEP),
    ("three languages and an 'or'", "Technical Lead - AI First Approach",
     "Backend in Laravel (PHP), Node.js (Express/Nest), or Python (Django/FastAPI) with OpenAI API integration.", KEEP),
    ("a choice that does not include Python", "Software Engineer",
     "Requires C# / .NET and Python.\nBuild .NET services and APIs.", "Different stack: .NET"),
    ("frameworks named inside a choice", "Senior Backend Engineer",
     "Backend services and REST APIs using Java/Spring Boot, Python or Rust, depending on the service.\n"
     "Strong programming skills in at least one of Java/Spring Boot, Python or Rust.", KEEP),
    ("slash pair followed by one language's frameworks", "Sr. Software Developer (Python & AI)",
     "Strong proficiency in Java/Python, J2EE, Spring, Spring Boot, microservices.\nPython for AI features.", "Different stack: Java"),
    ("'team' in the sentence does not hide a requirement", "Gen AI Developer",
     "A developer with 7+ years of experience in Java, Python, and Kubernetes, comfortable working as part of a paired team.\n"
     "Build LLM applications.", "Different stack: Java"),
    ("cloud infrastructure work under a software title", "Enterprise Cloud Software Engineer",
     "Automate provisioning with Terraform and Bicep.\nAnsible playbooks and patching.\nPython scripting.", "Different stack: DevOps / infrastructure"),
    ("GenAI platform job that also provisions infrastructure", "Engineer - AI Platform",
     "Python (preferred) or Java/Go.\nGenAI/LLM hands-on: calling LLM APIs, RAG patterns.\nTerraform, Helm and provisioning of the platform.", KEEP),
    ("Python job that integrates with an ERP", "Python Developer",
     "Backend services and APIs in Python (FastAPI).\nPython and PostgreSQL.\nPython tests with pytest.\nPython workers with Celery.\n"
     "Python data jobs.\nPython SDK.\nSync with SAP.\nSAP orders.\nSAP invoices.\nSAP stock.\nSAP customers.", KEEP),
    ("Java is the stack, Python once", "Software Engineer",
     "5 years of Java development.\nSpring Boot, Hibernate, microservices.\nJava 17 features.\nScripting in Python.", "Different stack: Java"),
    ("Python and Java both required", "Backend Developer", "Proficiency in Python and Java is required.", "Different stack: Java"),
    ("pick-one list", "Software Engineer",
     "Experience in one or more languages such as Java, Python, Go or C++.\nBuild services in Python.", KEEP),
    ("Python or Java", "Software Engineer", "Strong programming skills in Python or Java.\nPython services with Django.", KEEP),
    ("Java/Python slash", "Software Engineer (Java/Python)", "Hands-on with Java/Python.\nDjango or Flask web services in Python.", KEEP),
    ("Java only nice-to-have", "Python Developer",
     "Strong Python and Django.\nREST APIs in Python.\nNice to have: Java or Kotlin.", KEEP),
    ("Java under a preferred heading", "Python Developer",
     "Requirements:\nStrong Python and Django.\nPostgreSQL.\nPreferred Qualifications:\nExposure to Java\nKubernetes", KEEP),
    ("Java only in the company blurb", "Python Developer",
     "Strong Python and FastAPI.\nAbout Us:\nWe are the largest Java consultancy in Asia. Our Java teams build banks.", KEEP),
    ("JavaScript is not Java", "Full Stack Developer", "Python, Django, JavaScript, React and PostgreSQL.\nJavaScript ES6.", KEEP),
    (".NET required twice", "Software Engineer", "Python scripting.\nStrong C# and .NET Core.\nASP.NET Web API development.", "Different stack: .NET"),
    ("Kotlin required", "Backend Developer", "Backend services in Kotlin.\nKotlin coroutines.\nSome Python tooling.", "Different stack: Kotlin"),
    ("one stray Java line in a Python job", "Python Developer",
     "Strong Python.\nDjango and FastAPI services.\nPython testing with pytest.\nWork alongside our Java platform team.", KEEP),
    ("AI title that is not a developer job", "AI Specialist", "Manage AI adoption. Python is nice to know.", m.NOT_DEVELOPER_ROLE),
    ("non-developer title even with Python and GenAI in the text", "AI & BI Enablement", GEN, m.NOT_DEVELOPER_ROLE),
    ("Computer Scientist is a developer title", "Computer Scientist-I (Python)", PY, KEEP),
    ("research title", "AI Research Engineer (Model Compression)", GEN, m.NOT_DEVELOPER_ROLE),
    ("data scientist title", "Data Scientist - Generative AI", GEN, m.NOT_DEVELOPER_ROLE),
    ("front-end title", "AI Web Developer - Frontend & Web Platforms", GEN, "Different stack: Front end only"),
    ("RPA title", "Senior Software Engineer-Automation Anywhere - AI", PY, "Different stack: RPA / low-code"),
    ("ops title", "Senior AI-Ops Engineer (AWS & Agentic AI)", GEN, "Different stack: DevOps / infrastructure"),
    ("data-and-AI engineer title", "Lead Expert Data & AI Engineer - Manufacturing", PY, "Different stack: Data engineering / BI"),
    ("ML engineer / data scientist is decided by the description", "Machine Learning Engineer/Data Scientist", PY, KEEP),
    ("AI title without role noun, real GenAI dev work", "GEN AI", GEN, KEEP),
    ("no description", "Software Engineer", "", KEEP),
]
for name, title, desc, want in cases:
    check(name, m.role_mismatch(job(title, desc)), want)

print("rejection_reason reports the role reason")
check("Java title that reads as a target role", m.rejection_reason(job("Java Full Stack Developer", PY + " 2 years experience.")), "Different stack: Java")
check("title that was never a target role", m.rejection_reason(job("Java Developer", PY + " 2 years experience.")), "Job title")
check("fitting job passes", m.rejection_reason(job("Python Developer", PY + " 2 years experience.")), "")
check("experience still checked first", m.rejection_reason(job("Python Developer", PY + " 5+ years experience.")), "Experience")

print("Remote flag against the description")
pune = dict(location="Pune, Maharashtra, India", workplace_type="Remote")
check("remote-flagged, description silent -> eligible", m.location_is_eligible(job("Python Developer", PY, **pune)), True)
check("remote-flagged, description says remote -> eligible",
      m.location_is_eligible(job("Python Developer", PY + " This is a fully remote role in a hybrid cloud team.", **pune)), True)
check("remote-flagged, description says hybrid working model -> not eligible",
      m.location_is_eligible(job("Python Developer", PY + " Candidates must be based in Pune for our hybrid working model.", **pune)), False)
check("remote-flagged, 'hybrid cloud' is not a work mode",
      m.location_is_eligible(job("Python Developer", PY + " We run hybrid cloud environments.", **pune)), True)
check("target city is eligible whatever the work mode",
      m.location_is_eligible(job("Python Developer", PY + " Work from office 5 days a week in the office.")), True)

print("Score")
j = m.score_job(job("Software Engineer", "Azure, Git and SQL."))
check("three skills are scored out of five", j["skill_match_pct"], 60)
j = m.score_job(job("Python Developer", "Python, Django, FastAPI, React, PostgreSQL, Docker and Redis."))
check("seven of seven", j["skill_match_pct"], 100)
j = m.score_job(job("Python Developer", "Python, Django, FastAPI, React, PostgreSQL.\nNice to have: Kafka and AWS."))
check("nice-to-have skills count half", j["skill_match_pct"], round(5 / 6 * 100))
check("   and are still listed as missing", j["missing_skills"], ["aws", "kafka"])
j = m.score_job(job("Python Developer", "Python, Django, Kubernetes (k8s), MySQL, SQLite, MariaDB, Oracle Database and PL/SQL."))
check("Kubernetes and every SQL dialect are skills the candidate has", (j["skill_match_pct"], j["missing_skills"]), (100, []))
check("   each dialect is named, and plain SQL is not counted a second time", j["matched_skills"],
      ["django", "kubernetes", "mariadb", "mysql", "oracle sql", "python", "sqlite"])
j = m.score_job(job("Python Developer", "Python, Django, T-SQL, Oracle Fusion and Oracle EBS."))
check("T-SQL counts as SQL; Oracle business applications are still missing", (j["matched_skills"], j["missing_skills"]),
      (["django", "python", "sql"], ["oracle"]))
j = m.score_job(job("Software Engineer", "Python, Django, React, PostgreSQL and Docker.\nOne of Python, Java or Go."))
check("alternative language is not counted as missing", (j["skill_match_pct"], j["missing_skills"]), (100, []))
j = m.score_job(job("Python Developer", "Python, Django, FastAPI, React, PostgreSQL.\nAbout Us:\nWe build SAP and Salesforce and Java systems."))
check("company blurb skills are ignored", (j["skill_match_pct"], j["missing_skills"]), (100, []))
j = m.score_job(job("Software Engineer", "Great team, great culture."))
check("no skills -> 0", j["skill_match_pct"], 0)

print("Duplicate openings")
a = job("Platform Software Engineer 2", "Same text " * 30, job_id="1", location="Ahmedabad, Gujarat, India", company="Oracle")
b = job("Platform Software Engineer 2", "Same text " * 30, job_id="2", location="Hyderabad, Telangana, India", company="Oracle")
c = job("Platform Software Engineer 2", "Same text " * 30, job_id="3", location="Trivandrum, Kerala, India", company="Oracle", is_easy_apply=True)
d = job("Platform Software Engineer 2", "Another opening entirely " * 30, job_id="4", company="Oracle")
e = job("Platform Software Engineer 2", "Same text " * 30, job_id="5", company="Other Co")
merged = m.merge_duplicate_postings([a, b, c, d, e])
check("five rows -> three openings", len(merged), 3)
first = [x for x in merged if x["job_id"] in ("1", "2", "3")][0]
check("the copy in a target city is kept", first["job_id"], "2")
check("other copies are remembered", sorted(first["duplicate_ids"]), ["1", "3"])
check("other cities are shown", m.location_label(first), "Hyderabad, Telangana, India (also posted for: Trivandrum, Ahmedabad)")
check("a single opening has no suffix", m.location_label([x for x in merged if x["job_id"] == "4"][0]), "Hyderabad, Telangana, India")

print("History remembers openings as well as ids")
with tempfile.TemporaryDirectory() as tmp:
    m.DATA_DIR = Path(tmp)
    m.STATE_FILE = Path(tmp) / "seen.json"
    m.STATE_FILE.write_text(json.dumps({"seen_job_ids": ["11", "12"]}), encoding="utf-8")  # old-format file
    seen = m.load_seen_ids()
    check("old history file still loads", seen, {"11", "12"})
    seen.update({"13", m.SEEN_KEY_PREFIX + "abc"})
    m.save_seen_ids(seen)
    again = m.load_seen_ids()
    check("ids and fingerprints survive a save", again, {"11", "12", "13", m.SEEN_KEY_PREFIX + "abc"})

print("Search workers")
with tempfile.TemporaryDirectory() as tmp:
    shard_dir = Path(tmp) / "shards"
    now = m.datetime.now(m.timezone.utc).isoformat()

    def rec(job_id, loc, desc="Python Django developer, 2 years."):
        return {"job_id": job_id, "source": "LinkedIn", "job_url": f"https://www.linkedin.com/jobs/view/{job_id}/",
                "title": "Python Developer", "company": "Acme", "location": loc, "description": desc,
                "age_hours": 0.3, "scraped_at": now, "workplace_type": "Not specified"}

    terms = list(m.SEARCH_TERMS)
    half = len(terms) // 2

    def pairs(location, keywords=None):
        return [(t, location) for t in (keywords or terms)]

    m.RUN_NOTES.clear()
    m.write_shard(shard_dir / "Hyderabad.json", pairs("Hyderabad"), [rec("4100000001", "Hyderabad, Telangana, India")])
    m.write_shard(shard_dir / "Chennai.json", pairs("Chennai"), [rec("4100000002", "Chennai, Tamil Nadu, India"),
                                                                 rec("4100000009", "Chennai, Tamil Nadu, India", desc="")])
    # India-remote is split over two workers; the second one never delivered.
    m.write_shard(shard_dir / "Remote-1.json", pairs("Remote", terms[:half]),
                  [rec("4100000002", "Chennai, Tamil Nadu, India", desc=""),
                   {"job_id": "4100000003", "title": "Old", "location": "India", "age_hours": 5.0}])
    (shard_dir / "broken.json").write_text("{not json", encoding="utf-8")
    calls = []

    def fake_collect(**kwargs):
        calls.append(kwargs)
        return [rec("41000000" + str(10 + len(calls)), "Bengaluru, Karnataka, India")]

    enriched = []
    m.collect_jobs = fake_collect
    m.enrich_linkedin_jds = lambda jobs, **kw: enriched.extend(j["job_id"] for j in jobs) or jobs
    jobs = m.collect_from_shards(shard_dir, hours=1.0, target_count=0, delay=1.0)
    ids = sorted(j["job_id"] for j in jobs)
    check("jobs from all workers are merged with the searches the report job had to run itself", ids,
          ["4100000001", "4100000002", "4100000009", "4100000011", "4100000012"])
    check("exactly the searches no worker covered are run again",
          sorted((c["location"], c["keywords"]) for c in calls),
          sorted([("Bengaluru", ",".join(terms)), ("Remote", ",".join(terms[half:]))]))
    check("the copy with a description wins", [j for j in jobs if j["job_id"] == "4100000002"][0]["description"] != "", True)
    check("a job without a description gets a second attempt", enriched, ["4100000009"])
    check("the report is told which searches were missing",
          any("Bengaluru" in n for n in m.RUN_NOTES) and any("Remote" in n for n in m.RUN_NOTES), True)
    check("   and the mail shows it", "Bengaluru" in m.run_notes_html(), True)

    m.RUN_NOTES.clear()
    calls.clear()
    m.write_shard(shard_dir / "Bengaluru.json", pairs("Bengaluru"), [])
    m.write_shard(shard_dir / "Remote-2.json", pairs("Remote", terms[half:]), [])
    m.collect_from_shards(shard_dir, hours=1.0, target_count=0, delay=1.0)
    check("every search covered -> the report job searches nothing and adds no note", (calls, m.RUN_NOTES), ([], []))

    m.collect_from_shards(shard_dir, hours=1.0, target_count=0, delay=1.0, keywords="Rust Developer")
    check("a keyword the workers did not search is searched by the report job in every location",
          sorted(c["location"] for c in calls), sorted(m.SEARCH_LOCATIONS))

    m.RUN_NOTES.clear()
    calls.clear()
    m.collect_from_shards(Path(tmp) / "nothing-here", hours=1.0, target_count=0, delay=1.0)
    check("no worker files at all -> everything is searched by the report job",
          sorted(c["location"] for c in calls), sorted(m.SEARCH_LOCATIONS))

print()
print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
