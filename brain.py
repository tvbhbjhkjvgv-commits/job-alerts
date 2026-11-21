#!/usr/bin/env python3
"""The "brain": a free AI model reads a job posting and says what it asks of an applicant.

The scans recognise skills from a fixed list (SKILL_PATTERNS in job_match_runner.py). A posting can
name any product or tool, so a model that reads the posting itself finds the skills no list has.
For each posting it returns the skills asked for, the skills that are only preferred, and the years
of experience wanted.

It is optional and free-tier only:
  - It switches on when a key for a free plan is present: GEMINI_API_KEY (Google AI Studio) and/or
    GROQ_API_KEY (Groq). With no key the scans run exactly as before.
  - The models are tried best first. When one has used up its free allowance (HTTP 429) the next one
    is used; when all are used up, or the time allowed is over, the postings left are read by the
    built-in rules alone. A scan never fails or waits long because of the brain.
  - Only the posting's title and text are sent. Nothing about any candidate is.

What the model answers is not trusted blindly: job_match_runner.py keeps a skill only when the
posting's own text contains it, and uses the model's years only when the rules found none.

    python brain.py --check     try every configured model on a sample posting and report
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, List, Sequence, Tuple

try:
    import requests
except ImportError:  # the scans still run, with the rules alone
    requests = None

LOG = logging.getLogger("brain")

# Free plans, best model for this job first. Both speak the same chat-completions format. The model
# lists can be replaced without a code change (repository variables BRAIN_GEMINI_MODELS /
# BRAIN_GROQ_MODELS, comma-separated), because providers retire models often.
#
# "Best" here is measured, not assumed: on 2026-11-21 gemini-3.5-flash-lite read the sample posting
# correctly in under a second and gemini-3.6-flash gave the same answer in 21 seconds, while the two
# newest free models (gemini-4-hydrogen, gemini-3.8-flash) did not answer within 40 seconds. A scan
# has two minutes for all its postings, so the fast model leads and the slow ones are left out.
PROVIDERS: Tuple[Dict[str, Any], ...] = (
    {"name": "Gemini", "key": "GEMINI_API_KEY", "models_env": "BRAIN_GEMINI_MODELS",
     "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "models": ("gemini-3.5-flash-lite", "gemini-3.6-flash")},
    {"name": "Groq", "key": "GROQ_API_KEY", "models_env": "BRAIN_GROQ_MODELS",
     "url": "https://api.groq.com/openai/v1/chat/completions",
     "models": ("openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b")},
)

# Longest one process may spend on the brain (BRAIN_BUDGET_SECONDS), and the most postings it reads.
DEFAULT_BUDGET_SECONDS = 120.0
MAX_POSTINGS_PER_RUN = 40
MAX_POSTING_CHARS = 12000
MAX_OUTPUT_TOKENS = 1500
REQUEST_TIMEOUT = 30
# A per-minute limit that clears within this many seconds is waited out; a longer one means "use the next model".
SHORT_WAIT_SECONDS = 20.0
MAX_SKILLS = 30

INSTRUCTIONS = """You read one job posting and report what it asks of an applicant.
Reply with one JSON object and nothing else, in exactly this shape:
{"required_skills": ["..."], "optional_skills": ["..."], "min_years": null, "max_years": null}

Rules
- A skill is something concrete a person can be trained in: a technology, tool, programming language, framework,
  platform, product, method or certification ("Python", "Power BI", "SAP FICO", "Selenium", "GST filing").
- Write each skill as the posting names it, in 1 to 4 words. Do not explain or expand it.
- Do not list soft skills (communication, teamwork, leadership), degrees, years, job titles, company names,
  or general words that are not a skill on their own (development, testing, analysis, experience).
- required_skills: skills the posting asks for outright.
- optional_skills: skills it marks as preferred, nice to have, good to have, or a plus.
- At most 20 required and 10 optional skills. No skill twice.
- min_years and max_years: the total years of work experience the posting asks for.
  "3-5 years" gives 3 and 5. "5+ years" gives 5 and null. "up to 2 years" gives null and 2.
  Use the figure for the job as a whole, not the figure for one skill. Use null when the posting gives none.
