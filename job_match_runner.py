#!/usr/bin/env python3
"""Scheduled job collector, skills-overlap scorer, and GitHub Issue notifications.

The runner scores collected jobs, keeps a seen-job history, writes a two-sheet
Excel report, and creates an assigned GitHub Issue for new 60%+ matches.
Features a built-in LinkedIn Guest-Endpoint Scraper with anti-blocking behavior,
rotating browser headers, exponential backoff with jitter, proxy support, and
configurable hours / posted-before freshness filter (e.g., 1 hour ago).
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import smtplib
import ssl
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False


LOG = logging.getLogger("job_match_runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "seen_job_ids.json"
REPORT_DIR = ROOT / "reports"

# ---------------------------------------------------------------------------
# Anti-Blocking & Browser Emulation
# ---------------------------------------------------------------------------
USER_AGENTS: List[str] = [
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Safari macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _get_browser_headers() -> Dict[str, str]:
    """Generates realistic modern browser headers for anti-blocking."""
    ua = random.choice(USER_AGENTS)
    is_windows = "Windows" in ua
    platform = '"Windows"' if is_windows else ('"macOS"' if "Macintosh" in ua else '"Linux"')
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


class LinkedInBackoff:
    """Manages exponential backoff with jitter and Retry-After header parsing."""
    def __init__(self, base_delay: float = 3.0, max_delay: float = 60.0, max_retries: int = 5):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.max_retries = max_retries
        self.failures = 0

    def wait_seconds(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                val = float(retry_after)
                if 0 < val <= self.max_delay:
                    return val + random.uniform(0.5, 1.5)
            except ValueError:
                pass
        w = min(self.max_delay, self.base_delay * (2 ** attempt))
        return w + random.uniform(1.0, 3.0)

    def record_failure(self) -> None:
        self.failures += 1

    def reset(self) -> None:
        self.failures = 0

    @property
    def exhausted(self) -> bool:
        return self.failures >= self.max_retries


def _resolve_proxy(proxy_arg: str | None = None) -> str | None:
    """Resolves proxy configuration from arguments or standard environment variables."""
    p = (
        proxy_arg
        or os.environ.get("LINKEDIN_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("ALL_PROXY")
    )
    if not p or not p.strip():
        return None
    p = p.strip()
    # Guard against dummy or invalid placeholder strings
    if "host:port" in p or "example.com" in p or p.lower() in ("none", "null", "false", "undefined", "empty"):
        return None
    if not (p.startswith("http://") or p.startswith("https://") or p.startswith("socks5://")):
        p = f"http://{p}"
    return p


# ---------------------------------------------------------------------------
# Candidate Profile & Skill Vocabulary
# ---------------------------------------------------------------------------
# Based on the candidate profile supplied for this job-search workflow.
# Canonical names are used for scoring; aliases are normalised below.
CANDIDATE_SKILLS = {
    # Languages and application development
    "python", "javascript", "sql", "django", "django rest framework", "fastapi",
    "rest api", "react", "html5", "css3", "responsive ui",
    # AI / LLM engineering (as stated in the uploaded resume)
    "azure openai", "openai api", "gpt-4o", "langchain", "langgraph",
    "ai agents", "crewai", "llamaindex", "prompt engineering", "function calling",
    "structured outputs", "rag", "embedding search", "vector search", "pgvector",
    "faiss", "azure ai search", "document chunking", "fine-tuning",
    # Backend, data, and asynchronous processing
    "microservices", "websockets", "rabbitmq", "celery", "postgresql",
    "sql server", "mongodb", "redis",
    # Added at the candidate's request: Kubernetes, and every SQL dialect (not only the ones in the resume)
    "kubernetes", "mysql", "sqlite", "mariadb", "oracle sql",
    # Security, delivery, and engineering tools
    "jwt", "rbac", "session management", "multi-tenant", "api security",
    "burp suite", "docker", "nginx", "ci/cd", "git", "github", "postman",
    "power bi",
    # Implied by the resume's AI work: LLM applications, and Azure through Azure OpenAI / Azure AI Search
    "llm", "azure", "generative ai",
}

# Job-description vocabulary. A skill is scored once even if repeated many times.
SKILL_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "python": (r"\bpython\b",),
    "azure openai": (r"\bazure openai\b",),
    "openai api": (r"\bopenai api\b", r"\bopenai platform api\b"),
    "gpt-4o": (r"\bgpt[- ]?4o\b",),
    "langchain": (r"\blangchain\b",),
    "langgraph": (r"\blanggraph\b",),
    "ai agents": (r"\bai agents?\b", r"\bagentic ai\b"),
    "crewai": (r"\bcrewai\b",),
    "llamaindex": (r"\bllamaindex\b", r"\blamaindex\b"),
    "prompt engineering": (r"\bprompt engineering\b",),
    "function calling": (r"\bfunction calling\b", r"\btool calling\b"),
    "structured outputs": (r"\bstructured outputs?\b",),
    "pgvector": (r"\bpgvector\b",),
    "faiss": (r"\bfaiss\b",),
    "azure ai search": (r"\bazure ai search\b",),
    "document chunking": (r"\bdocument chunking\b", r"\bchunking\b"),
    "fine-tuning": (r"\bfine[- ]tuning\b", r"\bfine tuned\b"),
    "html5": (r"\bhtml5?\b",),
    "css3": (r"\bcss3?\b",),
    "responsive ui": (r"\bresponsive (?:ui|user interface|web design)\b",),
    "postman": (r"\bpostman\b",),
    "power bi": (r"\bpower bi\b",),
    "github": (r"\bgithub\b",),
    "django": (r"\bdjango\b",),
    "django rest framework": (r"\bdjango rest framework\b", r"\bdrf\b"),
    "fastapi": (r"\bfastapi\b",),
    "flask": (r"\bflask\b",),
    "javascript": (r"\bjavascript\b",),
    "typescript": (r"\btypescript\b",),
    "react": (r"\breact(?:\.js)?\b",),
    "node.js": (r"\bnode(?:\.js)?\b",),
    "rest api": (r"\brest(?:ful)? apis?\b", r"\brest api development\b"),
    "graphql": (r"\bgraphql\b",),
    "sql": (r"\bsql\b",),
    "postgresql": (r"\bpostgres(?:ql)?\b",),
    "sql server": (r"\bsql server\b", r"\bmssql\b"),
    "mysql": (r"\bmysql\b",),
    "sqlite": (r"\bsqlite\d?\b",),
    "mariadb": (r"\bmariadb\b",),
    # Oracle as a SQL database. Oracle's business applications are the separate "oracle" entry below.
    "oracle sql": (r"\boracle\s+(?:db|database|sql)\b", r"\bpl\s*/?\s*sql\b"),
    "mongodb": (r"\bmongodb\b",),
    "redis": (r"\bredis\b",),
    "rabbitmq": (r"\brabbitmq\b",),
    "celery": (r"\bcelery\b",),
    "docker": (r"\bdocker\b",),
    "kubernetes": (r"\bkubernetes\b", r"\bk8s\b"),
    "nginx": (r"\bnginx\b",),
    "ci/cd": (r"\bci\s*/\s*cd\b", r"\bcontinuous integration\b", r"\bcontinuous deployment\b"),
    "git": (r"\bgit\b", r"\bgithub\b"),
    "aws": (r"\baws\b", r"\bamazon web services\b"),
    "azure": (r"\bazure\b",),
    "gcp": (r"\bgcp\b", r"\bgoogle cloud platform\b"),
    "jwt": (r"\bjwt\b", r"\bjson web tokens?\b"),
    "rbac": (r"\brbac\b", r"\brole[- ]based access control\b"),
    "session management": (r"\bsession management\b",),
    "multi-tenant": (r"\bmulti[- ]tenant\b",),
    "api security": (r"\bapi security\b", r"\bapi authentication\b", r"\bapi authorization\b"),
    "burp suite": (r"\bburp suite\b",),
    "rag": (r"\brag\b", r"\bretrieval[- ]augmented generation\b"),
    "embedding search": (r"\bembedding(?:s)?\b", r"\bsemantic search\b"),
    "vector search": (r"\bvector search\b", r"\bvector database\b", r"\bvector db\b"),
    "llm": (r"\bllms?\b", r"\blarge language models?\b"),
    "generative ai": (r"\bgen(?:erative)?[- ]?ai\b",),
    "langchain": (r"\blangchain\b",),
    "websockets": (r"\bwebsockets?\b",),
    "microservices": (r"\bmicroservices?\b",),
    "linux": (r"\blinux\b",),
    "kafka": (r"\bkafka\b",),
    "pandas": (r"\bpandas\b",),
    "pytorch": (r"\bpytorch\b",),
    "tensorflow": (r"\btensorflow\b",),
    # Other stacks. The candidate does not have these, so a posting built on them must lose score;
    # without them here a Java or .NET role that mentions Python once scored 100%.
    "java": (r"\bjava\b", r"\bj2ee\b"),
    "spring": (r"\bspring\b",),
    ".net": (r"(?<![\w.])\.net\b", r"\bdotnet\b", r"\basp\.net\b"),
    "c#": (r"(?<![\w+#])c#(?!\w)",),
    "c++": (r"(?<![\w+#])c\+\+(?!\w)",),
    "golang": (r"\bgolang\b", r"\bgo\s+(?:developer|engineer|programming|language)\b"),
    "rust": (r"\brust\b",),
    "php": (r"\bphp\b", r"\blaravel\b"),
    "ruby": (r"\bruby\b",),
    "scala": (r"\bscala\b",),
    "kotlin": (r"\bkotlin\b",),
    "swift": (r"\bswift(?:ui)?\b",),
    "android": (r"\bandroid\b",),
    "ios": (r"\bios\b",),
    "angular": (r"\bangular(?:js)?\b",),
    "vue": (r"\bvue(?:\.js)?\b",),
    "next.js": (r"\bnext\.?js\b",),
    "express": (r"\bexpress\.?js\b",),
    "terraform": (r"\bterraform\b",),
    "ansible": (r"\bansible\b",),
    "jenkins": (r"\bjenkins\b",),
    "spark": (r"\bspark\b", r"\bpyspark\b"),
    "hadoop": (r"\bhadoop\b",),
    "snowflake": (r"\bsnowflake\b",),
    "databricks": (r"\bdatabricks\b",),
    "airflow": (r"\bairflow\b",),
    "oracle": (r"\boracle\s+(?:apex|ebs|fusion|cloud)\b",),
    "elasticsearch": (r"\belasticsearch\b",),
    "cassandra": (r"\bcassandra\b",),
    "dynamodb": (r"\bdynamodb\b",),
    "tableau": (r"\btableau\b",),
    "selenium": (r"\bselenium\b",),
    "salesforce": (r"\bsalesforce\b",),
    "sap": (r"\bsap\b",),
    "servicenow": (r"\bservicenow\b",),
    "embedded": (r"\bembedded\s+(?:systems?|software|c|linux)\b", r"\bfirmware\b"),
}

# Current search preferences supplied by the candidate:
# Strictly: Hyderabad, Bengaluru/Bangalore, Chennai (ALL work modes: On-site, In-office, Hybrid, Remote, WFH) and India Remote.
# Worldwide remote and other cities (e.g. Pune, Noida, Gurgaon, Mumbai) are strictly excluded.
TARGET_CITIES = (
    "hyderabad", "secunderabad",
    "bengaluru", "bangalore", "bgl",
    "chennai", "madras",
)
ALLOWED_LOCAL_WORK_MODES = {"onsite", "on-site", "in-office", "inoffice", "hybrid", "remote"}

REMOTE_INDIA_PATTERNS = (
    r"\bindia\b", r"\bindia[- ]eligible\b", r"\bopen to applicants in india\b",
)
REMOTE_ONLY_OUTSIDE_INDIA_PATTERNS = (
    r"\b(?:united states|usa|us only|canada only|uk only|united kingdom only|"
    r"australia only|europe only|emea only|germany only|france only|singapore only)\b",
)

SEARCH_TERMS = [
    "AI Engineer",
    "Generative AI",
    "LLM Engineer",
    "Software Engineer",
    "Python Developer",
    "Django",
    "Full Stack Developer",
    "Backend Developer",
]

# Each term is searched in each of these places. Searching all of India and filtering afterwards
# filled LinkedIn's result cap with other cities and returned about half as many usable jobs.
# "Remote" means remote jobs anywhere in India.
SEARCH_LOCATIONS = ["Hyderabad", "Bengaluru", "Chennai", "Remote"]

# Target role titles explicitly requested by candidate:
# "ai engineer, software engineer, python backend, python full stack, python jobs only"
ALLOWED_TITLE_PATTERNS = (
    # AI Engineer & GenAI
    r"\b(?:ai|a\.i\.|artificial intelligence|genai|gen ai|generative ai|llm|machine learning|ml)\b",
    # Software Engineer & SDE
    r"\bsoftware\s+(?:development\s+)?engineer\b",
    r"\bsoftware\s+developer\b",
    r"\bsde\b",
    r"\bsde[- ]?(?:1|2|i|ii)\b",
    r"\bmember of technical staff\b",
    r"\bmts\b",
    # Python Backend
    r"\b(?:backend|back[- ]end)\b",
    # Python Full Stack
    r"\b(?:full[- ]stack|fullstack)\b",
    # Python jobs
    r"\bpython\b",
    r"\bdjango\b",
    r"\bfastapi\b",
    r"\bflask\b",
)

_OTHER_FAMILY_TITLES = (
    r"\bqa\b",
    r"\bquality assurance\b",
    r"\btester\b",
    r"\btesting\b",
    r"\btest engineer\b",
    r"\bsdet\b",
    r"\bsystem performance\b",
    r"\bperformance engineer\b",
    r"\bhardware\b",
    r"\bfpga\b",
    r"\basic\b",
    r"\bmechanical\b",
    r"\bcivil\b",
    r"\belectrical\b",
    r"\bnetwork engineer\b",
    r"\bsalesforce\b",
    r"\bsap\b",
    r"\bscrum master\b",
    r"\bproduct manager\b",
    r"\bproject manager\b",
    r"\btechnical writer\b",
    r"\brecruiter\b",
    r"\bhr\b",
)
# Full-time only: exclude internships, trainees, contractors, evaluators, part-time
_NOT_FULL_TIME_TITLES = (
    r"\bintern\b",
    r"\binternship\b",
    r"\binterns\b",
    r"\btrainee\b",
    r"\bapprentice\b",
    r"\bpart[- ]time\b",
    r"\bcontract\b",
    r"\bcontractor\b",
    r"\bfreelance\b",
    r"\bevaluator\b",
    r"\brater\b",
    r"\bannotat(?:or|ion)\b",
)
# Over-senior roles (requires 5-15+ years experience: Staff, Principal, Director, VP, Architect, etc.)
_OVER_SENIOR_TITLES = (
    r"\b(?:staff|principal|distinguished)\b",
    r"\barchitect\b",
    r"\b(?:director|assoc(?:iate)?\.?\s*director|vp|vice[- ]president|head\s+of)\b",
    r"\b(?:engineering|technical)\s+manager\b",
    r"\b(?:sde|swe|engineer|developer)[- ]?(?:3|4|5|6|iii|iv|v|vi)\b",
    r"\b(?:iii|iv|v|vi)\b",
)
EXCLUDED_TITLE_KEYWORDS = _OTHER_FAMILY_TITLES + _NOT_FULL_TIME_TITLES + _OVER_SENIOR_TITLES

# Everything above this line describes one candidate: the built-in profile. A run started with
# --profile replaces those settings with one candidate's file (see apply_profile, further down),
# and these switches say how the rules below then behave.
PROFILE: Dict[str, Any] = {}        # the candidate this run is for; empty = the built-in profile
PYTHON_RULES = True                 # the detailed Python-role rules apply (analyse_role_fit)
MUST_HAVE: List[Tuple[str, "re.Pattern[str]"]] = []  # general rules: a posting must name one of these
AVOID_STACKS: Set[str] = set()      # general rules: job families whose titles are dropped
OWN_TITLE_WORDS: List["re.Pattern[str]"] = []        # general rules: a title naming one of these is the candidate's own
ALLOW_REMOTE_INDIA = True           # remote jobs open to India are wanted
SENIOR_TITLES_OK = False            # "Senior" / "Lead" titles are wanted even when the posting states no years
MAIL_EVERY_RUN = True               # the hourly report is mailed even when it has nothing new
ROLE_NOTE = "Only developer roles that use Python are listed (Java, .NET and other stacks are removed)"
ROLE_SHORT = "a Python developer role"
PLACES_NOTE = "Hyderabad, Bengaluru or Chennai (or remote in India)"


def employment_is_eligible(job: Dict[str, Any]) -> bool:
    """Ensure the role is strictly full-time (no internships, contract, or part-time)."""
    emp = str(job.get("employment_type") or "").lower().strip()
    if emp:
        disallowed = ("intern", "part-time", "contract", "temporary", "volunteer")
        if any(d in emp for d in disallowed):
            return False
    return True



def title_is_eligible(job: Dict[str, Any]) -> bool:
    """Validate that the job title strictly matches one of the candidate's target roles:
    AI Engineer, Software Engineer, Python Backend, Python Full Stack, or Python jobs.
    Excludes QA, testing, hardware, performance, non-software roles, and titles that name
    another stack (Java, .NET, DevOps, ...).
    """
    return title_is_target_role(job) and not title_other_stack(str(job.get("title") or ""))


def title_is_target_role(job: Dict[str, Any]) -> bool:
    """The title reads as one of the target roles, before asking which stack it is ("Java Full Stack Developer" passes)."""
    title = str(job.get("title") or "").strip().lower()
    if not title:
        return False
    if any(re.search(pat, title, re.I) for pat in EXCLUDED_TITLE_KEYWORDS):
        return False
    return any(re.search(pat, title, re.I) for pat in ALLOWED_TITLE_PATTERNS)


# ---------------------------------------------------------------------------
# Role fit: is the posting a job for the candidate's stack at all?
# ---------------------------------------------------------------------------
# The candidate builds Python back ends and GenAI / LLM features. A posting is dropped when
#   1. its title names another stack or another kind of job (Java, .NET, DevOps, ...), or
#   2. its posting never names Python (the candidate's GenAI work is Python work), or
#   3. its description requires another programming language at least as much as Python.
# A language that is only offered as an alternative ("Python, Java or Go") or only listed under
# nice-to-have does not count against the posting.

NOT_DEVELOPER_ROLE = "Not a developer role"
DIFFERENT_STACK = "Different stack"
NO_PYTHON = "No Python in the posting"
NO_MUST_HAVE = "None of your must-have skills in the posting"

# Title words that name another stack or job family. Checked on the title only.
TITLE_OTHER_STACKS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Java", re.compile(r"\bjava\b|\bj2ee\b|\bspring\s?boot\b|\bspring\b")),
    ("Kotlin", re.compile(r"\bkotlin\b")),
    (".NET", re.compile(r"(?<![\w.])\.net\b|\bdot\s?net\b|(?<![\w+#])c#|\bc\s?sharp\b|\basp\b")),
    ("C++", re.compile(r"(?<![\w+#])c\+\+|\bc\s*/\s*c\+\+|\bqt\b")),
    ("Go", re.compile(r"\bgolang\b|\bgo\s+(?:developer|engineer|lang|programmer)\b|\(\s*go\s*\)|\bgo\s*/|/\s*go\b")),
    ("Rust", re.compile(r"\brust\b")),
    ("PHP", re.compile(r"\bphp\b|\blaravel\b|\bwordpress\b|\bdrupal\b|\bmagento\b")),
    ("Ruby", re.compile(r"\bruby\b|\brails\b|\bror\b")),
    ("Scala", re.compile(r"\bscala\b")),
    ("Node.js", re.compile(r"\bnode(?:\.?js)?\b|\bnest\.?js\b|\bmern\b|\bmean\s+stack\b")),
    ("Angular", re.compile(r"\bangular(?:js)?\b|\bvue(?:\.?js)?\b")),
    ("Mobile", re.compile(r"\bandroid\b|\bios\b|\bflutter\b|\breact\s+native\b|\bswift\b|\bmobile\b")),
    ("Salesforce / SAP / ERP", re.compile(
        r"\bsalesforce\b|\bsfdc\b|\bsap\b|\babap\b|\bservicenow\b|\bpega\b|\bsharepoint\b|\bdynamics\b|"
        r"\bpower\s?(?:apps|platform|automate)\b|\boracle\b|\bpl\s*/?\s*sql\b|\bworkday\b|\bodoo\b|\bmulesoft\b|\berp\b|\bcrm\b")),
    ("Mainframe", re.compile(r"\bmainframes?\b|\bcobol\b|\bas\s?/?\s?400\b")),
    ("Embedded", re.compile(r"\bembedded\b|\bfirmware\b|\brtos\b|\bautosar\b|\bdevice\s+drivers?\b|\bbsp\b|\biot\b|\bplc\b")),
    ("DevOps / infrastructure", re.compile(
        r"\bdevops\b|\bdevsecops\b|\bsre\b|\bsite\s+reliability\b|\binfrastructure\b|\bops\b|\b[a-z]+ops\b|\bnetwork\w*\b|"
        r"\bcloud\s+(?:engineer|architect|admin\w*|operations)\b|"
        r"\bsecurity\b|\bcyber\w*\b|\bsystems?\s+admin\w*\b|\bdba\b|\bdatabase\s+admin\w*\b")),
    ("Data engineering / BI", re.compile(
        r"\bdata\b[^,;|()]{0,14}\bengineer\w*\b|\betl\b|\bbig\s+data\b|\bdata\s+warehouse\b|\bbi\s+developer\b|\bbusiness\s+intelligence\b|"
        r"\btableau\b|\bpower\s?bi\b|\bdatabricks\b|\bsnowflake\b|\bdata\s+analyst\b|\banalytics\s+engineer\b")),
    ("RPA / low-code", re.compile(
        r"\brpa\b|\bautomation\s+anywhere\b|\buipath\b|\bblue\s?prism\b|\blow[- ]code\b|\bno[- ]code\b")),
    ("Front end only", re.compile(r"\bfront[- ]?end\b|\bui\s+(?:developer|engineer)\b")),
    ("Games / graphics", re.compile(r"\bunity\b|\bunreal\b|\bgame\w*\b|\bgraphics\b")),
    ("Blockchain", re.compile(r"\bblockchain\b|\bsolidity\b|\bweb3\b|\bcrypto\w*\b")),
    (NOT_DEVELOPER_ROLE, re.compile(
        r"\bmarketing\b|\bsales\b|\bpre[- ]?sales\b|\bgovernance\b|\btrainer\b|\btutor\b|\bteacher\b|\bfaculty\b|\binstructor\b|"
        r"\bmentor\b|\bcontent\b|\bcopywriter\b|\bbusiness\s+(?:analyst|development)\b|\bcustomer\s+success\b|\bsupport\b|"
        r"\boperations\b|\bdata\s+entry\b|\bresearch(?:er)?\s+(?:analyst|associate)\b|\baccount\w*\b|\bfinance\b|\baudit\w*\b|"
        r"\bcompliance\b|\blegal\b|\bprocurement\b|\bdesigner\b|\bui\s*/\s*ux\b|\bux\b|\bresearch(?:er)?\b|(?<!computer )\bscientist\b|"
        r"\benablement\b|\bevangelist\b")),
]
_TITLE_PYTHON_RE = re.compile(r"\bpython\b|\bdjango\b|\bfastapi\b|\bflask\b")
# Stacks that stay "another stack" even when the title also says Python ("Python DevOps Engineer").
_TITLE_ALWAYS_OTHER = {
    "DevOps / infrastructure", "Data engineering / BI", NOT_DEVELOPER_ROLE, "Embedded", "Mobile",
    "Salesforce / SAP / ERP", "Mainframe", "Games / graphics", "Blockchain", "RPA / low-code",
}


_TITLE_LANGUAGES = {"Java", "Kotlin", ".NET", "C++", "Go", "Rust", "PHP", "Ruby", "Scala"}
_TITLE_LANGUAGE_WORD = r"(?:java|kotlin|c#|\.net|c\+\+|golang|go|rust|php|ruby|scala)"
# Python named as one of a choice: "Java/Python", "Python or Go".
_TITLE_PYTHON_CHOICE_RE = re.compile(
    rf"python[\s(]*(?:\bor\b|/|\|)[\s(]*{_TITLE_LANGUAGE_WORD}|{_TITLE_LANGUAGE_WORD}[\s)]*(?:\bor\b|/|\|)[\s(]*python"
)
_STRONG_DEVELOPER_TITLE_RE = re.compile(
    r"\bsoftware\s+(?:development\s+)?(?:engineer|developer)\b|\bfull[- ]?stack\b|\bback[- ]?end\s+(?:engineer|developer)\b|"
    r"\bpython\s+(?:engineer|developer)\b|\bsde\b|\b(?:ai|ml|ai\s*/\s*ml|genai|gen\s+ai|llm|machine\s+learning)\s+(?:engineer|developer)\b"
)


def title_other_stack(title: str) -> str:
    """Name the other stack or job family a title belongs to, or "" when the title could be the candidate's role.

    "Java Developer" -> "Java".  "Software Engineer (Java/Python)" -> "" (the description decides).
    """
    lowered = str(title or "").lower()
    if not PYTHON_RULES:
        return _general_title_other_stack(lowered)
    names_python = bool(_TITLE_PYTHON_RE.search(lowered))
    python_is_a_choice = bool(_TITLE_PYTHON_CHOICE_RE.search(lowered))
    for name, pattern in TITLE_OTHER_STACKS:
        if not pattern.search(lowered):
            continue
        if name == NOT_DEVELOPER_ROLE:
            # "Senior Full Stack Engineer - Marketing Capabilities" is a developer job in a marketing team.
            if _STRONG_DEVELOPER_TITLE_RE.search(lowered):
                continue
        elif name in _TITLE_LANGUAGES:
            # "Software Engineer (Java/Python)" may be a Python job; "C/C++ programmer, Linux, Python" is not.
            if python_is_a_choice:
                continue
        elif names_python and name not in _TITLE_ALWAYS_OTHER:
            continue
        return name
    return ""


# Job families a candidate on the general rules can also choose to avoid; the built-in list has no
# entry for them because Python is the built-in profile's own stack.
_GENERAL_ONLY_STACKS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Python", _TITLE_PYTHON_RE),
    ("QA / testing", re.compile(r"\bqa\b|\bquality\s+assurance\b|\btest(?:er|ing)?\b|\bsdet\b")),
]


def avoidable_stacks() -> List[str]:
    """Every job family a candidate's file may name under avoid_stacks."""
    return [name for name, _ in TITLE_OTHER_STACKS + _GENERAL_ONLY_STACKS]


