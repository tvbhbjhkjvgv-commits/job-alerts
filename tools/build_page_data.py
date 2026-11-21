#!/usr/bin/env python3
"""Write docs/data.js: what the settings page needs to know from the code.

The page reads a resume in the browser and offers the same skills, cities, job families and
companies the scans use, so they are written out from the code instead of being typed twice.
Run this after changing the skill vocabulary, the cities or the list of career sites:

    python tools/build_page_data.py          write docs/data.js
    python tools/build_page_data.py --check  exit 1 when docs/data.js is out of date
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import company_portals as portals  # noqa: E402
import job_match_runner as core  # noqa: E402

# Roles the page can suggest from a resume. "title" finds the role named outright; "signals" are
# skills that point to it. Both are JavaScript regular expressions (used without flags other than i).
ROLES = [
    ("Python Developer", r"python\s+(?:developer|engineer|programmer)", r"\b(?:python|django|fastapi|flask)\b"),
    ("AI Engineer", r"\b(?:ai|a\.i\.|artificial intelligence)\s+(?:engineer|developer)", r"\b(?:llm|langchain|rag|openai|gen(?:erative)?\s?ai|prompt engineering|ai agents?)\b"),
    ("Generative AI Engineer", r"gen(?:erative)?\s?ai\s+(?:engineer|developer)", r"\b(?:llm|langchain|langgraph|rag|gen(?:erative)?\s?ai)\b"),
    ("Machine Learning Engineer", r"(?:machine learning|\bml)\s+(?:engineer|developer)", r"\b(?:machine learning|deep learning|pytorch|tensorflow|scikit[- ]learn|nlp)\b"),
    ("Data Scientist", r"data\s+scientist", r"\b(?:machine learning|statistics|pandas|scikit[- ]learn|deep learning)\b"),
    ("Data Analyst", r"data\s+analyst", r"\b(?:power\s?bi|tableau|excel|sql|dax)\b"),
    ("Data Engineer", r"data\s+engineer", r"\b(?:spark|pyspark|airflow|etl|databricks|snowflake|kafka)\b"),
    ("Full Stack Developer", r"full[- ]?stack\s+(?:developer|engineer)", r"\b(?:react|angular|vue|node(?:\.js)?|django|spring\s?boot)\b"),
    ("Backend Developer", r"back[- ]?end\s+(?:developer|engineer)", r"\b(?:rest(?:ful)? apis?|microservices|django|fastapi|spring\s?boot|node(?:\.js)?)\b"),
    ("Frontend Developer", r"front[- ]?end\s+(?:developer|engineer)|ui\s+developer", r"\b(?:react|angular|vue|html5?|css3?|javascript|typescript)\b"),
    ("Software Engineer", r"software\s+(?:development\s+)?(?:engineer|developer)|\bsde\b", r"\b(?:data structures|algorithms|git|rest(?:ful)? apis?)\b"),
    ("Java Developer", r"java\s+(?:developer|engineer|programmer)", r"\b(?:java|spring\s?boot|hibernate|j2ee|maven)\b"),
    (".NET Developer", r"(?:\.net|dot\s?net)\s+(?:developer|engineer)", r"(?:\.net\b|\basp\.net\b|c#|\bentity framework\b)"),
    ("React Developer", r"react(?:\.?js)?\s+(?:developer|engineer)", r"\b(?:react|redux|next\.?js)\b"),
    ("Node.js Developer", r"node(?:\.?js)?\s+(?:developer|engineer)", r"\b(?:node(?:\.js)?|express(?:\.js)?|nest\.?js)\b"),
    ("Android Developer", r"android\s+(?:developer|engineer)", r"\b(?:android|kotlin)\b"),
    ("iOS Developer", r"ios\s+(?:developer|engineer)", r"\b(?:ios|swift(?:ui)?)\b"),
    ("DevOps Engineer", r"dev[- ]?ops\s+engineer|site reliability", r"\b(?:kubernetes|terraform|ansible|jenkins|ci\s?/\s?cd|docker|helm)\b"),
    ("Cloud Engineer", r"cloud\s+(?:engineer|architect)", r"\b(?:aws|azure|gcp|terraform|kubernetes)\b"),
    ("QA Engineer", r"\b(?:qa|quality assurance|test)\s+(?:engineer|analyst)|\btester\b|\bsdet\b", r"\b(?:selenium|manual testing|test cases|cypress|playwright|jmeter|testng|appium)\b"),
    ("Automation Test Engineer", r"automation\s+(?:test\s+)?engineer|\bsdet\b", r"\b(?:selenium|cypress|playwright|appium|testng|cucumber)\b"),
    ("Business Analyst", r"business\s+analyst", r"\b(?:requirements? gathering|user stories|brd|stakeholders?)\b"),
    ("SAP Consultant", r"sap\s+\w+\s+consultant|sap\s+consultant", r"\b(?:sap|abap|fico|hana)\b"),
    ("Salesforce Developer", r"salesforce\s+(?:developer|admin\w*|consultant)", r"\b(?:salesforce|apex|lightning)\b"),
    ("Embedded Engineer", r"embedded\s+(?:software\s+)?(?:engineer|developer)|firmware\s+engineer", r"\b(?:embedded|firmware|rtos|microcontrollers?)\b"),
    ("Support Engineer", r"(?:technical|application|production)\s+support\s+engineer", r"\b(?:incident management|itil|service\s?now|l[12]\s+support)\b"),
]


def page_data() -> dict:
    skills = {**core.EXTRA_SKILL_PATTERNS, **core.SKILL_PATTERNS}
    return {
        "skills": {name: list(skills[name]) for name in sorted(skills)},
        "stacks": core.avoidable_stacks(),
        "cities": {name.title(): list(names) for name, names in core.CITY_ALIASES.items()},
        "companies": {key: name for key, (name, _reader) in portals.PORTALS.items()},
        "roles": [{"name": name, "title": title, "signals": signals} for name, title, signals in ROLES],
        "defaults": {"linkedin": 60, "fast_alert": core.FAST_ALERT_MIN_MATCH, "career_sites": 60, "trial_days": core.TRIAL_DAYS},
    }


def render() -> str:
    body = json.dumps(page_data(), indent=1, ensure_ascii=True)
    return (
        "// Written by tools/build_page_data.py from the scan code. Do not edit by hand.\n"
        "(function (root, data) {\n"
        '  if (typeof module === "object" && module.exports) module.exports = data;\n'
        "  else root.JOB_ALERTS_DATA = data;\n"
        "})(this, " + body + ");\n"
    )


def main() -> int:
    target = ROOT / "docs" / "data.js"
    text = render()
    if "--check" in sys.argv:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current.replace("\r\n", "\n") != text:
            print("docs/data.js is out of date: run python tools/build_page_data.py")
            return 1
        print("docs/data.js is up to date.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