- Never guess. If the posting does not say something, leave it out."""

# Things models list as skills that are not: they would count against every candidate.
NOT_A_SKILL = {
    "communication", "communication skills", "teamwork", "team work", "team player", "leadership", "problem solving",
    "problem-solving", "analytical skills", "analytical thinking", "collaboration", "time management", "interpersonal skills",
    "attention to detail", "agile", "english", "development", "testing", "analysis", "experience", "coding", "programming",
    "software development", "debugging", "documentation", "stakeholder management", "presentation skills", "critical thinking",
}

FOREVER = float("inf")
_UNUSABLE: Dict[Tuple[str, str], float] = {}   # (provider, model or "*") -> unusable until this time.monotonic()
_NO_JSON_MODE: set = set()                      # (provider, model) that refused response_format
_STATE: Dict[str, Any] = {"deadline": None, "read": 0, "models": {}}


def _models(provider: Dict[str, Any]) -> List[str]:
    chosen = [name.strip() for name in os.environ.get(provider["models_env"], "").split(",") if name.strip()]
    return chosen or list(provider["models"])


def brains() -> List[Tuple[Dict[str, Any], str]]:
    """Every (provider, model) that can be asked right now, best first."""
    now = time.monotonic()
    usable = []
    for provider in PROVIDERS:
        if not os.environ.get(provider["key"], "").strip() or _UNUSABLE.get((provider["name"], "*"), 0) > now:
            continue
        usable += [(provider, model) for model in _models(provider) if _UNUSABLE.get((provider["name"], model), 0) <= now]
    return usable


def available() -> bool:
    return requests is not None and bool(brains())


def start_run(budget_seconds: float | None = None) -> None:
    """Begin a scan's allowance: the time it may spend on the brain and the count of postings read."""
    if budget_seconds is None:
        try:
            budget_seconds = float(os.environ.get("BRAIN_BUDGET_SECONDS", "") or DEFAULT_BUDGET_SECONDS)
        except ValueError:
            budget_seconds = DEFAULT_BUDGET_SECONDS
    _STATE.update(deadline=time.monotonic() + max(0.0, budget_seconds), read=0, models={})


def _time_left() -> float:
    if _STATE["deadline"] is None:
        start_run()
    return _STATE["deadline"] - time.monotonic()


def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float) -> Any:
    return requests.post(url, headers=headers, json=body, timeout=timeout)


def _retry_after(headers: Any) -> float | None:
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError, AttributeError):
        return None


def _json_object(text: str) -> Dict[str, Any] | None:
    """The JSON object in a model's reply, also when it wrapped it in a code fence or a sentence."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        answer = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def _skills(value: Any) -> List[str]:
    out: List[str] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, str):
            continue
        name = re.sub(r"\s+", " ", item).strip(" .,;:-")
        if not name or len(name) > 40 or len(name.split()) > 5 or name.lower() in NOT_A_SKILL:
            continue
        if name.lower() not in {existing.lower() for existing in out}:
            out.append(name)
    return out[:MAX_SKILLS]


def _years(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 50 else None


def tidy(answer: Dict[str, Any]) -> Dict[str, Any]:
    """A model's answer in the one shape the scans use; anything malformed is dropped, not repaired."""
    required = _skills(answer.get("required_skills"))
    optional = [name for name in _skills(answer.get("optional_skills")) if name.lower() not in {r.lower() for r in required}]
    low, high = _years(answer.get("min_years")), _years(answer.get("max_years"))
    if low is not None and high is not None and low > high:
        low = high = None
    return {"required": required, "optional": optional, "min_years": low, "max_years": high}