def _general_title_other_stack(lowered: str) -> str:
    """title_other_stack for a candidate on the general rules: only the families the candidate chose to avoid."""
    names_own = any(pattern.search(lowered) for pattern in OWN_TITLE_WORDS)
    for name, pattern in TITLE_OTHER_STACKS + _GENERAL_ONLY_STACKS:
        if name not in AVOID_STACKS or not pattern.search(lowered):
            continue
        if name == NOT_DEVELOPER_ROLE and _STRONG_DEVELOPER_TITLE_RE.search(lowered):
            continue
        # "Java / .NET Developer" stays for a Java candidate who avoids .NET.
        if names_own and name not in _TITLE_ALWAYS_OTHER:
            continue
        return name
    return ""


# Python itself, or a library that only exists in Python.
PYTHON_FAMILY_RE = re.compile(
    r"\b(?:python|django|fastapi|flask|pyspark|pytorch|pandas|numpy|pytest|celery|sqlalchemy|scikit[- ]learn|"
    r"langchain|langgraph|llamaindex|crewai)\b",
    re.I,
)
GENAI_RE = re.compile(
    r"\b(?:gen(?:erative)?[- ]?ai|llms?|large\s+language\s+models?|langchain|langgraph|llamaindex|crewai|autogen|"
    r"rag|retrieval[- ]augmented|prompt\s+engineering|openai|gpt[- ]?\d\w*|agentic|ai\s+agents?|"
    r"vector\s+(?:search|databases?|dbs?|stores?)|embeddings?|hugging\s?face|fine[- ]tun\w+)\b",
    re.I,
)
_DEVELOPER_TITLE_RE = re.compile(
    r"\b(?:engineer\w*|developer|programmer|sde|swe|mts|member\s+of\s+technical\s+staff|full[- ]?stack|back[- ]?end|"
    r"development|coder|computer\s+scientist|tech(?:nical)?\s+lead)\b",
    re.I,
)
# Programming languages the candidate does not work in. Value: (pattern, skill names used in SKILL_PATTERNS).
OTHER_LANGUAGES: Dict[str, Tuple["re.Pattern[str]", Tuple[str, ...]]] = {
    "Java": (re.compile(r"\b(?:java|j2ee|jee|spring\s?boot|spring\s+(?:framework|mvc|cloud|security|data|batch)|hibernate|jvm)\b", re.I), ("java", "spring")),
    "Kotlin": (re.compile(r"\bkotlin\b", re.I), ("kotlin",)),
    ".NET": (re.compile(r"(?<![\w.])\.net\b|\bdot\s?net\b|\basp\.net\b|(?<![\w+#])c#(?!\w)|\bc\s?sharp\b|\bvb\.net\b", re.I), (".net", "c#")),
    "C++": (re.compile(r"(?<![\w+#])c\+\+(?!\w)|\bpro\*c\b|\bc\s+(?:programming|language|developer)\b|\bembedded\s+c\b", re.I), ("c++",)),
    "Go": (re.compile(r"\bgolang\b|\bgo\s+(?:developer|engineer|programming|language|lang|services?|microservices?)\b|(?:[,/(]|\bor\b|\band\b)\s*go\s*(?=[,/).;]|$)", re.I | re.M), ("golang",)),
    "Rust": (re.compile(r"\brust\b", re.I), ("rust",)),
    "PHP": (re.compile(r"\bphp\b|\blaravel\b|\bsymfony\b|\bcodeigniter\b", re.I), ("php",)),
    "Ruby": (re.compile(r"\bruby\b|\brails\b", re.I), ("ruby",)),
    "Scala": (re.compile(r"\bscala\b", re.I), ("scala",)),
    "Swift / iOS": (re.compile(r"\bswift(?:ui)?\b|\bobjective[- ]c\b|\bios\b", re.I), ("swift", "ios")),
    "Android": (re.compile(r"\bandroid\b", re.I), ("android",)),
}
_LANGUAGE_WORD = (
    r"(?:python|java|kotlin|c#|\.net|c\+\+|c|golang|go|rust|php|ruby|scala|swift|node(?:\.?js)?|typescript|javascript|js|perl)"
)
# "Python or Java", "Java / Python", "Python, Java, or Go"
_LANGUAGE_ALTERNATIVE_RE = re.compile(
    rf"(?<![\w+#.]){_LANGUAGE_WORD}[\s,(]*(?:\bor\b|\band\s*/\s*or\b|/|\|)[\s(]*{_LANGUAGE_WORD}(?![\w+#])", re.I
)
_LANGUAGE_WORD_RE = re.compile(rf"(?<![\w+#.]){_LANGUAGE_WORD}(?![\w+#])", re.I)
_PYTHON_ALTERNATIVE_RE = re.compile(
    rf"\bpython[\s,(]*(?:\bor\b|\band\s*/\s*or\b|/|\|)[\s(]*{_LANGUAGE_WORD}(?![\w+#])|"
    rf"(?<![\w+#.]){_LANGUAGE_WORD}[\s,)]*(?:\bor\b|\band\s*/\s*or\b|/|\|)[\s(]*python\b",
    re.I,
)
_LANGUAGE_LIST_INTRO_RE = re.compile(
    r"\b(?:such\s+as|like|e\.?g|one\s+of|any\s+of|any\s+one|one\s+or\s+more|at\s+least\s+one|either|similar|equivalent|"
    r"most\s+of|some\s+of|several\s+of|any\s+combination|"
    r"including\s+but\s+not\s+limited|(?:any|a)\s+(?:modern\s+|high[- ]level\s+|general[- ]purpose\s+|object[- ]oriented\s+|"
    r"programming\s+|scripting\s+|backend\s+)+languages?)\b",
    re.I,
)
_OPTIONAL_SKILL_RE = re.compile(
    r"\b(?:nice[- ]to[- ]have|good[- ]to[- ]have|preferred|preferably|preferable|is\s+a\s+plus|a\s+plus|would\s+be\s+a\s+plus|"
    r"big\s+plus|bonus|desirable|desired|advantage\w*|optional|not\s+mandatory|not\s+required|familiarity|exposure\s+to|"
    r"basic\s+(?:knowledge|understanding)|added\s+benefit|helpful|beneficial|an\s+asset|ideally|would\s+be\s+(?:good|great|nice))\b",
    re.I,
)
# A sentence about who the job works with or what is being replaced, not about what the applicant must know:
# "work alongside our Java platform team", "migrate legacy Java services to Python".
_NOT_A_REQUIREMENT_RE = re.compile(
    r"\b(?:alongside|collaborat\w+|partner(?:ing|s)?\s+with|stakeholders|migrat\w+|legacy|rewrit\w+|replac\w+|"
    r"(?:java|kotlin|\.net|c#|c\+\+|golang|go|php|ruby|rust|scala|android|ios)\s+(?:[\w-]+\s+){0,2}teams?|"
    r"convert\w*|moving\s+(?:away\s+)?from)\b",
    re.I,
)
# Sentence ends for the role-fit reading. Not after "e.g." / "i.e." / "etc.", which sit inside a list of skills.
_FIT_SENTENCE_BREAK_RE = re.compile(r"(?<![eE]\.g\.)(?<![iI]\.e\.)(?<!etc\.)(?<=[.;!?])\s+")


def _terms(*pairs: Tuple[str, str]) -> List[Tuple[str, "re.Pattern[str]"]]:
    return [(name, re.compile(pattern, re.I)) for name, pattern in pairs]


# Framework and tool names that show which language a job is really built on, even when the language
# list itself is phrased as a choice ("server-side languages such as Java (Spring, Spring Boot) ...").
_ECOSYSTEMS: Dict[str, List[Tuple[str, "re.Pattern[str]"]]] = {
    "Java": _terms(
        ("java", r"\bjava\b"), ("spring", r"\bspring(?:\s?boot)?\b"), ("hibernate", r"\bhibernate\b"),
        ("maven", r"\bmaven\b"), ("gradle", r"\bgradle\b"), ("junit", r"\bjunit\b"), ("jvm", r"\bjvm\b"),
        ("j2ee", r"\bj2ee\b|\bjee\b|\bjsp\b|\bservlets?\b"), ("tomcat", r"\btomcat\b"),
        ("jpa", r"\bjpa\b|\bjdbc\b"), ("struts", r"\bstruts\b"),
    ),
    ".NET": _terms(
        (".net", r"(?<![\w.])\.net\b|\bdot\s?net\b"), ("c#", r"(?<![\w+#])c#(?!\w)|\bc\s?sharp\b"),
        ("asp.net", r"\basp\.net\b"), ("entity framework", r"\bentity\s+framework\b"), ("blazor", r"\bblazor\b"),
        ("wpf", r"\bwpf\b|\bwinforms\b"), ("linq", r"\blinq\b"), ("xamarin", r"\bxamarin\b"),
    ),
}
_PYTHON_ECOSYSTEM = _terms(
    ("python", r"\bpython\b"), ("django", r"\bdjango\b"), ("flask", r"\bflask\b"), ("fastapi", r"\bfastapi\b"),
    ("pandas", r"\bpandas\b"), ("numpy", r"\bnumpy\b"), ("pytest", r"\bpytest\b"), ("celery", r"\bcelery\b"),
    ("pytorch", r"\bpytorch\b"), ("tensorflow", r"\btensorflow\b"), ("scikit", r"\bscikit[- ]learn\b|\bsklearn\b"),
    ("sqlalchemy", r"\bsqlalchemy\b"), ("pyspark", r"\bpyspark\b"), ("asyncio", r"\basyncio\b"),
    ("langchain", r"\blangchain\b|\blanggraph\b|\bllamaindex\b"),
)
# A posting that is about data pipelines and warehouses, with no application or GenAI work, is a
# data-engineering job even when its title says "Software Engineer".
_DATA_ENGINEERING_TERMS = _terms(
    ("etl", r"\betl\b|\belt\b"), ("data pipelines", r"\bdata\s+pipelines?\b"), ("spark", r"\b(?:py)?spark\b"),
    ("databricks", r"\bdatabricks\b"), ("snowflake", r"\bsnowflake\b"), ("airflow", r"\bairflow\b"),
    ("warehouse", r"\bdata\s+(?:warehous\w+|lakes?|lakehouse)\b"), ("bigquery", r"\bbig\s?query\b"),
    ("redshift", r"\bredshift\b"), ("hadoop", r"\bhadoop\b|\bhive\b"), ("dbt", r"\bdbt\b"), ("glue", r"\baws\s+glue\b"),
)
_APP_DEVELOPMENT_RE = re.compile(
    r"\b(?:django|flask|fastapi|react(?:\.?js)?|rest(?:ful)?\s+(?:apis?|services?)|web\s+(?:applications?|apps?|services?|development)|"
    r"front[- ]?end|full[- ]?stack|microservices?|backend\s+(?:services?|apis?|development))\b",
    re.I,
)


_OR_WORD_RE = re.compile(r"\bor\b", re.I)
_STRONG_CHOICE_RE = re.compile(
    r"\b(?:one\s+of|any\s+of|any\s+one|at\s+least\s+one|either|one\s+or\s+more|most\s+of|some\s+of|several\s+of)\b", re.I
)
# Cloud infrastructure work. Three or more of these with no application work is a DevOps job under another title.
_INFRASTRUCTURE_TERMS = _terms(
    ("terraform", r"\bterraform\b"), ("bicep", r"\bbicep\b"), ("ansible", r"\bansible\b"),
    ("cloudformation", r"\bcloudformation\b"), ("pulumi", r"\bpulumi\b"), ("helm", r"\bhelm\b"),
    ("iac", r"\binfrastructure[- ]as[- ]code\b|\biac\b"), ("provisioning", r"\bprovision(?:ing|s)?\b"),
    ("patching", r"\bpatching\b"), ("cloud infrastructure", r"\bcloud\s+infrastructure\b"),
    ("landing zone", r"\blanding\s+zones?\b"), ("networking", r"\bvnets?\b|\bvpcs?\b|\bfirewalls?\b"),
)
# A line that is only a bracketed remark - "(Preferred)", "(Nice to have)" - belongs to the line above it.
_PARENTHETICAL_LINE_RE = re.compile(r"^\s*[(\[][^()\[\]]{1,40}[)\]]\s*$")
# "C/C++/STL/BOOST/Python/Java/REST API": a run of slashes is a skills list, not a choice between two.
_SLASH_CHAIN_RE = re.compile(r"(?:[\w+#.*]+\s*/\s*){3,}[\w+#.*]+")
# Enterprise platforms. A posting that keeps coming back to one of these is a job on that platform.
_PLATFORM_RE = re.compile(
    r"\boracle\s+(?:fusion|ebs|e-business|apps|cloud\s+applications|hcm|scm|erp|apex)\b|\bsap\b|\babap\b|\bs/4\s?hana\b|"
    r"\bsalesforce\b|\bservicenow\b|\bworkday\b|\bdynamics\s+365\b|\bpeoplesoft\b|\bnetsuite\b|\bpega\b|\bmulesoft\b|"
    r"\bautomation\s+anywhere\b|\buipath\b|\bblue\s?prism\b|\bpower\s+(?:apps|automate|platform)\b|\bsharepoint\b",
    re.I,
)
# The description's own opening names the job: "seeking a Product Manager", "Join us as a Data Engineer Expert".
_STATED_ROLE_RE = re.compile(
    r"\b(?:seeking|looking\s+for|hiring|join\s+(?:us\s+)?as|role\s+of|position\s+of|opening\s+for)\s+(?:an?\s+|our\s+)?"
    r"(?:[\w-]+\s+){0,4}?(?P<role>product\s+manager|project\s+manager|program\s+manager|delivery\s+manager|business\s+analyst|"
    r"data\s+engineer|devops\s+engineer|site\s+reliability\s+engineer|qa\s+engineer|test\s+engineer)\b",
    re.I,
)
_STATED_ROLE_REASON = {
    "data engineer": f"{DIFFERENT_STACK}: Data engineering / BI",
    "devops engineer": f"{DIFFERENT_STACK}: DevOps / infrastructure",
    "site reliability engineer": f"{DIFFERENT_STACK}: DevOps / infrastructure",
}
# Title made only of these words ("GEN AI", "Agentic AI / LLM") is a GenAI opening posted without a role word.
_GENAI_TITLE_WORDS = {"gen", "generative", "ai", "genai", "llm", "llms", "ml", "aiml", "agentic", "and"}


def _description_sentences(description: str) -> List[Tuple[str, str]]:
    """Split a description into (sentence, section) pairs; section is 'main', 'preferred' or 'company'."""
    original_lines: List[str] = []
    for line in str(description or "").split("\n"):
        if original_lines and _PARENTHETICAL_LINE_RE.match(line):
            original_lines[-1] = f"{original_lines[-1]} {line.strip()}"
        else:
            original_lines.append(line)
    lowered = [line.lower() for line in original_lines]
    sections = _section_of_each_line(lowered, original_lines)
    out: List[Tuple[str, str]] = []
    for line, section in zip(original_lines, sections):
        for sentence in _FIT_SENTENCE_BREAK_RE.split(line):
            if sentence.strip():
                out.append((sentence, section))
    return out


def analyse_role_fit(job: Dict[str, Any]) -> Dict[str, Any]:
    """Read the description for the role-fit facts. Stored on the job as job['role_fit'].

    python           : Python (or a Python framework) is named in the title or outside the company blurb
    genai            : the posting mentions GenAI / LLM work
    required         : {language: sentences that ask for it with no Python in the same sentence}
    paired           : {language: sentences that ask for it together with Python ("Python and Java")}
    alternative_only : languages that appear only as an alternative to Python or only as nice-to-have
    """
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    company_words = set(re.findall(r"[a-z0-9]+", str(job.get("company") or "").lower()))
    python_sentences = 0
    required: Dict[str, int] = {}
    paired: Dict[str, int] = {}
    soft: Set[str] = set()
    ecosystem: Dict[str, Set[str]] = {name: set() for name in _ECOSYSTEMS}
    python_ecosystem: Set[str] = set()
    data_engineering: Set[str] = set()
    infrastructure: Set[str] = set()
    app_development = False
    platform_sentences: Dict[str, int] = {}
    genai = bool(GENAI_RE.search(title))
    for sentence, section in _description_sentences(description):
        if section == "company":
            continue
        has_python = bool(PYTHON_FAMILY_RE.search(sentence))
        if has_python:
            python_sentences += 1
        genai = genai or bool(GENAI_RE.search(sentence))
        optional = section == "preferred" or bool(_OPTIONAL_SKILL_RE.search(sentence))
        skills_list = bool(_SLASH_CHAIN_RE.search(sentence))
        # Frameworks named inside "at least one of Java/Spring Boot, Python or Rust" do not show the stack.
        explicit_choice = has_python and (
            bool(_STRONG_CHOICE_RE.search(sentence))
            or (bool(_PYTHON_ALTERNATIVE_RE.search(sentence)) and bool(_OR_WORD_RE.search(sentence)))
        )
        if not optional:
            python_ecosystem.update(name for name, pattern in _PYTHON_ECOSYSTEM if pattern.search(sentence))
            if not explicit_choice:
                for stack, terms in _ECOSYSTEMS.items():
                    ecosystem[stack].update(name for name, pattern in terms if pattern.search(sentence))
            infrastructure.update(name for name, pattern in _INFRASTRUCTURE_TERMS if pattern.search(sentence))
            data_engineering.update(name for name, pattern in _DATA_ENGINEERING_TERMS if pattern.search(sentence))
            app_development = app_development or bool(_APP_DEVELOPMENT_RE.search(sentence))
            platform = _PLATFORM_RE.search(sentence)
            # The employer's own name is not a platform requirement (a job at Salesforce, SAP, Oracle ...).
            if platform and not (set(re.findall(r"[a-z0-9]+", platform.group(0).lower())) & company_words):
                family = platform.group(0).lower().split()[0]
                platform_sentences[family] = platform_sentences.get(family, 0) + 1
        present = [name for name, (pattern, _) in OTHER_LANGUAGES.items() if pattern.search(sentence)]
        if not present:
            continue
        has_or = bool(_OR_WORD_RE.search(sentence))
        languages_named = {word.lower() for word in _LANGUAGE_WORD_RE.findall(sentence)}
        alternative = has_python and (
            bool(_LANGUAGE_LIST_INTRO_RE.search(sentence))
            # "Python/Java", "Java or Python": the choice has to include Python. "C# / .NET and Python" does not.
            or (bool(_PYTHON_ALTERNATIVE_RE.search(sentence)) and not skills_list)
            or (bool(_LANGUAGE_ALTERNATIVE_RE.search(sentence)) and has_or and not skills_list)
            # "Laravel (PHP), Node.js (Express/Nest), or Python (Django/FastAPI)": three languages and an "or".
            or (len(languages_named) >= 3 and has_or and not skills_list)
        )
        for name in present:
            if optional or alternative:
                soft.add(name)
            elif _NOT_A_REQUIREMENT_RE.search(sentence):
                continue
            elif has_python:
                paired[name] = paired.get(name, 0) + 1
            else:
                required[name] = required.get(name, 0) + 1
    fit = {
        "python_sentences": python_sentences,
        "python": python_sentences > 0 or bool(PYTHON_FAMILY_RE.search(title)),
        "genai": genai,
        "required": required,
        "paired": paired,
        "alternative_only": sorted(soft - set(required) - set(paired)),
        "offered_as_choice": sorted(soft),
        "ecosystem": {stack: sorted(names) for stack, names in ecosystem.items() if names},
        "python_ecosystem": sorted(python_ecosystem),
        "data_engineering": sorted(data_engineering),
        "infrastructure": sorted(infrastructure),
        "app_development": app_development or bool(_APP_DEVELOPMENT_RE.search(title)),
        "platform_sentences": max(platform_sentences.values(), default=0),
    }
    job["role_fit"] = fit
    return fit


def role_mismatch(job: Dict[str, Any]) -> str:
    """Why the posting is not a role for the candidate, or "" when it is (or cannot be judged)."""
    title = str(job.get("title") or "")
    other = title_other_stack(title)
    if other:
        return other if other == NOT_DEVELOPER_ROLE else f"{DIFFERENT_STACK}: {other}"
    description = str(job.get("description") or "")
    if not description.strip():
        return ""
    if not PYTHON_RULES:
        # General rules: the posting only has to name one of the candidate's must-have skills.
        text = f"{title}\n{description}".lower()
        if MUST_HAVE and not any(pattern.search(text) for _, pattern in MUST_HAVE):
            return NO_MUST_HAVE
        return ""
    fit = analyse_role_fit(job)
    # The candidate's GenAI work is Python work. Postings that talk about AI but never name Python turned
    # out, on hand-checked samples, to be Node, DevOps, C or product-manager jobs six times out of seven.
    if not fit["python"]:
        return NO_PYTHON
    if not _DEVELOPER_TITLE_RE.search(title):
        # A title with no developer word is kept only when it is nothing but GenAI words ("GEN AI") and the
        # description shows GenAI work; "AI & BI Enablement" or "Cost Intelligence & AI Analytics" is not.
        words = set(re.findall(r"[a-z]+", title.lower()))
        if not fit["genai"] or not words or not words <= _GENAI_TITLE_WORDS:
            return NOT_DEVELOPER_ROLE
    stated = _STATED_ROLE_RE.search(description[:1200])
    if stated:
        role = re.sub(r"\s+", " ", stated.group("role").lower())
        return _STATED_ROLE_REASON.get(role, NOT_DEVELOPER_ROLE)
    # Another language is asked for outright ("Strong C# and .NET Core", "5 years of Java").
    for name in sorted(fit["required"], key=lambda n: -fit["required"][n]):
        return f"{DIFFERENT_STACK}: {name}"
    # Asked for together with Python ("Proficiency in Python and Java", "using Python, FastAPI, .NET and React")
    # and nowhere offered as a choice.
    for name in sorted(fit["paired"], key=lambda n: -fit["paired"][n]):
        if name not in fit["offered_as_choice"]:
            return f"{DIFFERENT_STACK}: {name}"
    # The frameworks named are another language's, whatever the language list says.
    for stack, names in fit["ecosystem"].items():
        if len(names) >= 2 and len(names) > len(fit["python_ecosystem"]):
            return f"{DIFFERENT_STACK}: {stack}"
    if len(fit["data_engineering"]) >= 3 and not fit["app_development"] and not fit["genai"]:
        return f"{DIFFERENT_STACK}: Data engineering / BI"
    if len(fit["infrastructure"]) >= 3 and not fit["app_development"] and not fit["genai"]:
        return f"{DIFFERENT_STACK}: DevOps / infrastructure"
    # One enterprise platform named in five or more sentences: the job is on that platform.
    if fit["platform_sentences"] >= 5 and fit["platform_sentences"] > fit["python_sentences"]:
        return f"{DIFFERENT_STACK}: Salesforce / SAP / ERP"
    return ""


SQL_ENGINES = ("postgresql", "sql server", "mysql", "sqlite", "mariadb", "oracle sql")


