"""Offline checks of the AI reader of postings (brain.py) and of how the scans use what it says.

Usage: python tests/test_brain.py
No model is contacted: the one function that sends a request is replaced by a recorder.
"""
import io
import json
import os
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import brain  # noqa: E402
import job_match_runner as core  # noqa: E402

failures = []


def check(name, condition, detail=""):
    print(("PASS " if condition else "FAIL ") + name + (f" | {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


class Reply:
    def __init__(self, status, answer=None, headers=None, text=None):
        self.status_code, self.headers = status, headers or {}
        self._content = text if text is not None else json.dumps(answer or {})

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


calls = []
script = {}  # model -> list of replies, handed out in order (the last one repeats)


def fake_post(url, headers, body, timeout):
    calls.append({"url": url, "model": body["model"], "key": headers["Authorization"], "json_mode": "response_format" in body,
                  "user": body["messages"][1]["content"]})
    replies = script.get(body["model"]) or [Reply(500)]
    return replies.pop(0) if len(replies) > 1 else replies[0]


def reset(**keys):
    calls.clear()
    script.clear()
    brain._UNUSABLE.clear()
    brain._NO_JSON_MODE.clear()
    for name in ("GEMINI_API_KEY", "GROQ_API_KEY", "BRAIN_GEMINI_MODELS", "BRAIN_GROQ_MODELS", "BRAIN_BUDGET_SECONDS"):
        os.environ.pop(name, None)
    os.environ.update(keys)
    brain.start_run(60)


brain._post = fake_post
brain.time.sleep = lambda seconds: None
GOOD = {"required_skills": ["SQL", "Power BI", "Alteryx", "Communication", "sql"], "optional_skills": ["Python", "Power BI"],
        "min_years": 2, "max_years": 4}
POSTING = "Two to four years in reporting. Strong SQL and Power BI. Hands-on with Alteryx. Python is good to have."

# --- 1. off unless a key is set -----------------------------------------------------------------------
reset()
job = {"title": "Data Analyst", "description": POSTING}
check("no key: the brain is off, nothing is sent, the posting is untouched",
      not brain.available() and brain.read_postings([job]) == 0 and calls == [] and "brain" not in job)

# --- 2. the best model first; the answer tidied -------------------------------------------------------
reset(GEMINI_API_KEY="g-key", GROQ_API_KEY="q-key")
script["gemini-4-hydrogen"] = [Reply(200, GOOD)]
answer = brain.read_one("Data Analyst", POSTING)
check("the first model of the first provider is asked, with that provider's key",
      len(calls) == 1 and calls[0]["model"] == "gemini-4-hydrogen" and calls[0]["key"] == "Bearer g-key"
      and "generativelanguage.googleapis.com" in calls[0]["url"] and calls[0]["json_mode"], str(calls))
check("only the posting is sent", calls[0]["user"] == f"Job title: Data Analyst\n\nPosting:\n{POSTING}")
check("the answer is tidied: no soft skill, no repeat, optional never repeats required",
      answer == {"required": ["SQL", "Power BI", "Alteryx"], "optional": ["Python"], "min_years": 2.0, "max_years": 4.0,
                 "model": "Gemini gemini-4-hydrogen"}, str(answer))

# --- 3. when a model has used up its free allowance ---------------------------------------------------
reset(GEMINI_API_KEY="g-key", GROQ_API_KEY="q-key")
for model in ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash"):
    script[model] = [Reply(429)]
script["gemini-3.5-flash-lite"] = [Reply(200, GOOD)]
first = brain.read_one("Data Analyst", POSTING)
asked_first = [c["model"] for c in calls]
calls.clear()
second = brain.read_one("Data Analyst", POSTING)
check("a used-up model is skipped and the next one answers",
      asked_first == ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
      and first["model"] == "Gemini gemini-3.5-flash-lite", str(asked_first))
check("...and is not asked again for a while", [c["model"] for c in calls] == ["gemini-3.5-flash-lite"] and second is not None, str(calls))

reset(GROQ_API_KEY="q-key")
script["openai/gpt-oss-120b"] = [Reply(429, headers={"retry-after": "3"}), Reply(200, GOOD)]
answer = brain.read_one("Data Analyst", POSTING)
check("a limit that clears in seconds is waited out once", [c["model"] for c in calls] == ["openai/gpt-oss-120b"] * 2
      and answer["model"] == "Groq openai/gpt-oss-120b" and "api.groq.com" in calls[0]["url"], str(calls))

reset(GEMINI_API_KEY="g-key", GROQ_API_KEY="q-key")
for provider in brain.PROVIDERS:
    for model in provider["models"]:
        script[model] = [Reply(429)]
check("every model used up: no answer, and the scan carries on", brain.read_one("Data Analyst", POSTING) is None and not brain.available())

# --- 4. keys, retired models, odd replies -------------------------------------------------------------
reset(GEMINI_API_KEY="bad", GROQ_API_KEY="q-key")
script["gemini-4-hydrogen"] = [Reply(401)]
script["openai/gpt-oss-120b"] = [Reply(200, GOOD)]
answer = brain.read_one("Data Analyst", POSTING)
check("a refused key switches that provider off, and the other one answers",
      [c["model"] for c in calls] == ["gemini-4-hydrogen", "openai/gpt-oss-120b"] and answer["model"].startswith("Groq")
      and all(provider["name"] == "Groq" for provider, _ in brain.brains()), str(calls))

reset(GROQ_API_KEY="q-key")
script["openai/gpt-oss-120b"] = [Reply(400), Reply(404)]
script["qwen/qwen3.8-27b"] = [Reply(200, text='Here it is:\n```json\n{"required_skills": ["SQL"], "min_years": 2}\n```')]
answer = brain.read_one("Data Analyst", POSTING)
check("a model that refuses the JSON switch is asked again without it; a retired model is passed over",
      [(c["model"], c["json_mode"]) for c in calls] == [("openai/gpt-oss-120b", True), ("openai/gpt-oss-120b", False), ("qwen/qwen3.8-27b", True)], str(calls))
check("JSON wrapped in a sentence and a code fence is still read", answer["required"] == ["SQL"] and answer["min_years"] == 2.0 and answer["max_years"] is None)

reset(GROQ_API_KEY="q-key", BRAIN_GROQ_MODELS="model-a, model-b")
script["model-a"] = [Reply(200, text="I cannot help with that.")]
script["model-b"] = [Reply(200, {"required_skills": "SQL", "min_years": 9, "max_years": 3, "optional_skills": [{"x": 1}, "A" * 60]})]
answer = brain.read_one("Data Analyst", POSTING)
check("the model list can be replaced from outside, and a reply that is not JSON moves on to the next model",
      [c["model"] for c in calls] == ["model-a", "model-b"], str(calls))
check("a malformed answer is dropped, not repaired", answer["required"] == [] and answer["optional"] == [] and answer["min_years"] is None and answer["max_years"] is None, str(answer))

reset(GROQ_API_KEY="q-key")
brain.start_run(0)
check("no time left: nothing is sent", brain.read_one("Data Analyst", POSTING) is None and calls == [])

reset(GROQ_API_KEY="q-key")
script["openai/gpt-oss-120b"] = [Reply(200, GOOD)]
brain.MAX_POSTINGS_PER_RUN = 2
batch = [{"title": "Data Analyst", "description": POSTING} for _ in range(4)] + [{"title": "x", "description": ""}]
read = brain.read_postings(batch)
brain.MAX_POSTINGS_PER_RUN = 40
check("a run reads no more postings than its limit, and skips one with no text", read == 2 and sum("brain" in job for job in batch) == 2 and len(calls) == 2)

# --- 5. how the scans use the answer ------------------------------------------------------------------
PROFILE = {"id": "ana01", "emails": ["a@example.com"], "experience_years": 2, "roles": ["Data Analyst"], "cities": ["Bengaluru"],
           "skills": ["sql", "power bi", "excel", "kinaxis"]}
core.apply_profile(PROFILE)


def posting(text, **answer):
    return {"job_id": "1", "source": "LinkedIn", "title": "Data Analyst", "company": "Acme", "location": "Bengaluru, India",
            "description": text, "job_url": "https://www.linkedin.com/jobs/view/1/", "employment_type": "Full-time",
            **({"brain": answer} if answer else {})}


TEXT = "Data Analyst, two to three years. Strong SQL and Power BI. Must know Kinaxis RapidResponse and Workfront Fusion."
plain = core.score_job(posting(TEXT))
smart = core.score_job(posting(TEXT, required=["SQL", "Power BI", "Kinaxis RapidResponse", "Workfront Fusion", "Tableau"], optional=[], min_years=7, max_years=9))
check("without the brain only the listed skills and the ones typed for the candidate are seen", plain["job_skills"] == ["kinaxis", "power bi", "sql"], str(plain["job_skills"]))
check("with it, a skill no list has is counted, here one the candidate lacks",
      smart["job_skills"] == ["kinaxis", "power bi", "sql", "workfront fusion"] and smart["matched_skills"] == ["kinaxis", "power bi", "sql"]
      and smart["missing_skills"] == ["workfront fusion"], f"{smart['job_skills']} {smart['matched_skills']} {smart['missing_skills']}")
check("a skill the model names but the posting never mentions is not counted", "tableau" not in smart["job_skills"])
check("the rules' own reading of the years stands; the model's is not used over it", core.parse_experience_requirement(smart) == (2.0, 3.0))

quiet = posting("Data Analyst for our reporting team. Experience level wanted: 3. SQL needed.", required=["SQL"], optional=[], min_years=3, max_years=None)
check("where the rules read no years the model's figure fills in, when the text holds it", core.parse_experience_requirement(quiet) == (3.0, None),
      str(core.parse_experience_requirement(quiet)))
invented = posting("Data Analyst for our reporting team. SQL needed.", required=["SQL"], optional=[], min_years=5, max_years=None)
check("a figure the text does not hold is not believed", core.parse_experience_requirement(invented) == (None, None))
history = "Data Analyst. SQL needed. We have 20 years of experience in the logistics industry."
check("(the rules set the employer's own history aside)", core.parse_experience_requirement(posting(history)) == (None, None))
extra = posting(history, required=["SQL"], optional=[], min_years=20, max_years=None)
check("years the rules deliberately set aside stay set aside", core.parse_experience_requirement(extra) == (None, None), str(core.parse_experience_requirement(extra)))

reset(GROQ_API_KEY="q-key")
script["openai/gpt-oss-120b"] = [Reply(200, {"required_skills": ["SQL", "Workfront Fusion"], "min_years": None, "max_years": None})]
right = posting(TEXT)
wrong_place = {**posting(TEXT), "location": "Pune, India"}
wrong_title = {**posting(TEXT), "title": "Sales Executive"}
too_senior = posting(TEXT.replace("two to three years", "8+ years of experience"))
core.read_with_brain([right, wrong_place, wrong_title, too_senior], 2.0)
check("only postings still in the running are sent to the model", len(calls) == 1 and "brain" in right
      and not any("brain" in job for job in (wrong_place, wrong_title, too_senior)), str(len(calls)))
check("a posting is read once", core.read_with_brain([right], 2.0) is None and len(calls) == 1)

print()
if failures:
    print(f"{len(failures)} FAILED: " + "; ".join(failures))
    sys.exit(1)
print("ALL PASS")