def _ask(provider: Dict[str, Any], model: str, title: str, text: str) -> Tuple[str, Any]:
    """One request. ('ok', answer) | ('next', why) to try another model | ('stop', why) when time is up."""
    name = provider["name"]
    body: Dict[str, Any] = {
        "model": model, "temperature": 0, "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "system", "content": INSTRUCTIONS},
                     {"role": "user", "content": f"Job title: {title}\n\nPosting:\n{text[:MAX_POSTING_CHARS]}"}],
    }
    headers = {"Authorization": f"Bearer {os.environ[provider['key']].strip()}", "Content-Type": "application/json"}
    waited = False
    while True:
        if _time_left() <= 0:
            return "stop", "time allowed for the brain is used up"
        if (name, model) in _NO_JSON_MODE:
            body.pop("response_format", None)
        else:
            body["response_format"] = {"type": "json_object"}
        try:
            response = _post(provider["url"], headers, body, min(REQUEST_TIMEOUT, max(5.0, _time_left())))
        except Exception as exc:  # a network fault with this provider: leave it alone for a while
            _UNUSABLE[(name, model)] = time.monotonic() + 300
            return "next", type(exc).__name__
        status = response.status_code
        if status == 200:
            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                content = ""
            answer = _json_object(str(content or ""))
            return ("ok", tidy(answer)) if answer is not None else ("next", "the reply was not JSON")
        if status == 429:
            wait = _retry_after(response.headers)
            if wait is not None and wait <= SHORT_WAIT_SECONDS and not waited and _time_left() > wait + 5:
                waited = True
                time.sleep(wait + 0.5)
                continue
            # The free allowance for this model is used up for now (a minute, or the day).
            _UNUSABLE[(name, model)] = time.monotonic() + max(wait or 900.0, 60.0)
            return "next", "free allowance used up"
        if status == 400 and "response_format" in body:
            _NO_JSON_MODE.add((name, model))  # this model wants the request without the JSON switch
            continue
        if status in (401, 403):
            _UNUSABLE[(name, "*")] = FOREVER
            LOG.error("Brain: %s refused the key in the %s secret (HTTP %d). Check the key.", name, provider["key"], status)
            return "next", f"HTTP {status}"
        if status in (400, 404):
            _UNUSABLE[(name, model)] = FOREVER
            LOG.warning("Brain: %s does not serve the model %s any more (HTTP %d); update %s.", name, model, status, provider["models_env"])
            return "next", f"HTTP {status}"
        _UNUSABLE[(name, model)] = time.monotonic() + 300
        return "next", f"HTTP {status}"


def read_one(title: str, text: str) -> Dict[str, Any] | None:
    """Ask the best model that still has allowance. None when no model could read the posting."""
    if requests is None or not str(text or "").strip():
        return None
    for provider, model in brains():
        if (provider, model) not in brains():  # switched off by an answer earlier in this same loop (a refused key)
            continue
        outcome, detail = _ask(provider, model, str(title or ""), str(text))
        if outcome == "ok":
            detail["model"] = f"{provider['name']} {model}"
            return detail
        if outcome == "stop":
            return None
    return None


def read_postings(jobs: Sequence[Dict[str, Any]]) -> int:
    """Read each posting that has not been read yet and store the answer as job["brain"]. Returns how many were read."""
    if not available():
        return 0
    done = 0
    for job in jobs:
        if job.get("brain") is not None or not str(job.get("description") or "").strip():
            continue
        if _STATE["read"] >= MAX_POSTINGS_PER_RUN or _time_left() <= 0 or not brains():
            break
        answer = read_one(job.get("title") or "", job["description"])
        if answer is None:
            continue
        job["brain"] = answer
        _STATE["read"] += 1
        _STATE["models"][answer["model"]] = _STATE["models"].get(answer["model"], 0) + 1
        done += 1
    unread = sum(1 for job in jobs if job.get("brain") is None and str(job.get("description") or "").strip())
    if done or unread:
        used = ", ".join(f"{count} by {model}" for model, count in _STATE["models"].items()) or "none"
        LOG.info("Brain: %d posting(s) read this time (so far: %s); %d left to the built-in rules.", done, used, unread)
    return done


SAMPLE_TITLE = "Data Analyst"
SAMPLE_POSTING = """We are hiring a Data Analyst for our Bengaluru office.
Requirements: two to four years of experience in reporting. Strong SQL and Power BI (DAX, Power Query).
Hands-on with Alteryx and Snowflake. Python is good to have. Excellent communication skills."""


def check() -> int:
    """Try every configured model on one sample posting and say what happened."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if requests is None:
        print("The requests library is not installed.")
        return 1
    configured = [provider for provider in PROVIDERS if os.environ.get(provider["key"], "").strip()]
    if not configured:
        print("No brain is configured. Add the GEMINI_API_KEY and/or GROQ_API_KEY secret (both have a free plan).")
        return 1
    start_run(600)
    worked = 0
    for provider in configured:
        for model in _models(provider):
            started = time.monotonic()
            outcome, detail = _ask(provider, model, SAMPLE_TITLE, SAMPLE_POSTING)
            took = time.monotonic() - started
            if outcome == "ok":
                worked += 1
                print(f"OK    {provider['name']} {model} ({took:.1f}s): required {detail['required']}, optional {detail['optional']}, "
                      f"years {detail['min_years']}-{detail['max_years']}")
            else:
                print(f"FAIL  {provider['name']} {model} ({took:.1f}s): {detail}")
    print(f"{worked} model(s) answered.")
    return 0 if worked else 1


if __name__ == "__main__":
    raise SystemExit(check() if "--check" in sys.argv else 0)