def canonical_skill_hits(text: str) -> List[str]:
    blob = (text or "").lower()
    hits = []
    for skill, patterns in SKILL_PATTERNS.items():
        if any(re.search(pattern, blob, re.I) for pattern in patterns):
            hits.append(skill)
    # Collapse broad SQL when a more specific SQL engine is identified.
    if any(engine in hits for engine in SQL_ENGINES):
        hits = [x for x in hits if x != "sql"]
    return sorted(set(hits))


# A posting that names only a few skills gives little to go on: "Azure, Git, SQL" is not a 100% match.
MIN_SKILLS_FOR_FULL_SCORE = 5


def score_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Skill match = skills the candidate has / skills the posting asks for.

    Skills found only in the company blurb are ignored, skills found only under nice-to-have count
    half, a language offered only as an alternative to Python is not counted as missing, and a
    posting naming fewer than MIN_SKILLS_FOR_FULL_SCORE skills is scored out of that number.
    """
    title = str(job.get("title") or "")
    description = str(job.get("description") or "")
    required_text = [title]
    optional_text = []
    for sentence, section in _description_sentences(description):
        if section == "company":
            continue
        if section == "preferred" or _OPTIONAL_SKILL_RE.search(sentence):
            optional_text.append(sentence)
        else:
            required_text.append(sentence)
    required_skills = set(canonical_skill_hits("\n".join(required_text)))
    optional_skills = set(canonical_skill_hits("\n".join(optional_text))) - required_skills

    alternative_skills: Set[str] = set()
    if PYTHON_RULES:  # "Python or Java": Java is then not a skill the candidate lacks
        fit = job.get("role_fit") or analyse_role_fit(job)
        alternative_skills = {skill for name in fit["alternative_only"] for skill in OTHER_LANGUAGES[name][1]}
    required_skills -= alternative_skills
    optional_skills -= alternative_skills

    weight = {skill: 1.0 for skill in required_skills}
    weight.update({skill: 0.5 for skill in optional_skills})
    skills = sorted(weight)
    matched = sorted(set(skills) & CANDIDATE_SKILLS)
    missing = sorted(set(skills) - CANDIDATE_SKILLS)
    have = sum(weight[s] for s in matched)
    asked = max(sum(weight.values()), float(MIN_SKILLS_FOR_FULL_SCORE))
    job["job_skills"] = skills
    job["matched_skills"] = matched
    job["missing_skills"] = missing
    job["optional_skills"] = sorted(optional_skills)
    job["skill_match_pct"] = round(have / asked * 100) if skills else 0
    return job


def load_seen_ids() -> set[str]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"seen_job_ids": [], "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
        return set()
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen = set(str(x) for x in payload.get("seen_job_ids", []))
        keys = payload.get("seen_posting_keys", {})
        if isinstance(keys, dict):
            _POSTING_KEY_DATES.update({str(k): str(v) for k, v in keys.items()})
            seen.update(SEEN_KEY_PREFIX + str(k) for k in keys)
        return seen
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        LOG.warning("Could not read job history; starting with empty history: %s", exc)
        return set()


# The history holds LinkedIn job ids and, with this prefix, fingerprints of reported openings
# (see _posting_key), so an opening re-posted under a new id is not reported a second time.
SEEN_KEY_PREFIX = "k:"
_POSTING_KEY_DATES: Dict[str, str] = {}


def save_seen_ids(ids: set[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Keep the state bounded to the latest 25,000 ids and 25,000 fingerprints.
    serialised = sorted(x for x in ids if not x.startswith(SEEN_KEY_PREFIX))[-25000:]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dated = {
        x[len(SEEN_KEY_PREFIX):]: _POSTING_KEY_DATES.get(x[len(SEEN_KEY_PREFIX):], today)
        for x in ids if x.startswith(SEEN_KEY_PREFIX)
    }
    newest = dict(sorted(dated.items(), key=lambda kv: kv[1])[-25000:])
    STATE_FILE.write_text(
        json.dumps(
            {"seen_job_ids": serialised, "seen_posting_keys": newest, "updated_at": datetime.now(timezone.utc).isoformat()},
            indent=2,
        ),
        encoding="utf-8",
    )


def _canonical_linkedin_job_url(raw_url: Any, job_id: Any = None) -> str:
    """Accept LinkedIn job URLs only and normalize them to the canonical job page."""
    raw = str(raw_url or "").strip()
    if raw:
        try:
            parsed = urlparse(raw)
            host = (parsed.hostname or "").lower()
            if host != "linkedin.com" and not host.endswith(".linkedin.com"):
                return ""
            match = re.search(r"/(?:comm/)?jobs/(?:view|collections)/(\d+)", parsed.path, re.I)
            if match:
                return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
        except ValueError:
            return ""
    candidate_id = str(job_id or "").strip()
    if re.fullmatch(r"\d{6,20}", candidate_id):
        return f"https://www.linkedin.com/jobs/view/{candidate_id}/"
    return ""


def clean_job_url(job: Dict[str, Any]) -> str:
    if job.get("portal"):
        # A record from a company career site (company_portals.py): its own https link is the job link.
        url = str(job.get("job_url") or job.get("apply_url") or "").strip()
        return url if url.startswith("https://") else ""
    return _canonical_linkedin_job_url(
        job.get("apply_url") or job.get("job_url") or job.get("url"),
        job.get("job_id") or job.get("id"),
    )


def _parse_linkedin_age(raw: Any) -> float | None:
    """Parse LinkedIn relative labels such as '35 minutes ago' or '2h ago'."""
    if raw in (None, ""):
        return None
    text = str(raw).strip().lower()
    text = re.sub(r"^(?:posted|reposted)\s+", "", text)
    if re.search(r"\b(?:just now|moments ago|right now)\b", text):
        return 0.0
    # "30+ days ago" and "1 month ago" must be read too: a label that is not understood counts as
    # "age unknown", and a posting of unknown age used to be taken for one posted just now.
    match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*\+?\s*"
        r"(seconds?|secs?|s|minutes?|mins?|months?|mos?|m|hours?|hrs?|h|days?|d|weeks?|wks?|w|years?|yrs?|y)\s*(?:ago)?\b",
        text,
        re.I,
    )
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("second", "sec")) or unit == "s":
        return amount / 3600.0
    if unit.startswith(("minute", "min")) or unit == "m":
        return amount / 60.0
    if unit.startswith(("hour", "hr")) or unit == "h":
        return amount
    if unit.startswith("day") or unit == "d":
        return amount * 24.0
    if unit.startswith("mo"):
        return amount * 730.0
    if unit.startswith("y"):
        return amount * 8760.0
    return amount * 168.0


def _parse_linkedin_timestamp(raw: Any) -> datetime | None:
    """Parse timezone-aware ISO timestamps or Unix timestamps; reject date-only strings."""
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        stamp = float(raw)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        if stamp < 1_000_000_000:
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc)
    value = str(raw).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _verified_posting_age(job: Dict[str, Any]) -> float | None:
    """Compute current age from a precise posting timestamp or captured age + capture time."""
    for key in ("posted_iso", "posted_at", "datePosted", "published_at"):
        posted = _parse_linkedin_timestamp(job.get(key))
        if posted is not None:
            age = (datetime.now(timezone.utc) - posted).total_seconds() / 3600.0
            return None if age < -0.25 else max(0.0, age)

    capture = _parse_linkedin_timestamp(job.get("scraped_at") or job.get("captured_at"))
    if capture is None:
        return None
    elapsed = max(0.0, (datetime.now(timezone.utc) - capture).total_seconds() / 3600.0)

    base_age = _as_float(job.get("age_hours"))
    if base_age is None:
        for key in ("posted_time", "posted_label", "freshness_badge", "time_ago"):
            base_age = _parse_linkedin_age(job.get(key))
            if base_age is not None:
                break
    if base_age is None or base_age < 0:
        return None
    return base_age + elapsed


# ---------------------------------------------------------------------------
# Experience requirement parsing
# ---------------------------------------------------------------------------
# A posting can mention years in many places. _experience_mentions() finds every
# statement that looks like an experience requirement, drops the ones that are
# not a requirement on the applicant (company age, contract length, notice
# period, ...), and _resolve_experience() picks the binding (min, max) pair.

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40,
}
_TENS_UNITS_RE = re.compile(r"\b(twenty|thirty|forty)[- ](one|two|three|four|five|six|seven|eight|nine)\b")
_NUMBER_WORD_RE = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b")
_EXP_CHAR_MAP = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-", "−": "-", "~": "-",
    "‘": "'", "’": "'", "`": "'", "´": "'", "“": '"', "”": '"',
    "_": " ", "\t": " ", "\r": " ", " ": " ", "​": " ", " ": " ",
})
_MISSPELT_YEARS_RE = re.compile(r"\b(?:yeras|yaers|yers|yeasr|yrears|yeears)\b")
_MISSPELT_EXPERIENCE_RE = re.compile(
    r"\b(?:expereince|experiance|experince|exprience|experiece|expirience|expierence|experiencce|experence|exeperience)\b"
)


# Degree abbreviations that are only recognisable while still in capitals ("BE" versus "be", "MS" versus "ms").
_DEGREE_CONTEXT = r"(?=\s*(?:/|,|\(|\+|in\b|degree\b|or\b|with\b|plus\b))"
_CAPITAL_BACHELOR_RE = re.compile(rf"\bB\.?\s?(?:E|S)\b\.?{_DEGREE_CONTEXT}")
_CAPITAL_MASTER_RE = re.compile(rf"\bM\.?\s?(?:E|S)\b\.?{_DEGREE_CONTEXT}")


def _normalise_experience_text(text: str) -> str:
    """Lower-case and tidy text so the experience patterns only deal with one spelling of everything."""
    t = _CAPITAL_BACHELOR_RE.sub("bachelor", str(text or ""))
    t = _CAPITAL_MASTER_RE.sub("master", t)
    t = t.lower().translate(_EXP_CHAR_MAP)
    t = re.sub(r"[ ]{2,}", " ", t)
    t = re.sub(r"\((s)\)", r"\1", t)  # year(s) -> years
    t = _MISSPELT_YEARS_RE.sub("years", t)
    t = _MISSPELT_EXPERIENCE_RE.sub("experience", t)
    t = re.sub(r"\by\.?o\.?e\b\.?", "yoe", t)
    t = _TENS_UNITS_RE.sub(lambda m: str(_NUMBER_WORDS[m.group(1)] + _NUMBER_WORDS[m.group(2)]), t)
    t = _NUMBER_WORD_RE.sub(lambda m: str(_NUMBER_WORDS[m.group(1)]), t)
    # "at least a year" -> "at least 1 year", "a couple of years" -> "2 years", "fresher to 3 years" -> "0 to 3 years"
    t = re.sub(
        r"\b((?:at least|atleast|minimum of|minimum|min|more than|over|up to|upto|less than|under) )an? (?=(?:year|yr)\b)",
        r"\g<1>1 ", t,
    )
    t = re.sub(r"\ba couple of (?=(?:years|yrs)\b)", "2 ", t)
    t = re.sub(r"\bfreshers?\s*(?:-|to)\s*(?=\d)", "0 to ", t)
    return t


_YR = r"(?:years?|yrs?|yoe)"
_MO = r"(?:months?|mths?|mos)"
_UNIT = rf"(?:{_YR}|{_MO})"


def _num(name: str) -> str:
    # One or two digits with an optional decimal part, not part of a longer number, date or version.
    return rf"(?<![\d.])(?<!\d[,/:])(?P<{name}>\d{{1,2}}(?:\.\d{{1,2}})?)(?![\d/]|\.\d)"


_MIN_KEYWORD = (
    r"(?<![a-z])(?:minimum|min\b\.?|at\s*-?\s*least|atleast|not\s+less\s+than|no\s+less\s+than|"
    r"(?<!no\s)(?<!not\s)more\s+than|greater\s+than|over|above|upwards\s+of|exceeding|>\s*=?|=\s*>|≥)"
)
_MAX_KEYWORD = (
    r"(?<![a-z])(?:up\s*-?\s*to|maximum(?:\s+of)?|max\b\.?(?:\s+of)?|(?<!not\s)(?<!no\s)less\s+than|fewer\s+than|"
    r"under|below|no\s+more\s+than|not\s+more\s+than|not\s+exceeding|<\s*=?|≤)"
)
_EXP_LABEL = (
    r"(?:(?:total|overall|relevant|work|working|required|minimum|min|professional|industry|prior|"
    r"years?\s+of|yrs?\s+of)\s+)?(?:experience|exp|yoe)\b\.?"
    r"(?:\s+(?:required|needed|range|level|bracket|band|criteria))?(?:\s*\((?:in\s+)?(?:years?|yrs?)\))?"
)
_BOUND_LABEL_TAIL = (
    r"\b\.?\s+(?:(?:work|relevant|total|required)\s+)?(?:experience|exp)\b\.?"
    r"(?:\s*\((?:in\s+)?(?:years?|yrs?)\))?\s*[:=\-]*\s*"
)
_NOT_EXPERIENCE_UNIT = (
    r"(?!\s*-?\s*(?:lpa|lakhs?|lacs?|k\b|%|days?|hrs?|hours?|pm\b|am\b|inr|rs\b|cr\b|members?|people|persons?|projects?))"
)
_END_OF_VALUE = r"(?=\s*(?:$|[)\]|,;.\n]))"
_GAP_STOP = r"(?:degree|bachelor|master|diploma|course|program|contract|bond|old|and|or|but|then|plus|is|are|was|has|have)\b"

# (kind, pattern). Order matters: an earlier pattern claims its text and later ones cannot reuse it.
_EXPERIENCE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # "2-4 years", "2 to 4 yrs", "2 yrs - 4 yrs", "6 months to 2 years", "2-4+ years"
    ("range", re.compile(
        rf"{_num('a')}\s*(?P<aplus>\+)?\s*(?:-?\s*(?P<au>{_UNIT})\b\.?)?\s*(?:-|to|till|through|thru)\s*"
        rf"{_num('b')}\s*(?P<bplus>\+)?\s*-?\s*(?P<bu>{_UNIT})\b"
    )),
    # "between 2 and 4 years"
    ("range", re.compile(
        rf"\bbetween\s+{_num('a')}\s*(?:-?\s*(?P<au>{_UNIT})\b\.?)?\s*(?:and|to|-|&)\s*{_num('b')}\s*-?\s*(?P<bu>{_UNIT})\b"
    )),
    # "Max Experience: 5 Years", "Min Experience: 2"
    ("max", re.compile(rf"\bmax(?:imum)?{_BOUND_LABEL_TAIL}{_num('b')}\s*(?:-?\s*(?P<bu>{_UNIT})\b|{_END_OF_VALUE})")),
    ("min", re.compile(rf"\bmin(?:imum)?{_BOUND_LABEL_TAIL}{_num('a')}\s*(?:\+\s*)?(?:-?\s*(?P<au>{_UNIT})\b|{_END_OF_VALUE})")),
    # "minimum 2 years", "at least 3 yrs", "more than 2 years", ">= 2 yrs"
    ("min", re.compile(rf"{_MIN_KEYWORD}\s*(?:of\s+)?(?:about\s+|around\s+)?{_num('a')}\s*(?:\+\s*)?-?\s*(?P<au>{_UNIT})\b")),
    # "3 years+", "3 years or more", "3 years and above", "2 years minimum"
    ("min", re.compile(
        rf"{_num('a')}\s*-?\s*(?P<au>{_UNIT})\b\.?\s*"
        r"(?:\+|plus\b|or\s+more\b|or\s+above\b|and\s+above\b|&\s*above\b|and\s+more\b|and\s+up\b|minimum\b|min\b|at\s+least\b)"
    )),
    # "3+ years", "3 plus years", "5-plus years"
    ("min", re.compile(rf"{_num('a')}\s*(?:\+|-?\s*plus\b)\s*-?\s*(?P<au>{_UNIT})\b")),
    # "3 or more years"
    ("min", re.compile(rf"{_num('a')}\s+or\s+more\s+(?P<au>{_UNIT})\b")),
    # "up to 2 years", "less than 3 years", "maximum 3 yrs"
    ("max", re.compile(rf"{_MAX_KEYWORD}\s*{_num('b')}\s*-?\s*(?P<bu>{_UNIT})\b")),
    # "2 years or less", "3 years max"
    ("max", re.compile(rf"{_num('b')}\s*-?\s*(?P<bu>{_UNIT})\b\.?\s*(?:or\s+less\b|or\s+below\b|or\s+fewer\b|max\b|maximum\b)")),
    # "Experience Range: 2 - 4", "Yrs of Exp: 4-6", "(Exp 4-6)" - a labelled range with no unit
    ("range", re.compile(
        rf"\b{_EXP_LABEL}\s*(?:[:=\-]+\s*|\s+){_num('a')}\s*(?:-|to)\s*{_num('b')}{_NOT_EXPERIENCE_UNIT}{_END_OF_VALUE}"
    )),
    # "YOE: 3+", "Experience: 5+"
    ("min", re.compile(rf"\b{_EXP_LABEL}\s*[:=\-]+\s*{_num('a')}\s*\+{_NOT_EXPERIENCE_UNIT}{_END_OF_VALUE}")),
    # "experience of 4 years", "Relevant Exp: 3 yrs"
    ("exact", re.compile(
        r"\b(?:experience|exp)\b\.?\s*(?:[:=\-]\s*)?"
        r"(?:(?:of|is|required|needed|around|about|approx\.?|approximately|level|range)\s*(?:[:=\-]\s*)?){0,2}"
        rf"{_num('a')}\s*-?\s*(?P<au>{_UNIT})\b"
    )),
    # "5 years of experience", "4 years of hands-on experience", "3 yrs exp", "5 years' experience"
    ("exact", re.compile(
        rf"{_num('a')}\s*-?\s*(?P<au>{_UNIT})\b(?:'s|s'|')?\.?\s*(?:(?:of|in|on|as|with|working)\s+)?"
        rf"(?:(?!{_GAP_STOP})[a-z][a-z0-9+#./&'\-]*,?\s+){{0,6}}?(?:experience|exp\b|expertise|exposure)"
    )),
    # "3 years of Python", "2 years in Django", "2 years as a developer"
    ("exact", re.compile(rf"{_num('a')}\s*-?\s*(?P<au>{_YR})\b(?:'s|s'|')?\.?\s+(?:of|in|on|as|working)\b")),
]
# Only safe in a job title or a dedicated experience field, where a bare figure is the requirement.
_TITLE_ONLY_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("min", re.compile(rf"\(\s*{_num('a')}\s*\+\s*\)")),  # "Engineer (2+)"
    ("exact", re.compile(rf"{_num('a')}\s*-?\s*(?P<au>{_YR})\b")),  # "Python Developer 3 Yrs"
]

_MAX_MENTIONS = 200
_REQUIRED_WORD_RE = re.compile(r"\b(?:required|must|mandatory|essential)\b")
_EXPERIENCE_WORD_RE = re.compile(r"\b(?:experience|experienced|exp|expertise|exposure|yoe)\b")
_SENTENCE_BREAK_RE = re.compile(r"[;|•]|[.!?](?=\s)")
_SENTENCE_ABBREVIATIONS = ("min", "max", "yrs", "yr", "exp", "approx", "no", "sr", "jr", "etc", "e.g", "i.e", "b.e", "b.tech", "m.tech")
_CLAUSE_BREAK_RE = re.compile(r"[,()\[\]]|\s-\s|\bbut\b|\bthough\b|\bwhereas\b")
_PREFERRED_RE = re.compile(
    r"\b(?:preferred|preferably|prefer|nice\s*-?\s*to\s*-?\s*have|good\s*-?\s*to\s*-?\s*have|great\s+to\s+have|"
    r"is\s+a\s+plus|as\s+a\s+plus|a\s+plus|plus\s+points?|bonus|desirable|desired|ideally|advantage|advantageous|"
    r"an\s+asset|beneficial|stand\s+out|set\s+you\s+apart|would\s+be\s+(?:great|nice|good|ideal)|optional)\b"
)
# Words right after / before a figure that show it is a duration of something else, not experience.
_DURATION_AFTER_RE = re.compile(
    r"^\W{0,3}(?:of\s+)?(?:old|age|contract|bond|agreement|service\s+agreement|commitment|degree|diploma|course|"
    r"program(?:me)?|graduat\w*|pass\s*-?\s*outs?|batch|notice|probation|warranty|roadmap|plan|tenure|lock\s*-?\s*in|"
    r"internship|training|duration|project|assignment|engagement|career\s+(?:break|gap)|gap|break|visa|ago|back|"
    r"in\s+business|in\s+a\s+row|history|legacy|valid\w*|vesting|cliff)\b"
)
_DURATION_BEFORE_RE = re.compile(
    r"(?:contract|bond|agreement|notice|probation|duration|tenure|internship|training\s+period|career\s+(?:break|gap)|"
    r"\bgap\b|\bbreak\b|\bage\b|\baged\b|lock\s*-?\s*in|commitment|for\s+(?:the\s+)?(?:past|last|next)|"
    r"in\s+(?:the\s+)?(?:past|last|next)|over\s+the\s+(?:past|last|next)|\bevery\b|\bper\b|\beach\b|validity|\bvalid\b|"
    r"passport|\bvisa\b|warranty|vest\w*|cliff|\besops?\b|extend\w*|renew\w*)[^.\n]{0,25}$"
)
# Signs that the text is talking about the applicant ...
_CANDIDATE_VOICE_RE = re.compile(
    r"\b(?:you|your|you'll|you've|candidates?|applicants?|must|should|shall|require[sd]?|requirements?|mandatory|"
    r"minimum|min|at\s*least|atleast|qualifications?|looking\s+for|seeking|seek|needs?|needed|expects?|wants?|hiring|"
    r"ideal|ideally|proven|hands\s*-?\s*on|relevant|preferred|preferably|eligib\w+|desired|exp|yoe|"
    r"who\s+(?:has|have|is|are|can)|he|she|incumbent|someone|individual|person)\b"
)
# ... or about the employer ("we have 20 years of experience", "a team whose members have 12+ years").
_COMPANY_SUBJECT_RE = re.compile(
    r"\b(?:we|we're|we've|our|ours|about\s+us|company|firm|organi[sz]ation|agency|group|teams?|members|founders|"
    r"leadership|mentors|colleagues|peers)\b"
)
_COMPANY_VERB_BEFORE_RE = re.compile(
    r"\b(?:has|brings?|boasts?|carr(?:y|ies)|celebrat\w+|complet\w+|spanning|spans?)\s+(?:of\s+)?(?:a\s+)?"
    r"(?:combined\s+|rich\s+|collective\s+)?$"
)
_COMPANY_HISTORY_RE = re.compile(
    r"\b(?:in\s+business|track\s+record|legacy|heritage|history|since\s+(?:19|20)\d\d|founded|established|inception|"
    r"decades?|market\s+leader|global\s+leader|industry\s+leader|leading\s+provider|pioneer\w*)\b"
)
# "With over 25 years of experience in IT services, Acme helps ..." - the figure opens a sentence about someone else.
_COMPANY_OPENING_RE = re.compile(r"(?:^|:)\W*(?:with|having|backed\s+by|leveraging|drawing\s+on|built\s+on)\s*$")
_HIGHER_DEGREE_RE = re.compile(
    r"\b(?:master'?s?|m\.?\s?tech|m\.e\b|m\.?s\b(?!\s*(?:office|sql|excel|azure|dynamics|word|teams|access|project))|"
    r"m\.?sc|ph\.?\s?d|doctorate|doctoral|post\s*-?\s*graduat\w*)"
)
_BACHELOR_DEGREE_RE = re.compile(r"\b(?:bachelor'?s?|b\.?\s?tech|b\.e\b|b\.?s\b|b\.?sc|bca|undergraduate)")
_OPTION_LINE_RE = re.compile(r"^\W*option\s*\d+\b")
# Section headings inside a description. A heading is a short line made up of the heading words only.
_SECTION_HEADINGS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("main", re.compile(
        r"(?:about\s+(?:the\s+|this\s+)?(?:role|job|position|opportunity|you)|(?:desired|ideal|preferred)\s+candidate)\b"
    )),
    ("preferred", re.compile(
        r"(?:preferred|desired|bonus|(?:additional|optional)\s+(?:qualifications?|skills?|requirements?|experience)|"
        r"nice\s*-?\s*to\s*-?\s*haves?|good\s*-?\s*to\s*-?\s*haves?|"
        r"great\s+to\s+haves?|plus\s+points?|pluses|brownie\s+points?|added\s+advantages?|extra\s+credit|even\s+better|"
        r"ways\s+to\s+stand\s+out|what\s+(?:will|would)\s+(?:make|set)\s+you|what\s+sets\s+you\s+apart|"
        r"what\s+would\s+be\s+(?:great|nice))\b"
    )),
    ("company", re.compile(
        r"(?:about\s+(?:us|the\s+company|company|the\s+team|our\s+team|\w+(?:\s+\w+){0,3})|who\s+we\s+are|"
        r"company\s+(?:overview|profile|description)|our\s+(?:story|company|mission|culture|values)|why\s+(?:join\s+us|us|\w+)|"
        r"benefits|perks|what\s+we\s+offer|we\s+offer|life\s+at\s+\w+|equal\s+opportunity|disclaimer)\b"
    )),
    ("main", re.compile(
        r"(?:(?:basic|minimum|required|key|must\s*-?\s*have|mandatory|essential|technical|primary|core)\s+)?"
        r"(?:qualifications?|requirements?|skills?|experience|responsibilities|duties|eligibility|criteria|education|"
        r"what\s+you(?:'ll|\s+will)?\s+(?:do|bring|need|be\s+doing)|what\s+we\s+need|who\s+you\s+are|you\s+have|"
        r"you(?:'ll|\s+will)?\s+(?:bring|need)|what\s+we\s+expect|we\s+expect|"
        r"your\s+(?:background|experience|qualifications|expertise|tasks|mission|impact)|"
        r"you\s+should\s+have|must\s*-?\s*haves?|what\s+we(?:'re|\s+are)\s+looking\s+for|"
        r"your\s+(?:role|profile|skills|responsibilities)|role|job\s+description|job\s+summary|overview|position|profile|"
        r"description)\b"
    )),
]


def _looks_like_heading(original_line: str) -> bool:
    """A short line with no figure that reads as a heading: ends with ':' or '?', is in capitals, or is Title Case."""
    stripped = original_line.strip(" \t*#-•>")
    if not 0 < len(stripped) <= 60 or re.search(r"\d", stripped):
        return False
    ellipsis = stripped.endswith(("...", "…"))
    if stripped.endswith((".", ",", ";")) and not ellipsis:
        return False
    words = stripped.rstrip(":?!….").split()
    if not words or len(words) > 8:
        return False
    if stripped.endswith((":", "?")) or ellipsis:
        return True
    return len(words) >= 2 and sum(1 for w in words if w[:1].isupper()) / len(words) >= 0.6


def _section_of_each_line(lines: Sequence[str], original_lines: Sequence[str]) -> List[str]:
    """Label every description line as 'main', 'preferred' or 'company' from the heading above it.

    A 'company' or 'preferred' section ends at the next heading, including one this code does not
    recognise, so an unusual heading ("You're our person if...") cannot hide the requirements below it.
    """
    current = "main"
    labels = []
    for line, original in zip(lines, original_lines):
        stripped = line.strip(" \t:*#-•>")
        recognised = False
        if 0 < len(stripped) <= 70 and not re.search(r"\d", stripped):
            for label, pattern in _SECTION_HEADINGS:
                match = pattern.match(stripped)
                # A heading has at most a few trailing words ("Preferred qualifications", "About Acme Corp").
                if match and len(stripped) - match.end() <= 25:
                    current, recognised = label, True
                    break
        if not recognised and current != "main" and _looks_like_heading(original):
            current = "main"
        labels.append(current)
    return labels


def _span_between_breaks(text: str, start: int, end: int, pattern: "re.Pattern[str]", skip_abbreviations: bool = False) -> Tuple[int, int]:
    """Return the (begin, finish) of the stretch of text around [start, end) bounded by pattern matches."""
    begin, finish = 0, len(text)
    for match in pattern.finditer(text):
        if skip_abbreviations and match.group(0) == ".":
            before = text[:match.start()].rsplit(None, 1)[-1] if text[:match.start()].strip() else ""
            if before.strip("(") in _SENTENCE_ABBREVIATIONS:
                continue
        if match.end() <= start:
            begin = match.end()
        elif match.start() >= end:
            finish = match.start()
            break
    return begin, finish


def _to_years(value: str, unit: str) -> float:
    return round(float(value) / 12.0, 2) if unit.startswith("m") else float(value)


def _is_employer_statement(section: str, sentence: str, before: str, clause_before: str, opens_line: bool) -> bool:
    """True when a figure describes the employer or its team rather than what the applicant must have.

    opens_line: the figure is the first thing on its line ("10+ years of experience in ..."), which is
    how a requirement bullet is written, so a surrounding company section does not rule it out.
    """
    governed_by_applicant = _CANDIDATE_VOICE_RE.search(clause_before) or "experience" in clause_before
    if not governed_by_applicant and (_COMPANY_SUBJECT_RE.search(clause_before) or _COMPANY_VERB_BEFORE_RE.search(before)):
        return True
    if _CANDIDATE_VOICE_RE.search(sentence):
        return False
    return bool(
        (section == "company" and not opens_line)
        or _COMPANY_HISTORY_RE.search(sentence)
        or (_COMPANY_OPENING_RE.search(before) and "," in sentence[len(before):])
    )


def _experience_mentions(text: str, is_title: bool = False) -> List[Dict[str, Any]]:
    """Find experience requirements in text.

    Each mention has 'lo' and 'hi' (years, or None for no bound), 'preferred' (nice-to-have only),
    'degree' ('bachelor', 'higher' or '' - the degree the figure is tied to) and 'option'
    (it sits on an "Option 1 / Option 2" line).
    """
    norm = _normalise_experience_text(text)
    if not norm.strip():
        return []
    patterns = _EXPERIENCE_PATTERNS + (_TITLE_ONLY_PATTERNS if is_title else [])
    raw: List[Tuple[int, int, float | None, float | None, bool]] = []
    for kind, pattern in patterns:
        for match in pattern.finditer(norm):
            if len(raw) >= _MAX_MENTIONS:
                break  # far more figures than any real posting has; keeps odd input from taking minutes
            if any(match.start() < e and s < match.end() for s, e, *_ in raw):
                continue
            groups = match.groupdict()
            unit_b = groups.get("bu")
            unit_a = groups.get("au") or unit_b or "years"
            months = unit_a.startswith("m") or bool(unit_b and unit_b.startswith("m"))
            lo = _to_years(groups["a"], unit_a) if groups.get("a") is not None else None
            hi = _to_years(groups["b"], unit_b or unit_a) if groups.get("b") is not None else None
            if kind == "range":
                if lo is None or hi is None or lo > hi:
                    continue
                if groups.get("bplus"):
                    hi = None  # "2-4+ years" has no upper limit
            if (lo is not None and lo > 40) or (hi is not None and hi > 50):
                continue
            raw.append((match.start(), match.end(), lo, hi, months))
    raw.sort()

    lines = norm.split("\n")
    original_lines = str(text).replace("\r", " ").split("\n")
    if is_title or len(original_lines) != len(lines):
        sections = ["main"] * len(lines)
    else:
        sections = _section_of_each_line(lines, original_lines)
    line_starts, offset = [], 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1

    mentions: List[Dict[str, Any]] = []
    previous: Tuple[int, int, int, bool] | None = None  # line, sentence begin, mention end, degree taken from text after it
    for index, (start, end, lo, hi, months) in enumerate(raw):
        line_no = max(i for i, s in enumerate(line_starts) if s <= start)
        line = lines[line_no]
        ls, le = start - line_starts[line_no], min(end - line_starts[line_no], len(line))
        s_begin, s_end = _span_between_breaks(line, ls, le, _SENTENCE_BREAK_RE, skip_abbreviations=True)
        sentence = line[s_begin:s_end]
        ss, se = ls - s_begin, le - s_begin
        c_begin, c_end = _span_between_breaks(sentence, ss, se, _CLAUSE_BREAK_RE)
        clause_before, clause_after = sentence[c_begin:ss], sentence[se:c_end]
        mention_text = norm[start:end]

        # The degree this figure is tied to: look between the previous figure and this one, then after it.
        same_sentence = previous is not None and previous[0] == line_no and previous[1] == s_begin
        gap_before = sentence[(previous[2] if same_sentence else 0):ss]
        next_start = len(sentence)
        if index + 1 < len(raw) and raw[index + 1][0] < line_starts[line_no] + s_end:
            next_start = raw[index + 1][0] - line_starts[line_no] - s_begin
        gap_after = sentence[se:next_start]
        degree, used_after = "", False
        for gap, is_after in ((gap_before, False), (gap_after, True)):
            if same_sentence and previous[3] and not is_after:
                continue  # the previous figure already claimed the degree words between the two
            if _BACHELOR_DEGREE_RE.search(gap):
                degree, used_after = "bachelor", is_after
            elif _HIGHER_DEGREE_RE.search(gap):
                degree, used_after = "higher", is_after
            if degree:
                break
        previous = (line_no, s_begin, se, used_after)

        # Months are usually a notice period, probation or contract length unless experience is mentioned.
        has_experience_word = bool(_EXPERIENCE_WORD_RE.search(sentence))
        if months and not is_title and not has_experience_word:
            continue
        # "over 4 years" / "above 3 years" is only a requirement when experience is mentioned.
        if not is_title and re.match(r"(?:over|above)\b", mention_text) and not has_experience_word:
            continue
        if _DURATION_AFTER_RE.search(clause_after) or _DURATION_BEFORE_RE.search(clause_before):
            continue
        opens_line = not line[:ls].strip(" \t*#-•>·")
        if not is_title and _is_employer_statement(sections[line_no], sentence, sentence[:ss], clause_before, opens_line):
            continue
        # Under a "preferred" heading a figure is preferred, unless its own sentence says it is required
        # ("Good to have skills: NA / Minimum 7.5 years of experience is required").
        in_preferred_section = sections[line_no] == "preferred" and not _REQUIRED_WORD_RE.search(sentence)
        preferred = not is_title and (in_preferred_section or bool(_PREFERRED_RE.search(sentence[c_begin:c_end])))
        mentions.append({
            "lo": lo, "hi": hi, "preferred": preferred, "degree": degree,
            "option": bool(_OPTION_LINE_RE.match(line)), "sentence": (line_no, s_begin), "text": mention_text,
        })
    return mentions


def _resolve_experience(mentions: Sequence[Dict[str, Any]]) -> Tuple[float | None, float | None]:
    """Pick the binding (min, max) years from a list of mentions.

    Required statements beat preferred ones. Where a posting gives alternatives by degree
    ("Bachelor's with 4 years or Master's with 2 years", "Option 1 / Option 2"), the Bachelor's
    path is the one that applies to this candidate. After that the applicant has to meet every
    remaining statement, so the one with the highest minimum decides.
    """
    pool = [m for m in mentions if not m["preferred"]] or list(mentions)
    if not pool:
        return None, None

    options = [m for m in pool if m["option"]]
    if len(options) > 1:
        bachelor_options = [m for m in options if m["degree"] == "bachelor"]
        with_min = [m for m in options if m["lo"] is not None]
        chosen = bachelor_options[0] if bachelor_options else (min(with_min, key=lambda m: m["lo"]) if with_min else options[0])
        pool = [m for m in pool if not m["option"] or m is chosen]
    # "Bachelor's with 5 years, Master's with 2 years, or 8 years without a degree" in one sentence:
    # of those alternatives only the Bachelor's figure applies.
    for sentence in {m["sentence"] for m in pool if m["degree"] == "bachelor"}:
        if any(m["sentence"] == sentence and m["degree"] == "higher" for m in pool):
            pool = [m for m in pool if m["sentence"] != sentence or m["degree"] == "bachelor"]
    if any(m["degree"] != "higher" for m in pool):
        pool = [m for m in pool if m["degree"] != "higher"]

    with_min = [m for m in pool if m["lo"] is not None]
    if not with_min:
        return None, min(m["hi"] for m in pool)
    binding = max(with_min, key=lambda m: (m["lo"], m["hi"] is not None))
    lo, hi = binding["lo"], binding["hi"]
    if hi is None:
        caps = [m["hi"] for m in pool if m["hi"] is not None and m["hi"] >= lo]
        hi = min(caps) if caps else None
    return lo, hi


def parse_experience_requirement(job: Dict[str, Any]) -> Tuple[float | None, float | None]:
    """Return the (min, max) years of experience a posting asks for; None where it states no bound.

    A figure in the job title is the binding one. Otherwise the description decides, then any
    separate experience field supplied with the record.
    """
    for key, is_title in (("title", True), ("description", False), ("experience", True), ("experience_requirement", True)):
        lo, hi = _resolve_experience(_experience_mentions(str(job.get(key) or ""), is_title=is_title))
        if lo is not None or hi is not None:
            return lo, hi
    return None, None


def _extract_experience_range(job: Dict[str, Any], overwrite: bool = False) -> None:
    """Populate minimum/maximum experience from title and description."""
    if not overwrite and (job.get("min_experience_years") is not None or job.get("max_experience_years") is not None):
        return
    lo, hi = parse_experience_requirement(job)
    if lo is None and hi is None:
        return
    job["min_experience_years"] = lo
    job["max_experience_years"] = hi
    if lo is not None and hi is not None:
        job["experience_requirement"] = f"{lo:g}-{hi:g} yrs"
    elif lo is not None:
        job["experience_requirement"] = f"{lo:g}+ yrs"
    else:
        job["experience_requirement"] = f"Up to {hi:g} yrs"


# ---------------------------------------------------------------------------
# LinkedIn Live Guest-Endpoint Scraper & Async JD Enrichment
# ---------------------------------------------------------------------------
# Things the reader of the report should know about this run (a worker that failed, a search cut short).
RUN_NOTES: List[str] = []


def _search_budget_seconds() -> float:
    """Longest the search phase may run, from SEARCH_BUDGET_SECONDS (default 20 minutes)."""
    try:
        return max(60.0, float(os.environ.get("SEARCH_BUDGET_SECONDS", "") or 1200.0))
    except ValueError:
        return 1200.0


# Search pages asked for and search pages LinkedIn refused, since the last reset. The Hyderabad
# watcher reads this to notice that its runner's address is being rate-limited.
SEARCH_STATS: Dict[str, int] = {"pages": 0, "failed": 0}


_JOB_URN_RE = re.compile(r"jobPosting:(\d{6,20})")


def _card_job_id(card: Any, href: str) -> str:
    """LinkedIn's id for one search-result card.

    The card names it outright (data-entity-urn="urn:li:jobPosting:4471386121"). Failing that, it is
    the number that ends the link, ".../python-developer-at-acme-4471386121". Taking the first long
    number in the link was wrong for a title that carries one ("Engineer - Req 31001234").
    """
    holders = [card, *card.find_all(attrs={"data-entity-urn": True})] if hasattr(card, "find_all") else []
    for holder in holders:
        urn = _JOB_URN_RE.search(str(holder.get("data-entity-urn") or ""))
        if urn:
            return urn.group(1)
    path = str(href or "").split("?")[0].split("#")[0].rstrip("/")
    last = re.search(r"(\d{6,20})$", path)
    if last:
        return last.group(1)
    numbers = re.findall(r"\d{8,12}", path)
    return numbers[-1] if numbers else path.split("-")[-1].split("/")[-1]


def scrape_linkedin_guest_jobs(
    hours: float,
    target_count: int,
    delay: float = 1.0,
    proxy: str | None = None,
    keywords_list: Sequence[str] | None = None,
    locations_list: Sequence[str] | None = None,
    empty_pages_to_stop: int = 2,
    budget_seconds: float | None = None,
) -> List[Dict[str, Any]]:
    """
    Direct live guest-endpoint scraper for LinkedIn jobs.
    Queries https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
    with dynamic f_TPR, rotating browser headers, backoff, and optional proxy.

    empty_pages_to_stop: a search ends after this many result pages in a row came back empty.
    budget_seconds: longest the whole search may run (default: SEARCH_BUDGET_SECONDS).
    """
    if requests is None or BeautifulSoup is None:
        LOG.error("requests or beautifulsoup4 not installed. Cannot run live LinkedIn scraper.")
        return []

    active_proxy = _resolve_proxy(proxy)
    session = requests.Session()
    if active_proxy:
        session.proxies.update({"http": active_proxy, "https": active_proxy})
        LOG.info("LinkedIn Guest Scraper active with proxy: %s", active_proxy)

    backoff = LinkedInBackoff()
    terms = list(keywords_list) if keywords_list else SEARCH_TERMS
    locs = list(locations_list) if locations_list else SEARCH_LOCATIONS

    # Calculate dynamic f_TPR
    tpr_seconds = max(3600, int(float(hours) * 3600))
    f_tpr = f"r{tpr_seconds}"

    # Allow scraping as many fresh jobs as possible (up to 100 per term)
    per_term_cap = target_count if target_count > 0 else 100
    collected: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    LOG.info(
        "Starting LinkedIn live guest scraping: %d keywords, %d locations, window=%.1fh (f_TPR=%s)",
        len(terms), len(locs), hours, f_tpr,
    )

    # Two passes per search: Easy Apply jobs first (so they can be labelled), then everything else.
    # Both passes read every result up to the cap. The second pass used to stop after 20 new jobs,
    # which silently dropped company-site ("Normal Apply") postings in a busy hour.
    apply_modes = [
        {"name": "Easy Apply", "is_easy": True, "param": "&f_AL=true", "ratio": 1.0},
        {"name": "Normal Apply", "is_easy": False, "param": "", "ratio": 1.0},
    ]

    # A search that LinkedIn keeps rate-limiting must not eat the whole run: stop after the budget
    # and go on with the cards collected so far.
    search_deadline = time.monotonic() + (budget_seconds or _search_budget_seconds())
    empty_pages_to_stop = max(1, int(empty_pages_to_stop))

    for term in terms:
        for loc in locs:
            if target_count > 0 and len(collected) >= target_count:
                break
            if time.monotonic() > search_deadline:
                break

            is_remote_loc = loc.strip().lower() == "remote"
            query_loc = "India" if is_remote_loc else loc
            remote_param = "&f_WT=2" if is_remote_loc else ""

            encoded_kw = quote_plus(term)
            encoded_loc = quote_plus(query_loc)

            for mode in apply_modes:
                mode_cap = max(2, int(per_term_cap * mode["ratio"]))
                mode_jobs = 0
                start = 0
                consecutive_empty = 0

                while mode_jobs < mode_cap and consecutive_empty < empty_pages_to_stop and time.monotonic() <= search_deadline:
                    url = (
                        f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                        f"?keywords={encoded_kw}&location={encoded_loc}&f_TPR={f_tpr}&f_JT=F{mode['param']}{remote_param}&start={start}"
                    )

                    resp = None
                    for attempt in range(backoff.max_retries):
                        try:
                            r = session.get(url, headers=_get_browser_headers(), timeout=15)
                            if r.status_code == 200:
                                backoff.reset()
                                resp = r
                                break
                            elif r.status_code in (429, 403):
                                wait = backoff.wait_seconds(attempt, r.headers.get("Retry-After"))
                                backoff.record_failure()
                                LOG.warning("Rate limit (HTTP %d). Waiting %.1fs before retry...", r.status_code, wait)
                                time.sleep(wait)
                                if backoff.exhausted:
                                    LOG.error("Backoff exhausted on LinkedIn guest API. Skipping %s (%s).", term, mode["name"])
                                    break
                            else:
                                LOG.warning("LinkedIn returned HTTP %d for %s (%s)", r.status_code, term, mode["name"])
                                time.sleep(2.0)
                        except Exception as err:
                            LOG.warning("Network error querying LinkedIn: %s", err)
                            time.sleep(backoff.wait_seconds(attempt))

                    SEARCH_STATS["pages"] += 1
                    if resp is None:
                        SEARCH_STATS["failed"] += 1
                    if not resp or not resp.text.strip():
                        consecutive_empty += 1
                        start += 10
                        continue

                    soup = BeautifulSoup(resp.text, "html.parser")
                    cards = soup.find_all("li")
                    if not cards:
                        consecutive_empty += 1
                        start += 10
                        continue

                    consecutive_empty = 0
                    for card in cards:
                        try:
                            t_el = card.find("h3", class_="base-search-card__title") or card.find("h3")
                            co_el = card.find("h4", class_="base-search-card__subtitle") or card.find("h4")
                            lo_el = card.find("span", class_="job-search-card__location")
                            ti_el = card.find("time")
                            li_el = card.find("a", class_="base-card__full-link") or card.find("a", href=True)

                            if not (t_el and li_el):
                                continue

                            title = t_el.get_text(" ", strip=True)
                            company = co_el.get_text(" ", strip=True) if co_el else "Unknown Company"
                            job_loc = lo_el.get_text(" ", strip=True) if lo_el else query_loc

                            job_id = _card_job_id(card, li_el.get("href", ""))

                            if not job_id or job_id in seen_ids:
                                continue
                            seen_ids.add(job_id)

                            canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}/"

                            posted_str = ""
                            posted_iso = ""
                            if ti_el:
                                posted_str = ti_el.get_text(strip=True)
                                posted_iso = ti_el.get("datetime", "")

                            age = _parse_linkedin_age(posted_str)
                            if age is None and posted_iso:
                                parsed_dt = _parse_linkedin_timestamp(posted_iso)
                                if parsed_dt:
                                    age = max(0.0, (datetime.now(timezone.utc) - parsed_dt).total_seconds() / 3600.0)

                            if age is not None and age >= float(hours):
                                continue

                            age_val = round(age, 3) if age is not None else 0.0
                            badge = f"<1h ago ({max(1, round(age_val * 60))}m)" if age_val < 1 else f"{age_val:.1f}h ago"

                            workplace = "Remote" if is_remote_loc else "Not specified"

                            rec = {
                                "job_id": job_id,
                                "source": "LinkedIn",
                                "job_url": canonical_url,
                                "apply_url": canonical_url,
                                "title": title,
                                "company": company,
                                "location": job_loc,
                                "description": "",
                                "workplace_type": workplace,
                                "is_easy_apply": mode["is_easy"],
                                "apply_type": "⚡ Easy Apply" if mode["is_easy"] else "Normal Apply",
                                "posted_time": posted_str,
                                "posted_iso": posted_iso,
                                "scraped_at": datetime.now(timezone.utc).isoformat(),
                                "age_hours": age_val,
                                "freshness_badge": badge,
                            }
                            _extract_experience_range(rec)
                            collected.append(rec)
                            mode_jobs += 1

                            if target_count > 0 and len(collected) >= target_count:
                                break
                        except Exception as card_err:
                            LOG.debug("Error parsing card: %s", card_err)
                            continue

                    start += max(len(cards), 10)
                    time.sleep(delay + random.uniform(0.4, 0.9))

    if time.monotonic() > search_deadline:
        note = f"Search of {', '.join(locs)} stopped at its time limit; some listings may be missing this hour."
        LOG.warning(note)
        RUN_NOTES.append(note)
    LOG.info("Guest scraper harvested %d initial LinkedIn job cards.", len(collected))
    return collected


async def _async_fetch_jd(
    session: "aiohttp.ClientSession",
    job: Dict[str, Any],
    sem: asyncio.Semaphore,
    proxy: str | None = None,
    hours: float = 24.0,
) -> Dict[str, Any]:
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['job_id']}"
    headers = _get_browser_headers()
    async with sem:
        for _ in range(2):
            try:
                async with session.get(url, headers=headers, proxy=proxy, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 200:
                        text = await r.text()
                        _parse_jd_html_into_job(job, text, hours)
                        return job
                    elif r.status in (429, 403):
                        await asyncio.sleep(2.0 + random.uniform(1.0, 2.0))
            except Exception as e:
                LOG.debug("Async JD error for %s: %s", job["job_id"], e)
        return job


_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "section", "blockquote", "pre")


def _html_to_text(element: Any) -> str:
    """Text of a description with a line break only where the page has one (paragraph, list item, <br>).

    Inline formatting stays inside its line. Breaking at every tag turned
    "<li>C#/.NET <em>(Preferred)</em></li>" into two lines and "<strong>Experience:</strong> 2-4 years"
    into two lines, and both the experience rule and the role-fit rule read a posting line by line.
    """
    for br in element.find_all("br"):
        br.replace_with("\n")
    for block in element.find_all(_BLOCK_TAGS):
        block.insert_before("\n")
        block.insert_after("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in element.get_text().split("\n")]
    return "\n".join(line for line in lines if line)


def _parse_jd_html_into_job(job: Dict[str, Any], html_text: str, hours: float = 24.0) -> None:
    if not html_text:
        return
    soup = BeautifulSoup(html_text, "html.parser")

    # Company name / URL
    comp_link = soup.find("a", class_=lambda c: c and "topcard__org-name-link" in c)
    if comp_link:
        if comp_link.get("href"):
            job["company_url"] = comp_link["href"].split("?")[0]
        if not job.get("company") or job["company"] == "Unknown Company":
            job["company"] = comp_link.get_text(strip=True)

    # Criteria items
    for item in soup.find_all("li", class_="description__job-criteria-item"):
        h = item.find("h3")
        v = item.find("span")
        if not (h and v):
            continue
        h_txt = h.get_text().strip().lower()
        v_txt = v.get_text().strip()
        if "seniority" in h_txt:
            job["seniority_level"] = v_txt
        elif "employment" in h_txt:
            job["employment_type"] = v_txt
        elif "workplace" in h_txt:
            job["workplace_type"] = v_txt
        elif "industr" in h_txt:
            job["industries"] = v_txt

    # Full description
    desc_el = (
        soup.find("div", class_="show-more-less-html__markup")
        or soup.find("div", class_="description__text")
        or soup.find("section", class_="description")
    )
    if desc_el:
        job["description"] = _html_to_text(desc_el)

    # Timestamp verification from detail page
    t_ago = soup.find(class_=lambda c: c and "posted-time-ago" in c)
    time_tag = soup.find("time")
    detail_posted = (t_ago.get_text(strip=True) if t_ago else "") or (time_tag.get_text(strip=True) if time_tag else "")
    detail_iso = time_tag.get("datetime", "") if time_tag else ""

    for script in soup.find_all("script", type=lambda t: t and "ld+json" in t.lower()):
        try:
            data = json.loads(script.get_text(strip=True))
            if isinstance(data, dict) and data.get("datePosted"):
                detail_iso = data["datePosted"]
        except Exception:
            pass

    if detail_posted or detail_iso:
        age = _parse_linkedin_age(detail_posted)
        if age is None and detail_iso:
            parsed_dt = _parse_linkedin_timestamp(detail_iso)
            if parsed_dt:
                age = max(0.0, (datetime.now(timezone.utc) - parsed_dt).total_seconds() / 3600.0)
        if age is not None:
            job["age_hours"] = round(age, 3)
            job["posted_time"] = detail_posted or job.get("posted_time", "")
            job["posted_iso"] = detail_iso or job.get("posted_iso", "")
            if age < 1:
                job["freshness_badge"] = f"<1h ago ({max(1, round(age * 60))}m)"
            else:
                job["freshness_badge"] = f"{age:.1f}h ago"

    _extract_experience_range(job, overwrite=True)


def enrich_linkedin_jds(
    jobs: List[Dict[str, Any]],
    proxy: str | None = None,
    max_concurrency: int = 5,
    hours: float = 24.0,
) -> List[Dict[str, Any]]:
    """Enriches job records with complete job descriptions and metadata."""
    to_enrich = [j for j in jobs if not j.get("description")]
    if not to_enrich:
        return jobs

    LOG.info("Enriching %d jobs with full job descriptions and criteria...", len(to_enrich))
    active_proxy = _resolve_proxy(proxy)

    if HAS_AIOHTTP and aiohttp:
        async def _run():
            sem = asyncio.Semaphore(max_concurrency)
            connector = aiohttp.TCPConnector(limit=10, ssl=False)
            async with aiohttp.ClientSession(connector=connector) as sess:
                tasks = [_async_fetch_jd(sess, j, sem, active_proxy, hours) for j in to_enrich]
                await asyncio.gather(*tasks)

        try:
            asyncio.run(_run())
        except Exception as exc:
            LOG.warning("Async JD fetch encountered exception: %s. Falling back to sync.", exc)

    _fetch_missing_descriptions(to_enrich, active_proxy, hours)
    fetched = sum(1 for j in to_enrich if j.get("description"))
    LOG.info("Job descriptions fetched for %d of %d jobs.", fetched, len(to_enrich))
    return jobs


def _fetch_missing_descriptions(
    jobs: Sequence[Dict[str, Any]],
    proxy: str | None,
    hours: float,
    budget_seconds: float = 480.0,
) -> None:
    """Fetch descriptions the fast concurrent pass did not get, one at a time and slowing down when rate-limited.

    Without its description a job cannot be checked for experience or scored, so this is worth a few
    minutes. Only jobs with an eligible title are retried, the ones in an eligible location first.
    """
    missing = [j for j in jobs if not j.get("description") and title_is_eligible(j)]
    if not missing:
        return
    missing.sort(key=lambda j: not location_is_eligible(j))
    LOG.info("Retrying %d job descriptions that were not returned...", len(missing))
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    started = time.monotonic()
    deadline = started + budget_seconds
    pause = 1.5
    for _ in range(3):
        for job in missing:
            if time.monotonic() > deadline:
                break
            url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job['job_id']}"
            try:
                r = session.get(url, headers=_get_browser_headers(), timeout=15)
                if r.status_code == 200:
                    age_before = job.get("age_hours")
                    _parse_jd_html_into_job(job, r.text, hours)
                    # The page reports the age as of now. Take off the time this retry pass has been
                    # running, so a job is not pushed outside the freshness window only because its
                    # description arrived late.
                    age_now = _as_float(job.get("age_hours"))
                    if age_now is not None and job.get("age_hours") != age_before:
                        job["age_hours"] = round(max(0.0, age_now - (time.monotonic() - started) / 3600.0), 3)
                    pause = max(1.5, pause * 0.8)
                elif r.status_code in (403, 429, 999):
                    pause = min(8.0, pause * 1.5)
                    time.sleep(pause * 3)
            except Exception as err:
                LOG.debug("JD fetch error for %s: %s", job["job_id"], err)
            time.sleep(pause + random.uniform(0.2, 0.8))
        missing = [j for j in missing if not j.get("description")]
        if not missing or time.monotonic() > deadline:
            break
    if missing:
        LOG.warning("%d job descriptions could not be fetched; those jobs are reported as not checked.", len(missing))


# ---------------------------------------------------------------------------
# Core Collection Pipeline (Supports Live Guest Scraping & Local JSON)
# ---------------------------------------------------------------------------
def collect_jobs(
    hours: float,
    target_count: int,
    delay: float,
    source: str = "auto",
    proxy: str | None = None,
    keywords: str | None = None,
    location: str | None = None,
    no_jd: bool = False,
    skip_ids: Set[str] | None = None,
    search_options: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Collect LinkedIn job records via live guest scraping and/or local JSON intake.

    source choices:
      - 'auto'  : Loads records from local JSON if present; also runs live guest scraper
                  if file is empty or missing.
      - 'guest' : Strictly runs live guest-endpoint scraper.
      - 'file'  : Only reads from local JSON file (LINKEDIN_JOBS_JSON).

    skip_ids: job ids the caller has already judged; they are dropped before any description is fetched.
    search_options: extra arguments for scrape_linkedin_guest_jobs (empty_pages_to_stop, budget_seconds).
    """
    accepted: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # 1. Local JSON Intake
    input_path = Path(os.environ.get("LINKEDIN_JOBS_JSON", str(DATA_DIR / "linkedin_jobs.json")))
    file_records: List[Dict[str, Any]] = []

    if source in ("auto", "file") and input_path.exists():
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            raw_list = payload.get("jobs", []) if isinstance(payload, dict) else payload
            if isinstance(raw_list, list):
                file_records = [r for r in raw_list if isinstance(r, dict)]
                LOG.info("Loaded %d records from local JSON file %s", len(file_records), input_path)
        except Exception as exc:
            LOG.warning("Could not read local JSON at %s: %s", input_path, exc)

    # 2. Live LinkedIn Guest Scraping
    guest_records: List[Dict[str, Any]] = []
    should_scrape = (source == "guest") or (source == "auto" and len(file_records) == 0)

    if should_scrape:
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None
        loc_list = _split_places(location, []) or None
        guest_records = scrape_linkedin_guest_jobs(
            hours=hours,
            target_count=target_count,
            delay=delay,
            proxy=proxy,
            keywords_list=kw_list,
            locations_list=loc_list,
            **(search_options or {}),
        )

    all_raw = file_records + guest_records

    # 3. Normalization and Verified Posting Age Check
    for raw_item in all_raw:
        job = dict(raw_item)
        job_id = str(job.get("job_id") or job.get("id") or "").strip()
        raw_url = job.get("job_url") or job.get("apply_url") or job.get("url") or ""
        canonical_url = _canonical_linkedin_job_url(raw_url, job_id)
        if not canonical_url:
            LOG.debug("Skipping record without a valid LinkedIn job URL: %s", job.get("title", "Untitled"))
            continue
        id_match = re.search(r"/jobs/view/(\d+)", canonical_url)
        if id_match:
            job_id = id_match.group(1)
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        if skip_ids and job_id in skip_ids:
            continue

        age = _verified_posting_age(job)
        if age is None or age >= float(hours):
            LOG.debug(
                "Skipping %s: posting age is unverified or outside the %.1fh window.",
                job.get("title", "Untitled"), hours,
            )
            continue

        job["job_id"] = job_id
        job["source"] = "LinkedIn"
        job["job_url"] = canonical_url
        job["apply_url"] = canonical_url
        job["title"] = str(job.get("title") or job.get("job_title") or "").strip()
        job["company"] = str(job.get("company") or job.get("company_name") or "Unknown Company").strip()
        job["location"] = str(job.get("location") or job.get("job_location") or "").strip()
        job["description"] = str(
            job.get("description") or job.get("job_description") or job.get("full_description") or ""
        ).strip()
        job["workplace_type"] = str(
            job.get("workplace_type") or job.get("workplaceType") or "Not specified"
        ).strip()
        job["is_easy_apply"] = bool(raw_item.get("is_easy_apply", False))
        job["apply_type"] = str(raw_item.get("apply_type") or ("⚡ Easy Apply" if job["is_easy_apply"] else "Normal Apply"))
        _extract_experience_range(job, overwrite=True)

        if not job["title"] or not job["location"]:
            LOG.debug("Skipping LinkedIn record missing title or location (%s).", job_id)
            continue

        job["age_hours"] = round(age, 3)
        if age < 1:
            job["freshness_badge"] = f"<1h ago ({max(1, round(age * 60))}m)"
        else:
            job["freshness_badge"] = f"{age:.1f}h ago"
        accepted.append(job)

    # 4. Enrich full JD if descriptions are missing (prioritize target candidate roles)
    if not no_jd:
        # Only jobs that can still be reported are worth a request: a description cannot rescue a job
        # whose title or location is already out, and every extra request brings the rate limit closer.
        eligible_target = [j for j in accepted if title_is_eligible(j) and location_is_eligible(j)]
        enrich_linkedin_jds(eligible_target, proxy=proxy, max_concurrency=8, hours=hours)

    # 5. Strictly enforce posting age window (<= hours) after full JD timestamp verification
    accepted = [
        j for j in accepted
        if _as_float(j.get("age_hours")) is not None and float(j["age_hours"]) <= float(hours)
    ]

    accepted.sort(key=lambda item: float(item.get("age_hours", 999999)))
    if target_count > 0:
        accepted = accepted[:target_count]

    LOG.info("Accepted %d verified LinkedIn records matching <= %.1fh window.", len(accepted), hours)
    return accepted


