"""Check the experience rule against 200 hand-written postings in tests/experience_cases/.

Usage: python tests/test_experience.py [<repo_dir>]   (default: the folder above tests/)

Each case is a title and description with the experience range it states and whether a candidate
with 2.0 years is eligible. Every case was written by one author and checked by a second one
(the *.audit.json files). A case counts when both agree and neither called it ambiguous; all of
those must pass. The few ambiguous ones are reported but do not fail the run.
"""
import glob
import importlib.util
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
repo_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(here)
spec = importlib.util.spec_from_file_location("jmr", os.path.join(repo_dir, "job_match_runner.py"))
jmr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(jmr)


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) < 0.06


agreed = [0, 0]      # cases, passed
ambiguous = [0, 0]
failures = []
for path in sorted(glob.glob(os.path.join(here, "experience_cases", "*.json"))):
    if path.endswith(".audit.json"):
        continue
    audit_path = path.replace(".json", ".audit.json")
    audits = {a["id"]: a for a in json.load(open(audit_path, encoding="utf-8"))} if os.path.exists(audit_path) else {}
    for case in json.load(open(path, encoding="utf-8")):
        audit = audits.get(case["id"])
        clear = bool(audit) and audit.get("agree") and not audit.get("ambiguous") and not case.get("ambiguous")
        job = {"title": case["title"], "description": case["description"]}
        lo, hi = jmr.parse_experience_requirement(dict(job))
        eligible = jmr.experience_is_eligible(dict(job), candidate_yoe=2.0)
        ok = eligible == case["expected_eligible"] and same(lo, case["expected_min"]) and same(hi, case["expected_max"])
        bucket = agreed if clear else ambiguous
        bucket[0] += 1
        bucket[1] += ok
        if clear and not ok:
            failures.append(case["id"])
            print(f"FAIL {case['id']}: expected {case['expected_min']}-{case['expected_max']} eligible={case['expected_eligible']}, "
                  f"got {lo}-{hi} eligible={eligible}\n     title: {case['title']!r}\n     text : {case['description'][:300]!r}")

print(f"clear cases    : {agreed[1]}/{agreed[0]} pass")
print(f"ambiguous cases: {ambiguous[1]}/{ambiguous[0]} pass (reported only)")
print("ALL PASS" if not failures and agreed[0] else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures or not agreed[0] else 0)
