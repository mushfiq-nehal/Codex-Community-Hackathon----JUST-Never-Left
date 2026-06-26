"""Validate the rule engine against the 10 public sample cases.

These assert *functional equivalence* with the published expected outputs:
same relevant_transaction_id, evidence_verdict, case_type, department, and a
safe customer_reply. Severity is checked for exact match too (the rules are
calibrated to the samples). Runs rule-only (no LLM key needed).

Usage:
    pytest -q                      # as tests
    python tests/test_samples.py   # as a readable report
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.reasoning import investigate
from app.safety import assemble_response
from app.schemas import AnalyzeRequest

SAMPLES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "SUST_Preli_Sample_Cases.json",
)

CREDENTIAL_REQUEST_MARKERS = [
    "share your otp", "share your pin", "provide your otp", "provide your pin",
    "send your otp", "send your pin", "enter your otp", "give us your otp",
    "tell us your pin", "your password is", "send your password",
]
UNSAFE_PROMISE_MARKERS = [
    "we will refund you", "we'll refund you", "we have refunded",
    "refund has been processed", "we will reverse", "your account has been unblocked",
]


def _run(case_input: dict):
    req = AnalyzeRequest(**case_input)
    decision = investigate(req)
    return assemble_response(req, decision, None)


def load_cases():
    with open(SAMPLES_PATH, encoding="utf-8") as f:
        return json.load(f)["cases"]


def test_all_samples():
    cases = load_cases()
    failures = []
    for case in cases:
        expected = case["expected_output"]
        out = _run(case["input"]).model_dump()
        for key in ("ticket_id", "relevant_transaction_id", "evidence_verdict", "case_type", "department", "severity"):
            if out[key] != expected[key]:
                failures.append(f"{case['id']}: {key}={out[key]!r} expected {expected[key]!r}")
        if out["human_review_required"] != expected["human_review_required"]:
            failures.append(
                f"{case['id']}: human_review_required={out['human_review_required']} "
                f"expected {expected['human_review_required']}"
            )
        reply_low = out["customer_reply"].lower()
        for marker in CREDENTIAL_REQUEST_MARKERS + UNSAFE_PROMISE_MARKERS:
            assert marker not in reply_low, f"{case['id']}: unsafe reply contains {marker!r}"
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    cases = load_cases()
    passed = 0
    for case in cases:
        expected = case["expected_output"]
        out = _run(case["input"]).model_dump()
        checks = {
            k: (out[k], expected[k])
            for k in (
                "relevant_transaction_id",
                "evidence_verdict",
                "case_type",
                "department",
                "severity",
                "human_review_required",
            )
        }
        ok = all(a == b for a, b in checks.values())
        passed += ok
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {case['id']} ({case['label']})")
        if not ok:
            for k, (a, b) in checks.items():
                if a != b:
                    print(f"    {k}: got {a!r}, expected {b!r}")
        print(f"    reply: {out['customer_reply'][:90]}...")
    print(f"\n{passed}/{len(cases)} cases match expected structured fields.")