# ---------------------------------------------------------------------------
# Parallel search workers
# ---------------------------------------------------------------------------
# On GitHub the search is split over several workers. Each is a separate runner (so a separate
# address for LinkedIn's rate limit) that runs `--collect-only` for some keywords in one location
# and uploads its jobs as a JSON file, together with the list of searches it was given.
# The report job then runs `--from-shards DIR`. It works out which (keyword, location) searches
# no worker file covers - a worker crashed, or a keyword was added here but not to the workflow -
# and runs those itself, so a failed worker costs time but never coverage.

def _split_list(value: str | None, default: Sequence[str]) -> List[str]:
    items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    return items or list(default)


def _split_places(value: str | None, default: Sequence[str]) -> List[str]:
    """Search places are separated by ';' because a place can hold a comma ("Pune, India")."""
    items = [item.strip() for item in str(value or "").split(";") if item.strip()]
    return items or list(default)


def write_shard(path: Path, searches: Sequence[Tuple[str, str]], jobs: Sequence[Dict[str, Any]]) -> None:
    """Write one worker's result. searches = the (keyword, location) pairs this worker was responsible for."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "searches": [list(pair) for pair in searches],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "notes": list(RUN_NOTES),
        "jobs": list(jobs),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    LOG.info("Wrote %d job(s) from %d search(es) to %s", len(jobs), len(payload["searches"]), path)


def collect_from_shards(
    shard_dir: Path,
    hours: float,
    target_count: int,
    delay: float,
    proxy: str | None = None,
    keywords: str | None = None,
    location: str | None = None,
    no_jd: bool = False,
) -> List[Dict[str, Any]]:
    """Merge the search workers' results; run any search no worker delivered."""
    wanted = [(term, loc) for loc in _split_places(location, SEARCH_LOCATIONS) for term in _split_list(keywords, SEARCH_TERMS)]
    merged: Dict[str, Dict[str, Any]] = {}
    delivered: Set[Tuple[str, str]] = set()
    files = 0
    for path in sorted(Path(shard_dir).glob("*.json")) if Path(shard_dir).is_dir() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            jobs = [j for j in payload.get("jobs", []) if isinstance(j, dict) and j.get("job_id")]
            searches = {(str(term).lower(), str(loc).lower()) for term, loc in payload.get("searches", [])}
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            LOG.warning("Ignoring unreadable worker file %s: %s", path, exc)
            continue
        files += 1
        delivered |= searches
        for note in payload.get("notes", []):
            if note not in RUN_NOTES:
                RUN_NOTES.append(str(note))
        for job in jobs:
            known = merged.get(str(job["job_id"]))
            # The same job can come from two workers (a city search and the India-remote search).
            # Keep the copy that has its description.
            if known is None or (not known.get("description") and job.get("description")):
                merged[str(job["job_id"])] = job
        LOG.info("Search worker file %s: %d job(s) from %d search(es).", path.name, len(jobs), len(searches))

    missing: Dict[str, List[str]] = {}
    for term, loc in wanted:
        if (term.lower(), loc.lower()) not in delivered:
            missing.setdefault(loc, []).append(term)
    for loc, terms in missing.items():
        note = (
            f"No search worker delivered {loc} ({', '.join(terms)}); "
            "the report step ran that search itself, so this report is later than usual."
        )
        LOG.warning(note)
        RUN_NOTES.append(note)
        for job in collect_jobs(
            hours=hours, target_count=target_count, delay=delay, source="guest", proxy=proxy,
            keywords=",".join(terms), location=loc, no_jd=no_jd,
        ):
            merged.setdefault(str(job["job_id"]), job)

    jobs = list(merged.values())
    if not no_jd:
        # Second chance, from this runner's address, for descriptions a worker could not get.
        retry = [j for j in jobs if not j.get("description") and title_is_eligible(j) and location_is_eligible(j)]
        if retry:
            enrich_linkedin_jds(retry, proxy=proxy, max_concurrency=4, hours=hours)
    jobs = [j for j in jobs if _as_float(j.get("age_hours")) is not None and float(j["age_hours"]) <= float(hours)]
    jobs.sort(key=lambda item: float(item.get("age_hours", 999999)))
    LOG.info("Merged %d job(s) from %d worker file(s); %d of %d searches came from workers.",
             len(jobs), files, len(wanted) - sum(len(t) for t in missing.values()), len(wanted))
    return jobs


def _posting_key(job: Dict[str, Any]) -> str:
    """Fingerprint of an opening: same company, same title, same description text."""
    import hashlib

    def norm(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    description = norm(job.get("description"))[:600]
    return hashlib.md5(f"{norm(job.get('company'))}|{norm(job.get('title'))}|{description}".encode("utf-8")).hexdigest()


def merge_duplicate_postings(jobs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse one opening posted several times (once per city, or re-posted) into a single row.

    The kept row is the one in a target city if there is one, then Easy Apply, then the newest.
    Its 'also_in' lists the other locations and 'duplicate_ids' the other LinkedIn job ids.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        groups.setdefault(_posting_key(job), []).append(job)
    merged = []
    for key, group in groups.items():
        group.sort(key=lambda j: (
            not any(re.search(rf"\b{re.escape(city)}\b", str(j.get("location") or "").lower()) for city in TARGET_CITIES),
            not j.get("is_easy_apply"),
            float(j.get("age_hours", 999999) or 999999),
        ))
        keep = group[0]
        keep["posting_key"] = key
        keep["duplicate_ids"] = [str(j.get("job_id")) for j in group[1:]]
        own_city = str(keep.get("location") or "").split(",")[0].strip().lower()
        also = []
        for other in group[1:]:
            city = str(other.get("location") or "").split(",")[0].strip()
            if city and city.lower() != own_city and city not in also:
                also.append(city)
        keep["also_in"] = also
        merged.append(keep)
    return merged


def location_label(job: Dict[str, Any]) -> str:
    location = str(job.get("location") or "Not specified")
    also = job.get("also_in") or []
    return f"{location} (also posted for: {', '.join(also)})" if also else location


def _as_float(value: Any) -> float | None:
    if value in (None, "", "N/A", "Not specified"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


SENIOR_TITLE_RE = re.compile(r"\b(?:senior|sr|lead)\b", re.I)


def experience_is_eligible(job: Dict[str, Any], candidate_yoe: float = 2.0) -> bool:
    """Is a candidate with candidate_yoe years inside the experience the posting asks for?

    With the default 2.0 years:
      Range "a - b yrs" : accept when a <= 2.0 <= b   (0-2, 1-2, 2-4, 2-10, 0-3, 1-3 yes; 0-1, 3-5 no)
      Minimum "a+"      : accept when a <= 2.0        (2+ yes; 3+, 4+, 5+ no)
      Maximum "up to b" : accept when b >= 2.0
      Nothing stated    : accept, unless the title says Senior / Sr / Lead
    Director and Executive seniority levels are always rejected.
    """
    seniority = str(job.get("seniority_level") or "").strip().lower()
    if any(s in seniority for s in ("director", "executive")):
        return False

    _extract_experience_range(job)
    minimum = _as_float(job.get("min_experience_years"))
    maximum = _as_float(job.get("max_experience_years"))
    if minimum is None and maximum is None:
        # No figure to go on. A Senior or Lead title is then taken at its word; when such a posting
        # does state years ("Senior Engineer, 2-4 yrs"), the figure decides instead.
        return SENIOR_TITLES_OK or not SENIOR_TITLE_RE.search(str(job.get("title") or ""))
    if minimum is not None and minimum > candidate_yoe:
        return False
    if maximum is not None and maximum < candidate_yoe:
        return False
    return True


_OFFICE_WORK_RE = re.compile(
    r"work\s*(?:mode|model|type|arrangement|location|style)\s*[:\-]\s*(?:hybrid|on-?site|office|wfo|in[- ]office)|"
    r"\bhybrid\s+(?:work\w*|model|role|position|mode|schedule|set-?up|arrangement|policy)\b|"
    r"\bwork(?:ing)?\s+from\s+(?:the\s+|our\s+)?office\b|\bwfo\b|"
    r"\bdays?\s+(?:a|per|in\s+a|every)\s+week\s+(?:in|from|at)\s+(?:the\s+|our\s+)?office\b|"
    r"\b(?:on-?site|in[- ]office)\s+(?:role|position|only|job|presence)\b",
    re.I,
)
_REMOTE_WORK_RE = re.compile(
    r"\bremote(?:ly)?\b|\bwork(?:ing)?\s+from\s+(?:home|anywhere)\b|\bwfh\b|\btelecommut\w+", re.I
)


def location_is_eligible(job: Dict[str, Any]) -> bool:
    """Strict candidate location requirements:
    1. Hyderabad, Bengaluru/Bangalore, Chennai:
       ALL work modes allowed — On-site, In-office, Hybrid, Remote, Work-from-home.
       No workplace type restriction whatsoever for these 3 cities.
    2. India Remote: Remote positions specifically based in India or open to applicants in India.
    3. Strictly excluded: Worldwide/global remote, outside-India remote, onsite/hybrid in other cities (Pune, Noida, Gurgaon, Mumbai, etc.).
    """
    location = str(job.get("location") or "").lower()
    workplace = str(job.get("workplace_type") or "").lower().replace("_", "-")
    title = str(job.get("title") or "").lower()
    description = str(job.get("description") or "").lower()
    blob = f"{title}\n{location}\n{workplace}\n{description}"

    # 1. Strictly exclude roles explicitly restricted to countries outside India
    if any(re.search(pat, location, re.I) for pat in REMOTE_ONLY_OUTSIDE_INDIA_PATTERNS):
        return False

    # 2. Check if job is in the 3 target cities: Hyderabad, Bengaluru/Bangalore, or Chennai
    # For these 3 cities, on-site, in-office, hybrid, and remote are all accepted.
    city_match = any(re.search(rf"\b{re.escape(city)}\b", location, re.I) for city in TARGET_CITIES)
    if city_match:
        return True

    # 3. Check for India Remote:
    # Must be remote AND based in India (not worldwide/global remote)
    is_remote = (
        any(term in workplace for term in ("remote", "work from home", "wfh", "distributed"))
        or bool(re.search(r"\b(remote|work from home|wfh)\b", location))
        or bool(re.search(r"\b(?:remote|wfh)\b", title))
    )

    # LinkedIn's remote filter goes by what the poster ticked. When the description itself says the job is
    # hybrid or office-based and never mentions remote work, believe the description.
    if is_remote and _OFFICE_WORK_RE.search(description) and not _REMOTE_WORK_RE.search(description):
        job["workplace_type"] = "Hybrid / on-site (stated in the description)"
        is_remote = False

    if is_remote and ALLOW_REMOTE_INDIA:
        # Check if explicitly worldwide/global remote without India base
        is_worldwide_only = bool(re.search(r"\b(?:worldwide|global(?:ly)? remote|anywhere in the world)\b", blob, re.I))
        if is_worldwide_only and "india" not in location:
            return False

        # Must have an explicit India connection
        has_india = (
            "india" in location
            or bool(re.search(r"\bindia\b", blob, re.I))
            or bool(re.search(r"\bopen to applicants in india\b", blob, re.I))
        )
        if has_india:
            return True

    # Non-target cities (e.g. Pune, Noida, Gurgaon, Mumbai) that are onsite/hybrid are excluded
    return False


def experience_label(job: Dict[str, Any]) -> str:
    minimum = _as_float(job.get("min_experience_years"))
    maximum = _as_float(job.get("max_experience_years"))
    if minimum is not None and maximum is not None:
        return f"{minimum:g}-{maximum:g} years"
    if minimum is not None:
        return f"{minimum:g}+ years"
    if maximum is not None:
        return f"Up to {maximum:g} years"
    return str(job.get("experience_requirement") or job.get("experience") or "Not specified")


def source_is_allowed(job: Dict[str, Any]) -> bool:
    if job.get("portal"):
        return bool(clean_job_url(job))
    source = str(job.get("source") or "").lower()
    url = clean_job_url(job).lower()
    # This monitor is intentionally LinkedIn-only; no other job sources are accepted.
    return ("linkedin" in source or bool(url)) and url.startswith("https://www.linkedin.com/jobs/view/")


DESCRIPTION_UNAVAILABLE = "Description not available"


def experience_is_unverified(job: Dict[str, Any]) -> bool:
    """True when LinkedIn returned no description and the title states no experience, so nothing could be checked."""
    if str(job.get("description") or "").strip():
        return False
    _extract_experience_range(job)
    return _as_float(job.get("min_experience_years")) is None and _as_float(job.get("max_experience_years")) is None


def rejection_reason(job: Dict[str, Any], enforce_title: bool = True, candidate_yoe: float = 2.0) -> str:
    """Name the first preference a job fails, or return "" when it passes all of them.

    The title comes first, so "Location" and "Experience" only ever count jobs that had a target title,
    and the role-fit reasons come last, so they only name jobs that met everything else.
    """
    if not source_is_allowed(job):
        return "Not a LinkedIn job link"
    if enforce_title and not title_is_target_role(job):
        return "Job title"
    if not location_is_eligible(job):
        return "Location"
    if not experience_is_eligible(job, candidate_yoe=candidate_yoe):
        return "Experience"
    if not employment_is_eligible(job):
        return "Not full-time"
    if enforce_title:
        other = title_other_stack(str(job.get("title") or ""))
        if other:
            return other if other == NOT_DEVELOPER_ROLE else f"{DIFFERENT_STACK}: {other}"
    if experience_is_unverified(job):
        return DESCRIPTION_UNAVAILABLE
    if enforce_title:
        return role_mismatch(job)
    return ""


def filter_job_preferences(
    jobs: Sequence[Dict[str, Any]],
    enforce_title: bool = True,
    candidate_yoe: float = 2.0,
) -> List[Dict[str, Any]]:
    return [job for job in jobs if not rejection_reason(job, enforce_title=enforce_title, candidate_yoe=candidate_yoe)]


def _short_list(items: Sequence[str], limit: int) -> str:
    items = list(items)
    more = f" +{len(items) - limit}" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def compact_table(jobs: Sequence[Dict[str, Any]]) -> str:
    """The job table of a candidate's mail: six narrow columns that fit a mail window, so nothing scrolls sideways."""
    cell = 'style="padding:5px 6px;border-bottom:1px solid #e0e0e0;vertical-align:top;word-wrap:break-word;overflow-wrap:anywhere"'
    head = 'style="padding:5px 6px;text-align:left;font-weight:bold"'
    rows = []
    for j in jobs:
        title = html.escape(str(j.get("title") or "Untitled role"))
        url = html.escape(clean_job_url(j), quote=True)
        linked = f'<a href="{url}" style="color:#1a55c4;font-weight:bold">{title}</a>' if url.startswith("https://") else f"<b>{title}</b>"
        easy = ' <span style="color:#1a73e8;white-space:nowrap">&#9889; Easy Apply</span>' if j.get("is_easy_apply") else ""
        mode = str(j.get("workplace_type") or "")
        where = html.escape(location_label(j)) + (f"<br>{html.escape(mode)}" if mode and mode != "Not specified" else "")
        have = _short_list(j.get("matched_skills", []), 8) or "none found"
        lacks = _short_list(j.get("missing_skills", []), 5)
        skills = f"<b>Have:</b> {html.escape(have)}" + (f'<br><span style="color:#666"><b>Missing:</b> {html.escape(lacks)}</span>' if lacks else "")
        vals = [
            f"<b>{int(j.get('skill_match_pct', 0))}%</b>",
            f"{linked}{easy}<br>{html.escape(str(j.get('company') or 'Unknown'))}",
            where,
            html.escape(experience_label(j)),
            html.escape(str(j.get("freshness_badge") or j.get("posted_time") or "")),
            skills,
        ]
        rows.append("<tr>" + "".join(f"<td {cell}>{v}</td>" for v in vals) + "</tr>")
    columns = (("Match", 8), ("Job", 30), ("Where", 17), ("Experience", 10), ("Posted", 11), ("Your skills", 24))
    return (
        '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;table-layout:fixed;width:100%;max-width:820px;'
        'font-size:11px;line-height:1.35">'
        + "".join(f'<col style="width:{width}%">' for _, width in columns)
        + f'<thead><tr style="background:#17365d;color:#fff">{"".join(f"<th {head}>{name}</th>" for name, _ in columns)}</tr></thead>'
        + f'<tbody>{"".join(rows)}</tbody></table>'
    )


def candidate_mail(title: str, tables: Sequence[Tuple[str, Sequence[Dict[str, Any]]]], empty: str = "") -> str:
    """A candidate's mail: a title and the job tables, nothing else.

    No run time, scan notes, counts of what was filtered out or anything about how the scan is run.
    """
    parts = "".join(
        f'<h3 style="font-size:13px;margin:16px 0 6px">{html.escape(heading)} ({len(jobs)})</h3>{compact_table(jobs)}'
        for heading, jobs in tables if jobs
    )
    if not parts:
        parts = f'<p style="margin:8px 0">{html.escape(empty or "No new jobs this time.")}</p>'
    return (
        '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124;font-size:12px;margin:0;padding:8px">'
        f'<h2 style="font-size:16px;margin:0 0 4px">{html.escape(title)}</h2>{parts}</body></html>'
    )


def html_table(jobs: Sequence[Dict[str, Any]], include_match: bool = True) -> str:
    if PROFILE:
        return compact_table(jobs)
    headers = ["Match %", "Apply Type", "Job title", "Company", "Location", "Work mode", "Posted", "Experience", "Matched skills", "Missing skills", "Direct job link"]
    rows = []
    if not jobs:
        return '<p style="color:#666;margin:8px 0 18px">No jobs in this section for this run.</p>'
    for j in jobs:
        title = html.escape(str(j.get("title") or "Untitled role"))
        url = html.escape(clean_job_url(j), quote=True)
        is_easy = bool(j.get("is_easy_apply", False))
        apply_badge = '<span style="background:#e8f0fe;color:#1a73e8;padding:2px 6px;border-radius:4px;font-weight:bold">⚡ Easy Apply</span>' if is_easy else '<span style="color:#5f6368">Normal</span>'
        link_text = "⚡ Easy Apply" if is_easy else "Open job / apply"
        link = f'<a href="{url}">{link_text}</a>' if url.startswith("https://") else "Link unavailable"
        vals = [
            f"{int(j.get('skill_match_pct', 0))}%",
            apply_badge,
            title,
            html.escape(str(j.get("company") or "Unknown")),
            html.escape(location_label(j)),
            html.escape(str(j.get("workplace_type") or "Not specified")),
            html.escape(str(j.get("freshness_badge") or j.get("posted_time") or "Unknown")),
            html.escape(experience_label(j)),
            html.escape(", ".join(j.get("matched_skills", [])) or "None detected"),
            html.escape(", ".join(j.get("missing_skills", [])) or "None detected"),
            link,
        ]
        rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    th = "".join(f"<th>{h}</th>" for h in headers)
    return (
        '<div style="overflow-x:auto"><table cellpadding="7" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;font-size:12px">'
        f'<thead><tr style="background:#17365d;color:#fff">{th}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def markdown_table(jobs: Sequence[Dict[str, Any]]) -> str:
    headers = ["Match %", "Apply Type", "Job title", "Company", "Location", "Work mode", "Posted", "Experience", "Matched skills", "Missing skills", "Direct application link"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    if not jobs:
        return "_No jobs in this section for this run._"
    def cell(value: Any) -> str:
        return str(value if value not in (None, "") else "Not specified").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    for j in jobs:
        url = clean_job_url(j)
        is_easy = bool(j.get("is_easy_apply", False))
        apply_type = "⚡ Easy Apply" if is_easy else "Normal Apply"
        link_text = "⚡ Easy Apply" if is_easy else "Open job / apply"
        link = f"[{link_text}]({url})" if url.startswith("https://") else "Link unavailable"
        experience = experience_label(j)
        row = [
            f"{int(j.get('skill_match_pct', 0))}%",
            apply_type,
            cell(j.get("title") or "Untitled role"),
            cell(j.get("company") or "Unknown"), cell(location_label(j)),
            cell(j.get("workplace_type")), cell(j.get("freshness_badge") or j.get("posted_time")),
            cell(experience), cell(", ".join(j.get("matched_skills", [])) or "None detected"),
            cell(", ".join(j.get("missing_skills", [])) or "None detected"), link,
        ]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def issue_owner() -> str:
    """GitHub login to assign and @mention on a notification issue.

    Hourly runs are started by the timer workflow, so GITHUB_ACTOR is then "github-actions[bot]",
    which cannot be assigned or notified. The repository owner is the person to tell.
    """
    for name in ("ISSUE_OWNER", "GITHUB_REPOSITORY_OWNER", "GITHUB_ACTOR"):
        login = os.environ.get(name, "").strip()
        if login and not login.endswith("[bot]"):
            return login
    return ""


def build_github_issue_body(matched: Sequence[Dict[str, Any]], all_scraped: Sequence[Dict[str, Any]], min_match: int = 60) -> str:
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    owner = issue_owner()
    mention_header = f"cc @{owner}\n\n" if owner else ""
    easy_count = sum(1 for j in matched if j.get("is_easy_apply"))
    normal_count = len(matched) - easy_count
    ratio_str = f"**Apply Mix ({min_match}%+ Matches):** ⚡ {easy_count} Easy Apply ({round(easy_count/len(matched)*100) if matched else 0}%) | 🔗 {normal_count} Normal Apply\n\n"
    return f"""# Job Match Report\n\n{mention_header}**Run time:** {run_time}  
**Skill score:** {SCORE_EXPLANATION}
**Filters:** Full-time only | {PLACES_NOTE} | {ROLE_NOTE} | one opening posted in several cities is shown once
**Notification rule:** this issue is created only when at least one new job scores {min_match}% or higher.\n\n{ratio_str}## 1. Jobs matching {min_match}% or more ({len(matched)})\n\nSorted by match percentage and Easy Apply priority.\n\n{markdown_table(matched)}\n\n## 2. All newly discovered jobs scraped ({len(all_scraped)})\n\nIncludes every new unique job collected in this run, including jobs below {min_match}%. Sorted highest first.\n\n{markdown_table(all_scraped)}\n\n---\nPlease verify eligibility and posting details on the employer's application page.\n"""


def create_github_issue(title: str, body: str) -> str:
    import subprocess

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    actor = issue_owner()
    if not token or not repo:
        LOG.warning("GITHUB_TOKEN or GITHUB_REPOSITORY not set. Skipping GitHub Issue creation.")
        return ""

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    body_path = REPORT_DIR / "github_issue_body.md"
    body_path.write_text(body, encoding="utf-8")
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", str(body_path)]
    if actor:
        cmd.extend(["--assignee", actor])
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=env)
        issue_url = result.stdout.strip()
        LOG.info("GitHub notification issue created: %s", issue_url)
        return issue_url
    except Exception as exc:
        LOG.error("Failed to create GitHub Issue via gh CLI: %s", exc)
        return ""


def send_push_notification(title: str, message: str, click_url: str = "", priority: str = "high") -> bool:
    """Send a phone push through ntfy.sh. The topic is the NTFY_TOPIC secret; without it no push is sent."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        LOG.warning("NTFY_TOPIC secret not set: phone push skipped.")
        return False
    endpoint = f"https://ntfy.sh/{topic}"
    headers = {
        "Title": title[:200],
        "Priority": priority,
        "Tags": "briefcase,bell",
    }
    if click_url and click_url.startswith("http"):
        headers["Click"] = click_url

    try:
        if requests is not None:
            r = requests.post(endpoint, data=message.encode("utf-8"), headers=headers, timeout=10)
            if r.status_code == 200:
                LOG.info("Push notification sent through ntfy.sh.")
                return True
        else:
            import urllib.request
            req = urllib.request.Request(endpoint, data=message.encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    LOG.info("Push notification sent through ntfy.sh.")
                    return True
    except Exception as exc:
        LOG.warning("Failed to send push notification to ntfy.sh: %s", exc)
    return False


def unchecked_jobs_html(unchecked: Sequence[Dict[str, Any]]) -> str:
    """Section listing jobs whose description LinkedIn did not return, so the reader can open them by hand."""
    if not unchecked:
        return ""
    rows = []
    for job in unchecked[:100]:
        url = html.escape(clean_job_url(job), quote=True)
        title = html.escape(str(job.get("title") or "Untitled role"))
        vals = [
            f'<a href="{url}">{title}</a>' if url.startswith("https://") else title,
            html.escape(str(job.get("company") or "Unknown")),
            html.escape(str(job.get("location") or "Not specified")),
            html.escape(str(job.get("freshness_badge") or job.get("posted_time") or "Unknown")),
        ]
        rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    th = "".join(f"<th>{h}</th>" for h in ("Job title", "Company", "Location", "Posted"))
    return (
        f'<h3 style="margin-top:28px">3. Not checked ({len(unchecked)})</h3>'
        "<p>LinkedIn did not return the description for these jobs, so their experience requirement and skills "
        "could not be checked. They passed the location and job-title filters; open them to check by hand.</p>"
        '<div style="overflow-x:auto"><table cellpadding="7" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;font-size:12px">'
        f'<thead><tr style="background:#5f6368;color:#fff">{th}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def is_role_mismatch_reason(reason: str) -> bool:
    return reason.startswith(DIFFERENT_STACK) or reason in (NOT_DEVELOPER_ROLE, NO_PYTHON, NO_MUST_HAVE)


def removed_jobs_html(removed: Sequence[Tuple[Dict[str, Any], str]]) -> str:
    """Section listing jobs that met location, experience and full-time but are not the candidate's kind of role."""
    if not removed:
        return ""
    rows = []
    for job, reason in removed[:80]:
        url = html.escape(clean_job_url(job), quote=True)
        title = html.escape(str(job.get("title") or "Untitled role"))
        vals = [
            f'<a href="{url}">{title}</a>' if url.startswith("https://") else title,
            html.escape(str(job.get("company") or "Unknown")),
            html.escape(location_label(job)),
            html.escape(reason),
        ]
        rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    th = "".join(f"<th>{h}</th>" for h in ("Job title", "Company", "Location", "Removed because"))
    more = f"<p>Showing the first 80 of {len(removed)}.</p>" if len(removed) > 80 else ""
    return (
        f'<h3 style="margin-top:28px">Removed as not your role ({len(removed)})</h3>'
        "<p>These met your location, experience and full-time rules but are built on another stack "
        "(Java, .NET, ...), never name Python, or are not developer jobs. "
        "They are listed only so you can see what the filter took out.</p>"
        '<div style="overflow-x:auto"><table cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;width:100%;font-size:11px;color:#5f6368">'
        f'<thead><tr style="background:#eceff1;color:#202124">{th}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>{more}'
    )


def run_notes_html() -> str:
    if not RUN_NOTES:
        return ""
    items = "".join(f"<li>{html.escape(note)}</li>" for note in RUN_NOTES)
    return f'<p style="margin-top:22px"><b>Notes on this run</b></p><ul>{items}</ul>'


SCORE_EXPLANATION = (
    "Skill score = skills you have ÷ skills the posting asks for. Nice-to-have skills count half, "
    "a language offered only as an alternative to Python is not counted against you, and a posting "
    f"that names fewer than {MIN_SKILLS_FOR_FULL_SCORE} skills is scored out of {MIN_SKILLS_FOR_FULL_SCORE}."
)


def scan_summary_html(reason_counts: Dict[str, int], hours: float) -> str:
    """One paragraph: how many postings were scanned and how many each filter took out."""
    total = sum(reason_counts.values())
    if not total:
        return ""
    passed = reason_counts.get("", 0)
    out = sorted(((reason, count) for reason, count in reason_counts.items() if reason), key=lambda kv: -kv[1])
    filtered = ", ".join(f"{html.escape(reason)} {count}" for reason, count in out)
    return (
        f"<p>Scanned <b>{total}</b> LinkedIn posting(s) from the last {hours:g} hour(s); <b>{passed}</b> passed every filter."
        + (f" Filtered out: {filtered}." if filtered else "")
        + "</p>"
    )


def build_email_html(
    matched: Sequence[Dict[str, Any]],
    all_scraped: Sequence[Dict[str, Any]],
    unchecked: Sequence[Dict[str, Any]] = (),
    removed: Sequence[Tuple[Dict[str, Any], str]] = (),
    scan_summary: str = "",
    min_match: int = 60,
) -> str:
    if PROFILE:
        below = [job for job in all_scraped if int(job.get("skill_match_pct", 0)) < min_match]
        return candidate_mail("New LinkedIn jobs for you", [
            (f"Matching your skills by {min_match}% or more", matched), (f"Other new jobs, below {min_match}%", below)])
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>LinkedIn Job Match Monitor Report</h2>
    <p>Run time: <b>{run_time}</b></p>
    {scan_summary}
    <p>{html.escape(ROLE_NOTE)}; one opening posted in several cities is shown once.</p>
    <p>{SCORE_EXPLANATION}</p>
    <h3>1. Jobs matching {min_match}% or more ({len(matched)})</h3>
    <p>Sorted by skill match percentage, highest first.</p>
    {html_table(matched)}
    <h3 style="margin-top:28px">2. All newly discovered jobs scraped ({len(all_scraped)})</h3>
    <p>This table includes every new job collected in this run, including jobs below {min_match}%. Sorted highest first.</p>
    {html_table(all_scraped)}
    {unchecked_jobs_html(unchecked)}
    {removed_jobs_html(removed)}
    {run_notes_html()}
    <p style="font-size:11px;color:#666;margin-top:22px">Automated report. Verify job requirements and eligibility on the employer's application page.</p>
    </body></html>"""


def build_status_email_html(collected: Sequence[Dict[str, Any]], hours: float, candidate_yoe: float) -> str:
    """Body of the mail sent when a run finds no eligible job: what was scanned and why each job was filtered out."""
    if PROFILE:
        return candidate_mail("LinkedIn jobs: nothing new for you", [], empty=f"No new matching jobs in the last {hours:g} hour(s).")
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    reasons = [(job, rejection_reason(job, candidate_yoe=candidate_yoe)) for job in collected]
    counts: Dict[str, int] = {}
    for _, reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    # Jobs whose title was never one of the target roles ("Business Development Manager") are counted, not listed.
    listed = [(job, reason) for job, reason in reasons if reason != "Job title"]
    off_title = len(reasons) - len(listed)
    if not collected:
        detail = (
            "<p>LinkedIn returned no job listings for this window. That can mean nothing new was posted, "
            "or that LinkedIn rate-limited this scan.</p>"
        )
    else:
        rows = []
        for job, reason in listed[:100]:
            url = html.escape(clean_job_url(job), quote=True)
            title = html.escape(str(job.get("title") or "Untitled role"))
            title_cell = f'<a href="{url}">{title}</a>' if url.startswith("https://") else title
            vals = [
                title_cell,
                html.escape(str(job.get("company") or "Unknown")),
                html.escape(str(job.get("location") or "Not specified")),
                html.escape(experience_label(job)),
                html.escape(reason),
            ]
            rows.append("<tr>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
        th = "".join(f"<th>{h}</th>" for h in ("Job title", "Company", "Location", "Experience", "Filtered out by"))
        more = f"<p>Showing the first 100 of {len(listed)} jobs.</p>" if len(listed) > 100 else ""
        not_listed = (
            f"<p>{off_title} other posting(s) had job titles outside your target roles and are not listed.</p>" if off_title else ""
        )
        table = (
            '<div style="overflow-x:auto"><table cellpadding="7" cellspacing="0" '
            'style="border-collapse:collapse;width:100%;font-size:12px">'
            f'<thead><tr style="background:#17365d;color:#fff">{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        ) if rows else ""
        detail = f"{scan_summary_html(counts, hours)}<p>Jobs with one of your target titles, and the filter each one failed:</p>{table}{more}{not_listed}"
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>LinkedIn Job Match Monitor: no eligible jobs this run</h2>
    <p>Run time: <b>{run_time}</b></p>
    <p>None of the postings from the last {hours:g} hour(s) passed the location, experience ({candidate_yoe:g} years),
    full-time, job-title and {"Python-role" if PYTHON_RULES else "role"} filters.</p>
    {detail}
    {run_notes_html()}
    <p style="font-size:11px;color:#666;margin-top:22px">Automated report. This mail is sent on every run, including runs with no matches.</p>
    </body></html>"""


def send_gmail(subject: str, html_body: str, attachment: Path | None = None) -> bool:
    """Send the mail. Returns False only when delivery was attempted and failed after three tries."""
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    # Who gets the mail is the JOB_ALERT_TO secret; no address is kept in this file.
    recipients = [r.strip() for r in re.split(r"[,;]+", os.environ.get("JOB_ALERT_TO", "")) if r.strip()]

    if PROFILE and (not sender or not app_password):
        # A candidate's run with no sending mailbox has delivered nothing, so nothing may be marked as sent.
        LOG.error("GMAIL_ADDRESS or GMAIL_APP_PASSWORD secret not set: the mail could not be sent.")
        return False
    if not sender or not app_password:
        LOG.warning(
            "GMAIL_ADDRESS or GMAIL_APP_PASSWORD secret not set in GitHub repository. "
            "Email delivery skipped. Add these secrets in Settings -> Secrets and variables -> Actions."
        )
        return True
    if not recipients:
        LOG.error(
            "JOB_ALERT_TO secret not set, so there is nobody to mail. Add it in Settings -> Secrets and variables -> Actions "
            "(the addresses, separated by commas)."
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content("Your email client does not support HTML. Please view this message in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")
    if attachment and attachment.exists():
        data = attachment.read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=attachment.name,
        )

    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            context = ssl.create_default_context()
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(sender, app_password)
                server.send_message(msg)
            LOG.info("Gmail report successfully sent to %d recipient(s).", len(recipients))
            return True
        except Exception as exc:
            problem = str(exc)
            for address in recipients:  # the log is public; keep the addresses out of it
                problem = problem.replace(address, "<recipient>")
            LOG.error("Failed to send email (attempt %d of %d): %s", attempt, attempts, problem)
            if attempt < attempts:
                time.sleep(10 * attempt)
    return False


def create_excel(matched: Sequence[Dict[str, Any]], all_scraped: Sequence[Dict[str, Any]], min_match: int = 60) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"job_match_report_{stamp}.xlsx"
    wb = Workbook()
    wb.remove(wb.active)
    columns = ["Match %", "Apply Type", "Job Title", "Company", "Location", "Workplace", "Posted", "Min Experience", "Matched Skills", "Missing Skills", "Job URL"]
    for sheet_name, data in ((f"{min_match}%+ Matches", matched), ("All Scraped", all_scraped)):
        ws = wb.create_sheet(sheet_name)
        ws.append(columns)
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="17365D")
            cell.alignment = Alignment(horizontal="center")
        for j in data:
            is_easy = bool(j.get("is_easy_apply", False))
            ws.append([
                int(j.get("skill_match_pct", 0)), "⚡ Easy Apply" if is_easy else "Normal Apply",
                j.get("title", ""), j.get("company", ""),
                location_label(j), j.get("workplace_type", ""),
                j.get("freshness_badge", j.get("posted_time", "")),
                experience_label(j), ", ".join(j.get("matched_skills", [])),
                ", ".join(j.get("missing_skills", [])), clean_job_url(j),
            ])
            cell = ws.cell(ws.max_row, 11)
            if cell.value and str(cell.value).startswith("https://"):
                cell.hyperlink = cell.value
                cell.style = "Hyperlink"
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        widths = [10, 16, 32, 24, 25, 14, 18, 16, 38, 38, 55]
        for idx, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(idx)].width = width
    wb.save(path)
    return path


# ---------------------------------------------------------------------------
# Hyderabad fast alert
# ---------------------------------------------------------------------------
# A second, much more frequent scan beside the hourly report (`--fast-alert`). It searches Hyderabad
# only, every few minutes, and notifies at once when a new opening is all of:
#   - in Hyderabad (any work mode), full-time, and a role the hourly report would keep,
#   - a skill match of FAST_ALERT_MIN_MATCH % or more,
#   - asking for experience that includes the candidate's years.
# A posting that states no experience never starts an alert. If it meets everything else it is held
# back and listed in a second table of the next alert that does go out.

FAST_ALERT_LOCATION = "Hyderabad"
HYDERABAD_NAMES = ("hyderabad", "secunderabad")
FAST_ALERT_MIN_MATCH = 70
FAST_STATE_FILE = DATA_DIR / "hyderabad_alert_state.json"
# A held-back opening (no experience stated) is dropped when no alert goes out within this many hours of its posting.
FAST_HELD_HOURS = 24.0
# Longest one pass may spend searching. A rate-limited runner then gives up and the next runner takes over.
FAST_SEARCH_BUDGET_SECONDS = 150.0
WORK_MODE_UNKNOWN = "work mode not stated"

# LinkedIn's public job page has no work-mode field and its public search ignores the work-mode
# filter, so the posting's own words are the only source for Remote / Hybrid / On-site.
_WORK_MODE_FIELD = r"\bwork(?:ing|place)?\s*(?:mode|model|type|arrangement|location|style|set-?up)\s*[:\-]\s*"
_DAYS_IN_OFFICE = r"\s*days?\s+(?:a|per|in\s+a|every|each)\s+week\s+(?:in|from|at)\s+(?:the\s+|our\s+)?office\b"
_HYBRID_MODE_RE = re.compile(
    _WORK_MODE_FIELD + r"hybrid\b|"
    r"\bhybrid\s+(?:work\w*|model|role|position|mode|schedule|set-?up|arrangement|policy|environment|opportunity|job)\b|"
    r"[(\[]\s*hybrid\s*[)\]]|"
    r"\b[1-4]" + _DAYS_IN_OFFICE,
    re.I,
)
_ONSITE_MODE_RE = re.compile(
    _WORK_MODE_FIELD + r"(?:on-?site|office|wfo|in[- ]office|work\s+from\s+office)\b|"
    r"\bwork(?:ing)?\s+from\s+(?:the\s+|our\s+)?office\b|\bwfo\b|"
    r"\b(?:on-?site|in[- ]office)\s+(?:role|position|only|job|presence|work\w*|opportunity)\b|"
    r"[(\[]\s*(?:on-?site|in[- ]office|wfo)\s*[)\]]|"
    r"\b5" + _DAYS_IN_OFFICE,
    re.I,
)
_REMOTE_MODE_RE = re.compile(
    _WORK_MODE_FIELD + r"(?:remote|wfh|work\s+from\s+home)\b|"
    r"\b(?:fully|100\s*%|completely|permanent(?:ly)?)\s+remote\b|"
    r"\bremote\s+(?:role|position|job|opportunity|work\b|working|first|only)|"
    r"\bwork(?:ing)?\s+from\s+(?:home|anywhere)\b|\bwfh\b|[(\[]\s*remote\s*[)\]]",
    re.I,
)
# In a job title or location the bare word is a statement: "Python Developer (Remote)", "Hyderabad - Hybrid".
_TITLE_WORK_MODES: List[Tuple[str, "re.Pattern[str]"]] = [
    ("Hybrid", re.compile(r"\bhybrid\b", re.I)),
    ("Remote", re.compile(r"\bremote\b|\bwfh\b|\bwork\s+from\s+home\b", re.I)),
    ("On-site", re.compile(r"\bon-?site\b|\bin[- ]office\b|\bwfo\b", re.I)),
]


def work_mode_label(job: Dict[str, Any]) -> str:
    """Remote, Hybrid or On-site as the posting says it; WORK_MODE_UNKNOWN when it does not say."""
    heading = f"{job.get('title') or ''}\n{job.get('location') or ''}"
    for name, pattern in _TITLE_WORK_MODES:
        if pattern.search(heading):
            return name
    description = str(job.get("description") or "")
    hybrid = bool(_HYBRID_MODE_RE.search(description))
    onsite = bool(_ONSITE_MODE_RE.search(description))
    remote = bool(_REMOTE_MODE_RE.search(description))
    if hybrid or (onsite and remote):
        return "Hybrid"
    if remote:
        return "Remote"
    if onsite:
        return "On-site"
    # A work mode supplied with the record (a hand-made data/linkedin_jobs.json entry).
    given = str(job.get("workplace_type") or "").lower()
    for name, words in (("Hybrid", ("hybrid",)), ("Remote", ("remote",)), ("On-site", ("on-site", "onsite", "office"))):
        if any(word in given for word in words):
            return name
    return WORK_MODE_UNKNOWN


def is_hyderabad_job(job: Dict[str, Any]) -> bool:
    location = str(job.get("location") or "").lower()
    return any(re.search(rf"\b{name}\b", location) for name in HYDERABAD_NAMES)


def experience_is_stated(job: Dict[str, Any]) -> bool:
    """The posting gives a figure for years of experience (in its title, description or experience field)."""
    _extract_experience_range(job)
    return _as_float(job.get("min_experience_years")) is not None or _as_float(job.get("max_experience_years")) is not None


def fast_alert_class(job: Dict[str, Any], candidate_yoe: float = 2.0, min_match: int = FAST_ALERT_MIN_MATCH) -> str:
    """'alert', 'no_yoe' or '' for one job whose description has been read.

    alert  : Hyderabad, full-time, passes every filter of the hourly report, scores min_match or more,
             and states experience that includes candidate_yoe.
    no_yoe : the same, except that the posting states no experience at all.
    """
    if not is_hyderabad_job(job) or rejection_reason(job, candidate_yoe=candidate_yoe):
        return ""
    score_job(job)
    if int(job.get("skill_match_pct", 0)) < min_match:
        return ""
    return "alert" if experience_is_stated(job) else "no_yoe"


def load_fast_state() -> Dict[str, Any]:
    """State of the Hyderabad watcher.

    decided : {job id: when} - postings already read and judged; they are not fetched again
    alerted : {job id or "k:" + fingerprint: when} - openings already sent in an alert
    queue   : openings that qualify for an alert and have not been delivered yet
    held    : openings that qualify except for stating no experience, waiting for the next alert
    """
    state: Dict[str, Any] = {"decided": {}, "alerted": {}, "queue": [], "held": []}
    try:
        payload = json.loads(FAST_STATE_FILE.read_text(encoding="utf-8"))
        for key, kind in (("decided", dict), ("alerted", dict), ("queue", list), ("held", list)):
            if isinstance(payload.get(key), kind):
                state[key] = payload[key]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as exc:
        LOG.warning("Could not read the Hyderabad alert state; starting empty: %s", exc)
    return state


def save_fast_state(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    def newer_than(stamps: Dict[str, str], days: float) -> Dict[str, str]:
        cutoff = (now - timedelta(days=days)).isoformat()
        return {key: stamp for key, stamp in stamps.items() if str(stamp) >= cutoff}

    # A posting is only ever seen while it is inside the search window, so judgements need not be kept long.
    state["decided"] = newer_than(state["decided"], 3)
    state["alerted"] = newer_than(state["alerted"], 45)
    FAST_STATE_FILE.write_text(json.dumps({**state, "updated_at": now.isoformat()}, ensure_ascii=False), encoding="utf-8")


_FAST_RECORD_FIELDS = (
    "job_id", "source", "title", "company", "location", "also_in", "duplicate_ids", "posting_key", "workplace_type",
    "is_easy_apply", "skill_match_pct", "matched_skills", "missing_skills", "min_experience_years",
    "max_experience_years", "experience_requirement", "job_url", "apply_url", "posted_at",
)


def _fast_record(job: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a job that an alert needs, small enough to keep in the state file."""
    record = dict(job)
    record["workplace_type"] = work_mode_label(job)
    age = _as_float(job.get("age_hours")) or 0.0
    record["posted_at"] = (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
    record["job_url"] = record["apply_url"] = clean_job_url(job)
    return {key: record[key] for key in _FAST_RECORD_FIELDS if key in record}


def _hours_since_posted(record: Dict[str, Any]) -> float | None:
    posted = _parse_linkedin_timestamp(record.get("posted_at"))
    if posted is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - posted).total_seconds() / 3600.0)


def _with_current_age(record: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of a stored record whose 'Posted' text is worked out as of now."""
    out = dict(record)
    age = _hours_since_posted(record)
    if age is not None:
        out["age_hours"] = round(age, 3)
        out["freshness_badge"] = f"<1h ago ({max(1, round(age * 60))}m)" if age < 1 else f"{age:.1f}h ago"
    return out


def _alert_ids(record: Dict[str, Any]) -> List[str]:
    """Everything that identifies an opening: its id, the ids of its copies, and its fingerprint."""
    ids = [str(record.get("job_id")), *[str(x) for x in record.get("duplicate_ids") or []]]
    if record.get("posting_key"):
        ids.append(SEEN_KEY_PREFIX + str(record["posting_key"]))
    return ids


def fast_alert_name(alerts: Sequence[Dict[str, Any]]) -> str:
    """Name of the alert, used as the mail subject: "Hyderabad (Hybrid) jobs apply fast" (the candidate's fast-alert city)."""
    modes: List[str] = []
    for record in alerts:
        mode = str(record.get("workplace_type") or WORK_MODE_UNKNOWN)
        if mode not in modes:
            modes.append(mode)
    modes.sort(key=lambda mode: mode == WORK_MODE_UNKNOWN)
    return f"{FAST_ALERT_LOCATION} ({', '.join(modes) or WORK_MODE_UNKNOWN}) jobs apply fast"


def _fast_alert_rule(candidate_yoe: float, min_match: int) -> str:
    return (
        f"in {FAST_ALERT_LOCATION} (any work mode), full-time, a skill match of {min_match}% or more, "
        f"and asking for experience that includes your {candidate_yoe:g} years"
    )


def build_fast_alert_email_html(
    name: str,
    alerts: Sequence[Dict[str, Any]],
    held: Sequence[Dict[str, Any]],
    candidate_yoe: float,
    min_match: int,
) -> str:
    if PROFILE:
        return candidate_mail(name, [("Apply now", alerts), ("Also a match, but the posting states no years of experience", held)])
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    held_table = html_table(held) if held else '<p style="color:#666;margin:8px 0 18px">None found since the last alert.</p>'
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>{html.escape(name)}</h2>
    <p>Run time: <b>{run_time}</b></p>
    <p>Every job in table 1 is {html.escape(_fast_alert_rule(candidate_yoe, min_match))}.</p>
    <h3>1. Apply now ({len(alerts)})</h3>
    {html_table(alerts)}
    <h3 style="margin-top:28px">2. Also qualified, but the posting states no experience ({len(held)})</h3>
    <p>These meet the same rules ({html.escape(FAST_ALERT_LOCATION)}, full-time, {min_match}% or more), but the posting gives no years of
    experience. Such a job never starts an alert by itself; it is listed here with the next alert, if one goes
    out within {FAST_HELD_HOURS:g} hours of its posting.</p>
    {held_table}
    <p>{SCORE_EXPLANATION}</p>
    <p style="font-size:11px;color:#666;margin-top:22px">Automated alert from the {html.escape(FAST_ALERT_LOCATION)} scan, which runs every few minutes.
    The hourly report still lists these jobs with all the others. Verify the requirements on the employer's application page.</p>
    </body></html>"""


def build_fast_alert_issue_body(
    name: str,
    alerts: Sequence[Dict[str, Any]],
    held: Sequence[Dict[str, Any]],
    candidate_yoe: float,
    min_match: int,
) -> str:
    run_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
    owner = issue_owner()
    mention = f"cc @{owner}\n\n" if owner else ""
    return (
        f"# {name}\n\n{mention}**Run time:** {run_time}  \n"
        f"**Rule:** every job in table 1 is {_fast_alert_rule(candidate_yoe, min_match)}.\n\n"
        f"## 1. Apply now ({len(alerts)})\n\n{markdown_table(alerts)}\n\n"
        f"## 2. Also qualified, but the posting states no experience ({len(held)})\n\n"
        f"Same rules, but the posting gives no years of experience, so these never start an alert by themselves.\n\n"
        f"{markdown_table(held) if held else '_None found since the last alert._'}\n\n"
        f"---\n{SCORE_EXPLANATION}\n"
    )


def _alert_order(record: Dict[str, Any]) -> Tuple[int, int, float]:
    return (-int(record.get("skill_match_pct", 0)), 0 if record.get("is_easy_apply") else 1, float(record.get("age_hours", 0) or 0))


def send_fast_alert(
    alerts: Sequence[Dict[str, Any]],
    held: Sequence[Dict[str, Any]],
    candidate_yoe: float,
    min_match: int,
    dry_run: bool = False,
) -> Dict[str, bool]:
    """Push, mail and issue for one alert. 'delivered' is True when at least one of them went out."""
    alerts = sorted((_with_current_age(r) for r in alerts), key=_alert_order)
    held = sorted((_with_current_age(r) for r in held), key=_alert_order)
    name = fast_alert_name(alerts)
    mail_body = build_fast_alert_email_html(name, alerts, held, candidate_yoe, min_match)
    issue_body = build_fast_alert_issue_body(name, alerts, held, candidate_yoe, min_match)
    if dry_run:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "hyderabad_alert_preview.html").write_text(mail_body, encoding="utf-8")
        (REPORT_DIR / "hyderabad_alert_preview.md").write_text(issue_body, encoding="utf-8")
        LOG.info("Dry run: alert %r not sent; preview written to %s", name, REPORT_DIR)
        return {"delivered": True, "mailed": True}

    top = alerts[0]
    more = f" (+{len(alerts) - 1} more)" if len(alerts) > 1 else ""
    no_years = f" Also {len(held)} without stated experience." if held else ""
    message = (
        f"{top.get('title')} at {top.get('company')}{more}: {int(top.get('skill_match_pct', 0))}% match, "
        f"{experience_label(top)}, {top.get('workplace_type')}"
        f"{', Easy Apply' if top.get('is_easy_apply') else ''}, posted {top.get('freshness_badge', 'just now')}.{no_years}"
    )
    # The push goes first: it is the quickest way to reach the phone, and it opens the job itself.
    pushed = send_push_notification(name, message, click_url=str(top.get("job_url") or ""), priority="urgent")
    mailed = send_gmail(name, mail_body)
    run_stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p IST")
    issue_url = create_github_issue(f"{name} | {run_stamp}", issue_body)
    return {"delivered": bool(pushed or mailed or issue_url), "mailed": bool(mailed)}


def fast_alert_pass(state: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    """One scan of Hyderabad: judge the postings not judged before and send an alert if one qualifies."""
    RUN_NOTES.clear()
    SEARCH_STATS.update(pages=0, failed=0)
    now_iso = datetime.now(timezone.utc).isoformat()
    jobs = collect_jobs(
        hours=args.hours, target_count=0, delay=args.delay, source=args.source, proxy=args.proxy,
        keywords=args.keywords, location=FAST_ALERT_LOCATION,
        skip_ids=set(state["decided"]),
        # One empty page ends a search: this scan repeats every few minutes, so it keeps its request count low.
        search_options={"empty_pages_to_stop": 1, "budget_seconds": FAST_SEARCH_BUDGET_SECONDS},
    )
    candidates = []
    waiting = 0
    for job in jobs:
        if is_hyderabad_job(job) and title_is_eligible(job) and not str(job.get("description") or "").strip():
            waiting += 1  # LinkedIn did not return the description; the next pass asks again
            continue
        state["decided"][str(job["job_id"])] = now_iso
        kind = fast_alert_class(job, candidate_yoe=args.candidate_yoe, min_match=args.fast_min_match)
        if kind:
            job["fast_class"] = kind
            candidates.append(job)

    known = set(state["alerted"]) | {x for record in state["queue"] + state["held"] for x in _alert_ids(record)}
    new_alerts = new_held = 0
    for job in merge_duplicate_postings(candidates):
        record = _fast_record(job)
        ids = _alert_ids(record)
        if any(x in known for x in ids):
            continue  # already sent, or the same opening re-posted under a new id
        known.update(ids)
        if job["fast_class"] == "alert":
            state["queue"].append(record)
            new_alerts += 1
        else:
            state["held"].append(record)
            new_held += 1
    state["held"] = [r for r in state["held"] if (_hours_since_posted(r) or 0.0) <= FAST_HELD_HOURS]

    result: Dict[str, Any] = {
        "scanned": len(jobs), "waiting": waiting, "new_alerts": new_alerts, "new_held": new_held,
        "sent": False, "mail_failed": False,
    }
    if state["queue"]:
        outcome = send_fast_alert(state["queue"], state["held"], args.candidate_yoe, args.fast_min_match, dry_run=args.dry_run)
        result["mail_failed"] = not outcome["mailed"]
        if outcome["delivered"]:
            result["sent"] = True
            for record in state["queue"] + state["held"]:
                for x in _alert_ids(record):
                    state["alerted"][x] = now_iso
            state["queue"], state["held"] = [], []
        else:
            LOG.error("The alert could not be delivered by push, mail or issue; it stays queued for the next pass.")
    LOG.info(
        "%s pass: %d unjudged posting(s) read, %d still without a description, %d new alert(s), "
        "%d held for stating no experience (%d waiting in all), alert sent: %s.",
        FAST_ALERT_LOCATION, len(jobs), waiting, new_alerts, new_held, len(state["held"]), "yes" if result["sent"] else "no",
    )
    return result


def run_fast_alert(args: argparse.Namespace) -> int:
    """Scan Hyderabad now and then every --interval-minutes until --watch-minutes have passed (0 = one scan)."""
    state = load_fast_state()
    interval = max(60.0, float(args.interval_minutes) * 60.0)
    stop_at = time.monotonic() + max(0.0, float(args.watch_minutes)) * 60.0
    passes = crashed = 0
    mail_failed = False
    while True:
        started = time.monotonic()
        passes += 1
        try:
            mail_failed = fast_alert_pass(state, args)["mail_failed"] or mail_failed
        except Exception:
            # One bad pass must not stop the watcher, and must not send a failure mail every few minutes.
            crashed += 1
            LOG.exception("Hyderabad pass %d failed; carrying on.", passes)
        if not args.dry_run:
            save_fast_state(state)
        if started + interval > stop_at:
            break
        if SEARCH_STATS["failed"] >= 3 or (SEARCH_STATS["failed"] and SEARCH_STATS["failed"] == SEARCH_STATS["pages"]):
            LOG.warning("LinkedIn is refusing this runner's searches; stopping early so the next run starts from a new address.")
            break
        time.sleep(max(0.0, started + interval - time.monotonic()))
    LOG.info("Hyderabad watcher finished: %d pass(es), %d failed.", passes, crashed)
    if crashed == passes:
        return 1
    return _exit_code(not mail_failed)


# ---------------------------------------------------------------------------
# Candidate profiles
# ---------------------------------------------------------------------------
# One JSON file per candidate (see candidates.example/). A run started with --profile loads one file
# and apply_profile() replaces the built-in candidate's settings with it: skills, roles, cities,
# years of experience, and who gets the mail. Each run is for exactly one candidate.
#
# Two sets of role rules exist:
#   python_rules = true  : the detailed rules written for a Python / GenAI developer (role_mismatch).
#   python_rules = false : the general rules - the title must read as one of the candidate's roles,
#                          must not belong to a job family the candidate avoids, and the posting
#                          must name one of the candidate's must-have skills (when any are given).

# Cities and the other names postings use for them. A city that is not here still works under its own name.
CITY_ALIASES: Dict[str, Tuple[str, ...]] = {
    "hyderabad": ("hyderabad", "secunderabad"),
    "bengaluru": ("bengaluru", "bangalore", "bgl"),
    "chennai": ("chennai", "madras"),
    "pune": ("pune", "pimpri"),
    "mumbai": ("mumbai", "bombay", "navi mumbai", "thane"),
    "delhi": ("delhi", "new delhi"),
    "gurugram": ("gurugram", "gurgaon"),
    "noida": ("noida", "greater noida"),
    "kolkata": ("kolkata", "calcutta"),
    "ahmedabad": ("ahmedabad", "gandhinagar"),
    "kochi": ("kochi", "cochin", "ernakulam"),
    "thiruvananthapuram": ("thiruvananthapuram", "trivandrum"),
    "coimbatore": ("coimbatore",),
    "jaipur": ("jaipur",),
    "indore": ("indore",),
    "chandigarh": ("chandigarh", "mohali"),
    "visakhapatnam": ("visakhapatnam", "vizag"),
    "vijayawada": ("vijayawada",),
    "bhubaneswar": ("bhubaneswar",),
    "nagpur": ("nagpur",),
    "mysuru": ("mysuru", "mysore"),
    "lucknow": ("lucknow",),
}
# Cities whose bare name is a proven LinkedIn search; any other city is searched as "<city>, India".
_BARE_SEARCH_CITIES = ("hyderabad", "bengaluru", "chennai")

# Skills beyond the built-in vocabulary, so that a tester's, an analyst's or a Java developer's
# postings are scored on what they ask for. Added to SKILL_PATTERNS when a profile is applied.
EXTRA_SKILL_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "spring boot": (r"\bspring\s?boot\b",),
    "hibernate": (r"\bhibernate\b", r"\bjpa\b"),
    "maven": (r"\bmaven\b",),
    "gradle": (r"\bgradle\b",),
    "junit": (r"\bjunit\b",),
    "asp.net core": (r"\basp\.net\s+core\b", r"\b\.net\s+core\b"),
    "entity framework": (r"\bentity\s+framework\b",),
    "redux": (r"\bredux\b",),
    "tailwind": (r"\btailwind(?:\s?css)?\b",),
    "bootstrap": (r"\bbootstrap\b",),
    "jquery": (r"\bjquery\b",),
    "sass": (r"\bsass\b", r"\bscss\b"),
    "figma": (r"\bfigma\b",),
    "manual testing": (r"\bmanual\s+test(?:ing|er)?\b",),
    "automation testing": (r"\b(?:test\s+automation|automation\s+test(?:ing|er)?|automated\s+test(?:ing|s)?)\b",),
    "api testing": (r"\bapi\s+test(?:ing|s)?\b", r"\brest\s?assured\b"),
    "cypress": (r"\bcypress\b",),
    "playwright": (r"\bplaywright\b",),
    "appium": (r"\bappium\b",),
    "testng": (r"\btestng\b",),
    "cucumber": (r"\bcucumber\b", r"\bbdd\b"),
    "jmeter": (r"\bjmeter\b",),
    "jira": (r"\bjira\b",),
    "excel": (r"\b(?:ms|microsoft)?\s?excel\b",),
    "dax": (r"\bdax\b",),
    "etl": (r"\betl\b", r"\belt\b"),
    "data modeling": (r"\bdata\s+model(?:ing|ling)\b",),
    "statistics": (r"\bstatistic(?:s|al)\b",),
    "machine learning": (r"\bmachine\s+learning\b", r"\bml\b"),
    "deep learning": (r"\bdeep\s+learning\b",),
    "nlp": (r"\bnlp\b", r"\bnatural\s+language\s+processing\b"),
    "computer vision": (r"\bcomputer\s+vision\b", r"\bopencv\b"),
    "scikit-learn": (r"\bscikit[- ]learn\b", r"\bsklearn\b"),
    "numpy": (r"\bnumpy\b",),
    "r": (r"(?<![\w&])r\s+(?:programming|language|studio)\b", r"\brstudio\b"),
    "bash": (r"\bbash\b", r"\bshell\s+script(?:ing|s)?\b"),
    "prometheus": (r"\bprometheus\b",),
    "grafana": (r"\bgrafana\b",),
    "helm": (r"\bhelm\b",),
    "github actions": (r"\bgithub\s+actions\b",),
    "gitlab ci": (r"\bgitlab(?:\s+ci)?\b",),
    "azure devops": (r"\bazure\s+devops\b",),
    "sap abap": (r"\babap\b",),
    "sap fico": (r"\bsap\s+fi(?:co)?\b", r"\bfico\b"),
    "sap mm": (r"\bsap\s+mm\b",),
    "sap sd": (r"\bsap\s+sd\b",),
    "sap hana": (r"\bhana\b",),
    "data structures": (r"\bdata\s+structures\b", r"\bdsa\b"),
    "system design": (r"\bsystem\s+design\b",),
}

_ROLE_NOUN = r"(?:developer|engineer|programmer|development|engineering|sde|swe)"
# Words of a role and what a title may say instead. "Generative AI" is listed before "AI".
_ROLE_PHRASES: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"full[- ]?stack"), r"full[- ]?stack"),
    (re.compile(r"back[- ]?end"), r"back[- ]?end"),
    (re.compile(r"front[- ]?end"), r"front[- ]?end"),
    (re.compile(r"dev[- ]?ops"), r"dev[- ]?ops"),
    (re.compile(r"\bqa\b|quality\s+assurance|\btest(?:er|ing)?\b|\bsdet\b"), r"(?:qa|quality\s+assurance|sdet|test(?:er|ing)?)"),
    (re.compile(r"\bgen(?:erative)?[- ]?ai\b"), r"(?:gen[- ]?ai|generative\s+ai)"),
    (re.compile(r"\bml\b|machine\s+learning"), r"(?:ml|machine\s+learning)"),
    (re.compile(r"\bai\b|artificial\s+intelligence"), r"(?:ai|a\.i\.|artificial\s+intelligence)"),
    (re.compile(r"\bdeveloper\b|\bengineer\b|\bprogrammer\b"), _ROLE_NOUN),
]
_ROLE_FILLER = {"and", "of", "the", "for", "in", "a", "an", "jr", "junior", "senior", "sr", "lead", "associate", "i", "ii"}
# A candidate added on a free trial is scanned for this many days (their file can say otherwise),
# and is sent the welcome mail once, within this many hours of being added.
TRIAL_DAYS = 7
WELCOME_WINDOW_HOURS = 24.0
_PROFILE_ID_RE = re.compile(r"^[a-z0-9]{3,24}$")
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _literal_pattern(words: str) -> str:
    """A pattern for a skill or title word typed by hand: the words themselves, not part of a longer word."""
    return rf"(?<![\w+#]){re.escape(words.strip().lower()).replace(chr(92) + ' ', r'\s+')}(?![\w+#])"


def role_title_pattern(role: str) -> str:
    """A title pattern for one role: every word of the role has to be in the title, in any order.

    "Java Developer" matches "Senior Java Backend Engineer"; "QA Engineer" matches "Software Test Engineer".
    """
    rest = str(role or "").lower()
    needs: List[str] = []
    for finder, wanted in _ROLE_PHRASES:
        if finder.search(rest):
            needs.append(wanted)
            rest = finder.sub(" ", rest)
    needs += [re.escape(word) for word in re.findall(r"[a-z0-9+#.]+", rest) if word.strip(".") and word not in _ROLE_FILLER]
    if not needs:
        return ""
    return "^" + "".join(rf"(?=.*(?<![\w+#])(?:{need})(?![\w+#]))" for need in needs)


def canonical_city(name: Any) -> str:
    """'bangalore' -> 'Bengaluru'; a city that is not in CITY_ALIASES keeps its own name."""
    lowered = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    for city, names in CITY_ALIASES.items():
        if lowered in names:
            return city.title()
    return lowered.title()


def city_names(city: str) -> Tuple[str, ...]:
    """Every lower-case name a posting may use for the city."""
    lowered = canonical_city(city).lower()
    return CITY_ALIASES.get(lowered, (lowered,)) if lowered else ()


def search_place(city: str) -> str:
    """What LinkedIn is asked for: 'Hyderabad', or 'Pune, India' for a city outside the proven three."""
    name = canonical_city(city)
    return name if name.lower() in _BARE_SEARCH_CITIES else f"{name}, India"


def _clean_list(value: Any) -> List[str]:
    """A list from a JSON list or a comma-separated string: trimmed, no blanks, no repeats (case ignored)."""
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;\n]+", str(value or ""))
    out: List[str] = []
    for item in items:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text and text.lower() not in {x.lower() for x in out}:
            out.append(text)
    return out


def normalise_profile(raw: Any) -> Dict[str, Any]:
    """Check one candidate's settings and fill in the defaults. Raises ValueError naming every problem."""
    if not isinstance(raw, dict):
        raise ValueError("a candidate file must hold one JSON object")
    problems: List[str] = []
    profile_id = str(raw.get("id") or "").strip().lower()
    if not _PROFILE_ID_RE.match(profile_id):
        problems.append("id must be 3 to 24 lower-case letters or digits")
    emails = [address for address in _clean_list(raw.get("emails") or raw.get("email")) if _EMAIL_RE.match(address)]
    if not emails:
        problems.append("at least one valid email address is needed")
    roles = _clean_list(raw.get("roles"))
    if not any(role_title_pattern(role) for role in roles):
        problems.append("at least one role (job title) is needed")
    skills = [skill.lower() for skill in _clean_list(raw.get("skills"))]
    if not skills:
        problems.append("at least one skill is needed")
    cities = _clean_list([canonical_city(city) for city in _clean_list(raw.get("cities"))])
    remote = bool(raw.get("remote_india", True))
    if not cities and not remote:
        problems.append("at least one city is needed, or remote jobs have to be allowed")
    try:
        years = float(raw.get("experience_years"))
        if not 0 <= years <= 50:
            raise ValueError
    except (TypeError, ValueError):
        years = 0.0
        problems.append("experience_years must be a number from 0 to 50")
    plan = str(raw.get("plan") or "full").strip().lower()
    if plan not in ("trial", "full"):
        problems.append('plan must be "trial" or "full"')
    started = _parse_time(raw.get("trial_started"))
    if plan == "trial" and started is None:
        problems.append("a trial needs the time it started (trial_started)")
    try:
        trial_days = min(365, max(1, int(raw.get("trial_days", TRIAL_DAYS))))
    except (TypeError, ValueError):
        trial_days = TRIAL_DAYS
    if problems:
        raise ValueError("; ".join(problems))

    def part(name: str, default_match: int) -> Dict[str, Any]:
        given = raw.get(name) if isinstance(raw.get(name), dict) else {}
        try:
            match = min(100, max(0, int(given.get("min_match", default_match))))
        except (TypeError, ValueError):
            match = default_match
        return {**given, "enabled": bool(given.get("enabled", True)), "min_match": match}

    linkedin, fast, sites = part("linkedin", 60), part("fast_alert", FAST_ALERT_MIN_MATCH), part("career_sites", 60)
    linkedin["mail_every_run"] = bool(linkedin.get("mail_every_run", False))
    fast["city"] = canonical_city(fast.get("city") or (cities[0] if cities else ""))
    fast["enabled"] = fast["enabled"] and bool(fast["city"])
    sites["companies"] = [key.lower() for key in _clean_list(sites.get("companies"))]
    return {
        "id": profile_id,
        "name": re.sub(r"\s+", " ", str(raw.get("name") or "")).strip(),
        "emails": emails,
        "ntfy_topic": re.sub(r"[^A-Za-z0-9_-]", "", str(raw.get("ntfy_topic") or "")),
        "enabled": bool(raw.get("enabled", True)),
        "experience_years": years,
        "roles": roles,
        "exclude_title_words": _clean_list(raw.get("exclude_title_words")),
        "skills": skills,
        "must_have": [skill.lower() for skill in _clean_list(raw.get("must_have"))],
        "python_rules": bool(raw.get("python_rules", False)),
        "avoid_stacks": [name for name in _clean_list(raw.get("avoid_stacks")) if name in avoidable_stacks()],
        "cities": cities,
        "remote_india": remote,
        "linkedin": linkedin,
        "fast_alert": fast,
        "career_sites": sites,
        "plan": plan,
        "trial_started": started.isoformat() if started else "",
        "trial_days": trial_days,
    }


def _parse_time(value: Any) -> datetime | None:
    """A time written by the settings page ("2026-11-21T12:30:00.000Z"); None when it is missing or unreadable."""
    try:
        moment = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def trial_end(profile: Dict[str, Any]) -> datetime | None:
    """When a candidate's free trial ends; None for a candidate with full access."""
    started = _parse_time(profile.get("trial_started"))
    if profile.get("plan") != "trial" or started is None:
        return None
    return started + timedelta(days=int(profile.get("trial_days") or TRIAL_DAYS))


def trial_is_over(profile: Dict[str, Any], now: datetime | None = None) -> bool:
    """The candidate is on a trial and its last day has passed: no scan runs for them any more."""
    end = trial_end(profile)
    return end is not None and (now or datetime.now(timezone.utc)) >= end


def welcome_is_due(profile: Dict[str, Any], now: datetime | None = None) -> bool:
    """The candidate's trial started within the last WELCOME_WINDOW_HOURS, so the welcome mail belongs to now.

    The workflow remembers who has been welcomed; the window keeps a welcome from ever going out late,
    for example to someone whose record was lost long after they were added.
    """
    started = _parse_time(profile.get("trial_started"))
    now = now or datetime.now(timezone.utc)
    if profile.get("plan") != "trial" or started is None or not profile.get("enabled", True) or trial_is_over(profile, now):
        return False
    return timedelta(0) <= now - started <= timedelta(hours=WELCOME_WINDOW_HOURS)


def build_welcome_email_html(profile: Dict[str, Any]) -> str:
    """The mail a candidate gets once, when they are added on a free trial."""
    end = trial_end(profile)
    ends_on = end.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y") if end else ""
    name = html.escape(profile["name"].split(" ")[0]) if profile.get("name") else "there"
    places = _or_list(profile["cities"]) if profile["cities"] else ""
    if profile["remote_india"]:
        places = f"{places}, or remote in India" if places else "remote jobs in India"
    alerts = []
    if profile["linkedin"]["enabled"]:
        alerts.append(f"<b>LinkedIn, every hour</b>: new postings that match your skills by {profile['linkedin']['min_match']}% or more.")
    if profile["fast_alert"]["enabled"]:
        alerts.append(f"<b>Fast alert for {html.escape(profile['fast_alert']['city'])}</b>: checked every few minutes, "
                      "so you can apply while a posting is new.")
    if profile["career_sites"]["enabled"]:
        alerts.append("<b>Company career pages, every hour</b>: new openings on companies' own sites that ask for your years of experience.")
    alert_items = "".join(f"<li>{item}</li>" for item in alerts) or "<li>No alert is switched on for you yet.</li>"
    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">
    <h2>Your job alerts are active</h2>
    <p>Hi {name},</p>
    <p>You are now activated on a <b>free trial</b>. It runs for {int(profile['trial_days'])} days and ends on <b>{ends_on}</b>.</p>
    <p>During the trial you will get:</p>
    <ul>{alert_items}</ul>
    <p>The alerts look for these roles: <b>{html.escape(', '.join(profile['roles']))}</b><br>
    Where: <b>{html.escape(places)}</b><br>
    Experience: <b>{profile['experience_years']:g} year(s)</b></p>
    <p>A mail is sent only when there is something new for you, so a quiet hour means nothing matched.</p>
    <p>If any of this is wrong, or you want the alerts stopped, reply to this mail.</p>
    <p style="font-size:11px;color:#666;margin-top:22px">Automated message. After {ends_on} the alerts stop unless your access is extended.</p>
    </body></html>"""


def send_welcome(profile: Dict[str, Any], dry_run: bool = False) -> int:
    """Send the welcome mail when it is due. Exit code 0 when it went out or was not due, 1 when it could not be sent."""
    if not welcome_is_due(profile):
        LOG.info("No welcome mail is due for candidate %s.", profile["id"])
        return 0
    end = trial_end(profile)
    subject = f"Your job alerts are active: free trial until {end.astimezone(ZoneInfo('Asia/Kolkata')).strftime('%d %b %Y')}"
    body = build_welcome_email_html(profile)
    if dry_run:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "welcome_preview.html").write_text(body, encoding="utf-8")
        LOG.info("Dry run: welcome mail %r not sent; preview written to %s", subject, REPORT_DIR)
        return 0
    mailed = send_gmail(subject, body)
    if mailed:
        LOG.info("Welcome mail sent to candidate %s.", profile["id"])
    return _exit_code(mailed)


def load_profile(path: Any) -> Dict[str, Any]:
    """Read and check one candidate's file. The file name is the candidate's id when the file gives none."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"candidate file {path.name} could not be read: {exc}") from exc
    if isinstance(raw, dict) and not raw.get("id"):
        raw["id"] = path.stem.lower()
    try:
        return normalise_profile(raw)
    except ValueError as exc:
        raise ValueError(f"candidate file {path.name}: {exc}") from exc


def _or_list(items: Sequence[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} or {items[-1]}"


def linkedin_places(profile: Dict[str, Any]) -> List[str]:
    """The places one candidate's hourly LinkedIn search covers; "Remote" is remote jobs anywhere in India."""
    return [search_place(city) for city in profile["cities"]] + (["Remote"] if profile["remote_india"] else [])


_BUILT_IN_ALLOWED_TITLES = ALLOWED_TITLE_PATTERNS


def apply_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Make this run a run for one candidate. Returns the checked profile."""
    global PROFILE, PYTHON_RULES, MUST_HAVE, AVOID_STACKS, OWN_TITLE_WORDS, ALLOW_REMOTE_INDIA, SENIOR_TITLES_OK, MAIL_EVERY_RUN
    global ROLE_NOTE, ROLE_SHORT, PLACES_NOTE, CANDIDATE_SKILLS, TARGET_CITIES, SEARCH_TERMS, SEARCH_LOCATIONS
    global ALLOWED_TITLE_PATTERNS, EXCLUDED_TITLE_KEYWORDS, FAST_ALERT_LOCATION, HYDERABAD_NAMES
    profile = normalise_profile(raw)
    years = profile["experience_years"]

    # Skills: the wider vocabulary, plus anything typed by hand as its own words.
    for name, patterns in EXTRA_SKILL_PATTERNS.items():
        SKILL_PATTERNS.setdefault(name, patterns)
    for skill in profile["skills"] + profile["must_have"]:
        SKILL_PATTERNS.setdefault(skill, (_literal_pattern(skill),))
    CANDIDATE_SKILLS = set(profile["skills"])

    # Roles.
    PYTHON_RULES = profile["python_rules"]
    MUST_HAVE = [(name, re.compile("|".join(f"(?:{p})" for p in SKILL_PATTERNS[name]), re.I)) for name in profile["must_have"]]
    OWN_TITLE_WORDS = [pattern for _, pattern in MUST_HAVE]
    AVOID_STACKS = set(profile["avoid_stacks"])
    role_titles = tuple(pattern for pattern in (role_title_pattern(role) for role in profile["roles"]) if pattern)
    # "Staff", "Principal", "Architect" and the like are for people with many years.
    too_senior = () if years >= 8 else _OVER_SENIOR_TITLES
    own_exclusions = tuple(_literal_pattern(word) for word in profile["exclude_title_words"])
    if PYTHON_RULES:
        ALLOWED_TITLE_PATTERNS = _BUILT_IN_ALLOWED_TITLES + role_titles
        EXCLUDED_TITLE_KEYWORDS = _OTHER_FAMILY_TITLES + _NOT_FULL_TIME_TITLES + too_senior + own_exclusions
        ROLE_NOTE = "Only developer roles that use Python are listed (Java, .NET and other stacks are removed)"
        ROLE_SHORT = "a Python developer role"
    else:
        ALLOWED_TITLE_PATTERNS = role_titles
        EXCLUDED_TITLE_KEYWORDS = _NOT_FULL_TIME_TITLES + too_senior + own_exclusions
        ROLE_NOTE = f"Only jobs titled like one of your roles are listed ({', '.join(profile['roles'][:8])})"
        if profile["must_have"]:
            ROLE_NOTE += f", and only when the posting names {_or_list(profile['must_have'])}"
        ROLE_SHORT = "one of your roles"
    SENIOR_TITLES_OK = years >= 5

    # Places.
    cities = profile["cities"]
    TARGET_CITIES = tuple(name for city in cities for name in city_names(city))
    ALLOW_REMOTE_INDIA = profile["remote_india"]
    SEARCH_TERMS = list(profile["roles"])
    SEARCH_LOCATIONS = linkedin_places(profile)
    PLACES_NOTE = (_or_list(cities) + (" (or remote in India)" if ALLOW_REMOTE_INDIA else "")) if cities else "Remote in India"
    if profile["fast_alert"]["city"]:
        FAST_ALERT_LOCATION = profile["fast_alert"]["city"]
        HYDERABAD_NAMES = city_names(FAST_ALERT_LOCATION)

    # Who is told. The addresses live only in the candidate's file and in this process.
    MAIL_EVERY_RUN = profile["linkedin"]["mail_every_run"]
    os.environ["JOB_ALERT_TO"] = ",".join(profile["emails"])
    if profile["ntfy_topic"]:
        os.environ["NTFY_TOPIC"] = profile["ntfy_topic"]
    else:
        os.environ.pop("NTFY_TOPIC", None)
    PROFILE = profile
    LOG.info("Running for candidate %s: %g year(s), %d role(s), %d skill(s), %s.", profile["id"], years, len(profile["roles"]),
             len(profile["skills"]), PLACES_NOTE)
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Scheduled LinkedIn job scraping, skills matching, and GitHub notifications")
    parser.add_argument("--hours", type=float, default=1.0, help="Only consider jobs posted within this many hours (e.g., 1 for 1 hour ago, 24 for 24h)")
    parser.add_argument("--target-count", type=int, default=0, help="Maximum jobs to collect (0 for all fresh / scrape as many as possible)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between search pages in seconds")
    parser.add_argument("--min-match", type=int, default=60, help="Minimum skill match percentage to notify (default 60)")
    parser.add_argument("--source", choices=["auto", "guest", "file"], default="auto",
                        help="Intake source: 'auto' (live guest scraper + file fallback), 'guest' (live scraper), or 'file' (JSON only)")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP/HTTPS/SOCKS proxy URL (or set via LINKEDIN_PROXY / HTTPS_PROXY env)")
    parser.add_argument("--candidate-yoe", type=float, default=2.0, help="Candidate years of experience (default 2.0)")
    parser.add_argument("--keywords", type=str, default=None, help="Optional comma-separated search terms override")
    parser.add_argument("--location", type=str, default=None, help="Optional search locations override, separated by ';'")
    parser.add_argument("--no-jd", action="store_true", help="Skip full JD enrichment (listing cards only)")
    parser.add_argument("--force-notify", action="store_true", help="Force report and notification generation even if jobs were previously seen")
    parser.add_argument("--collect-only", action="store_true",
                        help="Search worker mode: collect jobs for --location, write them to --output, and stop (no report)")
    parser.add_argument("--output", type=str, default="shards/jobs.json", help="File written by --collect-only")
    parser.add_argument("--from-shards", type=str, default=None,
                        help="Report mode: read the search workers' files from this folder instead of searching")
    parser.add_argument("--fast-alert", action="store_true",
                        help="Hyderabad watcher: scan Hyderabad only and notify at once about new openings that are full-time, "
                             "score --fast-min-match or more and state experience that includes --candidate-yoe")
    parser.add_argument("--fast-min-match", type=int, default=FAST_ALERT_MIN_MATCH, help="Skill match needed for a Hyderabad alert (default 70)")
    parser.add_argument("--watch-minutes", type=float, default=0.0,
                        help="With --fast-alert: keep scanning for this many minutes (0 = scan once)")
    parser.add_argument("--interval-minutes", type=float, default=5.0, help="With --fast-alert: minutes from one scan to the next")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --fast-alert: send nothing and keep no state; write the alert to reports/ instead")
    parser.add_argument("--profile", type=str, default=os.environ.get("CANDIDATE_PROFILE", ""),
                        help="One candidate's settings file (JSON). Its skills, roles, cities, years and addresses replace the built-in ones")
    parser.add_argument("--welcome", action="store_true",
                        help="With --profile: send the candidate's welcome mail if their free trial has just started, and stop")
    args = parser.parse_args()

    if args.welcome:
        if not args.profile:
            parser.error("--welcome needs --profile")
        return send_welcome(apply_profile(load_profile(args.profile)), dry_run=args.dry_run)

    if args.profile:
        profile = apply_profile(load_profile(args.profile))
        if trial_is_over(profile):
            LOG.info("The free trial of candidate %s has ended. Nothing to do.", profile["id"])
            return 0
        args.candidate_yoe = profile["experience_years"]
        args.min_match = profile["linkedin"]["min_match"]
        args.fast_min_match = profile["fast_alert"]["min_match"]
        wanted = profile["fast_alert"] if args.fast_alert else profile["linkedin"]
        if not profile["enabled"] or not wanted["enabled"]:
            LOG.info("This scan is switched off for candidate %s. Nothing to do.", profile["id"])
            return 0

    if args.fast_alert:
        return run_fast_alert(args)

    collect_args = dict(
        hours=args.hours,
        target_count=args.target_count,
        delay=args.delay,
        proxy=args.proxy,
        keywords=args.keywords,
        location=args.location,
        no_jd=args.no_jd,
    )
    if args.collect_only:
        searches = [
            (term, loc)
            for loc in _split_places(args.location, SEARCH_LOCATIONS)
            for term in _split_list(args.keywords, SEARCH_TERMS)
        ]
        write_shard(Path(args.output), searches, collect_jobs(source=args.source, **collect_args))
        return 0

    LOG.info("Notification issues will be assigned to: %s", issue_owner() or "nobody (no owner login available)")
    if args.from_shards:
        collected = collect_from_shards(Path(args.from_shards), **collect_args)
    else:
        collected = collect_jobs(source=args.source, **collect_args)

    reasons = {id(job): rejection_reason(job, candidate_yoe=args.candidate_yoe) for job in collected}
    eligible = [job for job in collected if not reasons[id(job)]]
    removed = [
        (job, reasons[id(job)])
        for job in merge_duplicate_postings([j for j in collected if is_role_mismatch_reason(reasons[id(j)])])
    ]
    all_found = merge_duplicate_postings(eligible)
    LOG.info(
        "Collected %d jobs; %d meet location, %.1f-year experience, full-time, title and role-fit eligibility "
        "(%d distinct openings); %d removed as another stack or not a developer role.",
        len(collected), len(eligible), args.candidate_yoe, len(all_found), len(removed),
    )
    for job in all_found:
        score_job(job)
        job["job_url"] = clean_job_url(job)
        job["apply_url"] = job["job_url"]

    # Keep only first-time discoveries, so the same job is not reported repeatedly. An opening counts as
    # already reported when its id, the id of one of its copies, or its fingerprint is in the history.
    seen_ids = load_seen_ids()

    def reported_ids(job: Dict[str, Any]) -> List[str]:
        return [str(job.get("job_id")), *job.get("duplicate_ids", []), SEEN_KEY_PREFIX + str(job.get("posting_key"))]

    new_jobs = [j for j in all_found if not any(x in seen_ids for x in reported_ids(j))]
    LOG.info("Collected %d unique jobs; %d are newly discovered.", len(all_found), len(new_jobs))

    # Report newly discovered jobs; if none are brand-new, report current active eligible jobs in this window
    jobs_to_report = new_jobs if new_jobs else list(all_found)
    run_stamp = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p IST")

    if not jobs_to_report:
        LOG.info("No eligible jobs found in this window. Generating scan status report.")
        if args.force_notify and collected:
            jobs_to_report = list(collected)
        else:
            save_seen_ids(seen_ids)
            if not MAIL_EVERY_RUN:
                return 0  # this candidate is mailed only when there is a new match
            send_push_notification(
                f"LinkedIn Scan at {run_stamp}: 0 Matches",
                f"Scan finished: Evaluated {len(collected)} fresh LinkedIn jobs. No matching roles found in the last {args.hours:g} hour(s).",
                priority="default",
            )
            # Every run sends a mail, so a quiet hour is distinguishable from a run that never happened.
            mailed = send_gmail(
                f"LinkedIn job scan: no eligible jobs | {run_stamp}",
                build_status_email_html(collected, hours=args.hours, candidate_yoe=args.candidate_yoe),
            )
            return _exit_code(mailed)

    jobs_to_report.sort(key=lambda j: (int(j.get("skill_match_pct", 0)), 1 if j.get("is_easy_apply") else 0, str(j.get("title", "")).lower()), reverse=True)
    matched = [j for j in jobs_to_report if int(j.get("skill_match_pct", 0)) >= args.min_match]
    matched.sort(key=lambda j: (int(j.get("skill_match_pct", 0)), 1 if j.get("is_easy_apply") else 0, str(j.get("title", "")).lower()), reverse=True)
    if not MAIL_EVERY_RUN and not (new_jobs and matched):
        # Nothing new that reaches the candidate's match level: remember what was seen and stay quiet.
        seen_ids.update(x for job in new_jobs for x in reported_ids(job) if x != SEEN_KEY_PREFIX + "None")
        save_seen_ids(seen_ids)
        LOG.info("No new job at %d%% or more; no mail this run.", args.min_match)
        return 0
    all_scraped = sorted(jobs_to_report, key=lambda j: (int(j.get("skill_match_pct", 0)), 1 if j.get("is_easy_apply") else 0, str(j.get("title", "")).lower()), reverse=True)

    report_path = create_excel(matched, all_scraped, args.min_match)
    if new_jobs:
        title = f"LinkedIn job matches: {len(matched)} at {args.min_match}%+ | {run_stamp}"
    else:
        title = f"LinkedIn job scan: nothing new, {len(matched)} earlier match(es) still open | {run_stamp}"

    issue_url = ""
    if not new_jobs:
        # Everything in this report was already sent by an earlier run: mail it, but no second issue.
        LOG.info("No new jobs; %d already-reported job(s) are still inside the window. No new GitHub Issue.", len(all_scraped))
        send_push_notification(
            f"LinkedIn Scan at {run_stamp}: nothing new",
            f"{len(all_scraped)} job(s) from earlier runs are still open; no new postings matched.",
            priority="default",
        )
    elif not matched:
        LOG.info("LinkedIn jobs were found, but none reached %d%%. Excel report created; no GitHub Issue.", args.min_match)
        send_push_notification(
            f"LinkedIn Scan at {run_stamp}: 0 Matches at {args.min_match}%+",
            f"Scanned {len(all_scraped)} roles from the last {args.hours:g} hour(s). None reached the {args.min_match}% skill threshold.",
            priority="default",
        )
    else:
        issue_url = create_github_issue(title, build_github_issue_body(matched, all_scraped, args.min_match))
        top_job = matched[0]
        easy_tag = " [⚡ Easy Apply]" if top_job.get("is_easy_apply") else ""
        push_msg = f"{len(matched)} match(es) >={args.min_match}%! Top: {top_job.get('title')} at {top_job.get('company')} ({int(top_job.get('skill_match_pct', 0))}%) in {top_job.get('location')}{easy_tag}"
        send_push_notification(title, push_msg, click_url=issue_url or top_job.get("job_url", ""), priority="high")

    # Send email notification with attached Excel report
    unchecked = [j for j in collected if reasons.get(id(j)) == DESCRIPTION_UNAVAILABLE]
    reason_counts: Dict[str, int] = {}
    for reason in reasons.values():
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    mailed = send_gmail(
        title,
        build_email_html(matched, all_scraped, unchecked, removed, scan_summary_html(reason_counts, args.hours), min_match=args.min_match),
        attachment=report_path,
    )

    for job in jobs_to_report:
        seen_ids.update(x for x in reported_ids(job) if x != SEEN_KEY_PREFIX + "None")
    save_seen_ids(seen_ids)
    LOG.info("Excel report saved: %s", report_path)
    return _exit_code(mailed)


def _exit_code(mailed: bool) -> int:
    """A run whose mail could not be delivered is reported as failed, so it shows red on the Actions tab."""
    if not mailed:
        LOG.error("The scan finished but its email could not be sent; marking this run as failed.")
        return 1
    return 0


def run() -> int:
    """Run main(); if it crashes, send a failure mail before re-raising."""
    try:
        return main()
    except Exception as exc:
        # A crashed run still sends a mail, so a missing hourly mail always means the run never started.
        LOG.exception("Job scan failed.")
        if PROFILE or not MAIL_EVERY_RUN:
            raise  # the failed run shows on the Actions tab; a candidate is never mailed about it
        failed_at = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %I:%M %p IST")
        send_gmail(
            f"LinkedIn job scan FAILED | {failed_at}",
            '<!doctype html><html><body style="font-family:Arial,sans-serif;color:#202124">'
            "<h2>LinkedIn Job Match Monitor: this run failed</h2>"
            f"<p>The scan at <b>{failed_at}</b> stopped with an error, so no jobs were checked this hour.</p>"
            f"<p>Error: <code>{html.escape(repr(exc))}</code></p>"
            "<p>The full log is on the repository's Actions tab.</p></body></html>",
        )
        raise


if __name__ == "__main__":
    raise SystemExit(run())
