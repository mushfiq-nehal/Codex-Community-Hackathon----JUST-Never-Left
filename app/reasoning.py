"""Deterministic evidence engine.

This module is the source of truth for every *scored decision field*:
relevant_transaction_id, evidence_verdict, case_type, severity, department, and
human_review_required. It runs purely on rules so the service always produces a
valid, internally-consistent answer even when the LLM layer is unavailable.

The LLM later only drafts free-text fields (and may refine an "other"
classification); it never overrides the evidence findings computed here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .config import CASE_TYPE_TO_DEPARTMENT
from .schemas import AnalyzeRequest, TransactionHistoryEntry

_BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_BANGLA_RANGE = re.compile(r"[\u0980-\u09FF]")
# Numbers up to 7 digits; excludes 11-digit phone numbers from being read as amounts.
_NUMBER_RE = re.compile(r"\b\d{1,7}(?:,\d{3})*(?:\.\d+)?\b")


@dataclass
class Decision:
    """The deterministic verdict for a ticket."""

    relevant_transaction_id: Optional[str] = None
    evidence_verdict: str = "insufficient_data"
    case_type: str = "other"
    severity: str = "low"
    department: str = "customer_support"
    human_review_required: bool = False
    confidence: float = 0.6
    reason_codes: List[str] = field(default_factory=list)

    # Context for downstream text drafting (not part of the API response).
    reply_language: str = "en"
    matched_transaction: Optional[TransactionHistoryEntry] = None
    case_type_uncertain: bool = False


# --- Text normalization helpers ---------------------------------------------


def _normalize(text: str) -> str:
    return (text or "").translate(_BN_DIGITS).lower()


def has_bangla(text: str) -> bool:
    return bool(_BANGLA_RANGE.search(text or ""))


def resolve_reply_language(req: AnalyzeRequest) -> str:
    lang = (req.language or "").strip().lower()
    if lang == "bn":
        return "bn"
    if lang == "en":
        return "en"
    # mixed / unknown / missing -> detect from script.
    return "bn" if has_bangla(req.complaint) else "en"


def extract_amounts(text: str) -> List[float]:
    normalized = (text or "").translate(_BN_DIGITS)
    amounts: List[float] = []
    for match in _NUMBER_RE.finditer(normalized):
        raw = match.group(0).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        if value > 0:
            amounts.append(value)
    return amounts


# --- Keyword dictionaries (English + Banglish + Bangla) ----------------------

_KW = {
    "phishing": [
        "otp", "ওটিপি", "pin code", "password", "পাসওয়ার্ড", "scam", "phishing",
        "fraud", "প্রতারণা", "প্রতারক", "suspicious call", "suspicious sms",
        "suspicious message", "asked for my otp", "asking for my otp",
        "asked for otp", "asking for otp", "asked for my pin", "claiming to be",
        "claim to be", "someone called", "someone messaged", "spam call",
        "verify my account", "account will be blocked", "share my otp",
        "share the otp", "ভেরিফাই", "সন্দেহজনক", "ব্লক", "হ্যাক",
    ],
    "duplicate_payment": [
        "twice", "two times", "double", "duplicate", "deducted twice",
        "charged twice", "paid twice", "deducted two times", "দুইবার", "দুবার",
        "double charge", "double deduct",
    ],
    "payment_failed": [
        "failed", "ব্যর্থ", "transaction failed", "payment failed", "showed failed",
        "but balance was deducted", "balance deducted", "money deducted",
        "deducted but", "recharge failed", "টাকা কেটে", "কেটে নিয়েছে",
        "কেটে নিল", "failed but",
    ],
    "agent_cash_in_issue": [
        "agent", "এজেন্ট", "cash in", "cash-in", "cashin", "ক্যাশ ইন", "ক্যাশইন",
        "deposit", "জমা",
    ],
    "merchant_settlement_delay": [
        "settlement", "settle", "settled", "নিষ্পত্তি", "merchant settlement",
        "payout", "disbursement",
    ],
    "wrong_transfer": [
        "wrong number", "wrong person", "wrong recipient", "wrong account",
        "ভুল নম্বর", "ভুল মানুষ", "ভুল", "vul number", "bhul number", "vul", "bhul",
        "mistakenly sent", "sent by mistake", "sent to the wrong", "wrong transfer",
        "reverse it", "reverse", "didn't get it", "did not get it", "didn't receive",
        "did not receive", "not received", "hasn't received", "পাইনি", "পায়নি",
        "পাঠিয়েছি কিন্তু",
    ],
    "refund_request": [
        "refund", "ফেরত", "return my money", "money back", "want my money back",
        "changed my mind", "change my mind", "don't want", "do not want",
        "cancel my order", "cancel the order",
    ],
}


def _contains_any(haystack: str, needles: List[str]) -> bool:
    return any(n in haystack for n in needles)


def classify_case_type(req: AnalyzeRequest, history: List[TransactionHistoryEntry]) -> tuple[str, bool]:
    """Return (case_type, uncertain). Order encodes precedence (safety first)."""
    text = _normalize(req.complaint)

    if _contains_any(text, _KW["phishing"]):
        return "phishing_or_social_engineering", False

    duplicate_in_data = _detect_duplicate_group(history) is not None
    if _contains_any(text, _KW["duplicate_payment"]) or (
        duplicate_in_data and _contains_any(text, ["bill", "paid", "payment", "বিল", "পেমেন্ট"])
    ):
        return "duplicate_payment", False

    if _contains_any(text, _KW["payment_failed"]):
        return "payment_failed", False

    # Agent cash-in: needs an agent/deposit signal together with cash-in context.
    if _contains_any(text, ["এজেন্ট", "agent"]) and _contains_any(
        text, ["cash in", "cash-in", "cashin", "ক্যাশ ইন", "ক্যাশইন", "deposit", "জমা"]
    ):
        return "agent_cash_in_issue", False

    if _contains_any(text, _KW["merchant_settlement_delay"]):
        return "merchant_settlement_delay", False

    if _contains_any(text, _KW["wrong_transfer"]):
        return "wrong_transfer", False

    if _contains_any(text, _KW["refund_request"]):
        return "refund_request", False

    # Nothing matched: it's "other", but flag as uncertain so the LLM may refine.
    return "other", True


# --- Transaction matching ----------------------------------------------------

_EXPECTED_TYPES = {
    "wrong_transfer": {"transfer"},
    "payment_failed": {"payment"},
    "duplicate_payment": {"payment"},
    "agent_cash_in_issue": {"cash_in"},
    "merchant_settlement_delay": {"settlement"},
    "refund_request": {"payment", "transfer", "refund"},
}


def _detect_duplicate_group(
    history: List[TransactionHistoryEntry],
) -> Optional[List[TransactionHistoryEntry]]:
    """Find >=2 payments with identical amount+counterparty (likely duplicates)."""
    groups: dict[tuple, List[TransactionHistoryEntry]] = {}
    for t in history:
        if t.amount is None:
            continue
        key = (t.type, t.amount, t.counterparty)
        groups.setdefault(key, []).append(t)
    for key, items in groups.items():
        type_ = key[0]
        if type_ in ("payment", "transfer") and len(items) >= 2:
            return items
    return None


def _sort_by_time(items: List[TransactionHistoryEntry]) -> List[TransactionHistoryEntry]:
    return sorted(items, key=lambda t: t.timestamp or "")


def match_transaction(
    case_type: str,
    history: List[TransactionHistoryEntry],
    amounts: List[float],
) -> tuple[Optional[TransactionHistoryEntry], bool]:
    """Return (matched_transaction, ambiguous).

    ambiguous=True means several transactions plausibly match and we should NOT
    guess (-> insufficient_data, relevant_transaction_id=null).
    """
    if not history:
        return None, False

    # Phishing is about a call/message, not a transaction in the ledger.
    if case_type == "phishing_or_social_engineering":
        return None, False

    if case_type == "duplicate_payment":
        dup = _detect_duplicate_group(history)
        if dup:
            # The later transaction is the suspected duplicate.
            return _sort_by_time(dup)[-1], False

    amount_set = set(amounts)
    amount_candidates = [t for t in history if t.amount is not None and t.amount in amount_set]

    expected = _EXPECTED_TYPES.get(case_type)
    if expected:
        typed = [t for t in amount_candidates if t.type in expected]
        pool = typed if typed else amount_candidates
    else:
        pool = amount_candidates

    if not pool:
        # No amount in the complaint matched any transaction.
        if not amounts and case_type not in ("other",):
            # No amount mentioned but a single relevant-typed transaction exists.
            typed_all = (
                [t for t in history if t.type in expected] if expected else list(history)
            )
            if len(typed_all) == 1:
                return typed_all[0], False
        return None, False

    if len(pool) == 1:
        return pool[0], False

    # Several plausible matches of the same amount -> ambiguous, do not guess.
    return None, True


# --- Verdict / severity / routing -------------------------------------------


def _established_recipient(
    matched: TransactionHistoryEntry, history: List[TransactionHistoryEntry]
) -> bool:
    prior = [
        t
        for t in history
        if t.type == "transfer"
        and t.counterparty == matched.counterparty
        and t.transaction_id != matched.transaction_id
    ]
    return len(prior) >= 1


def investigate(req: AnalyzeRequest) -> Decision:
    """Run the full deterministic investigation for one ticket."""
    history = list(req.transaction_history or [])
    amounts = extract_amounts(req.complaint)
    reply_language = resolve_reply_language(req)

    case_type, uncertain = classify_case_type(req, history)
    matched, ambiguous = match_transaction(case_type, history, amounts)

    decision = Decision(
        case_type=case_type,
        case_type_uncertain=uncertain,
        reply_language=reply_language,
        matched_transaction=matched,
    )

    decision.relevant_transaction_id = (
        matched.transaction_id if matched is not None else None
    )

    # --- evidence_verdict ---
    if matched is None:
        decision.evidence_verdict = "insufficient_data"
    else:
        decision.evidence_verdict = "consistent"
        if case_type == "wrong_transfer" and _established_recipient(matched, history):
            decision.evidence_verdict = "inconsistent"

    # --- severity ---
    if case_type == "phishing_or_social_engineering":
        decision.severity = "critical"
    elif case_type == "wrong_transfer":
        decision.severity = (
            "high"
            if (matched is not None and decision.evidence_verdict == "consistent")
            else "medium"
        )
    elif case_type in ("payment_failed", "duplicate_payment", "agent_cash_in_issue"):
        decision.severity = "high"
    elif case_type == "merchant_settlement_delay":
        decision.severity = "medium"
    else:  # refund_request, other
        decision.severity = "low"

    # --- department ---
    decision.department = CASE_TYPE_TO_DEPARTMENT.get(case_type, "customer_support")
    # A merchant reporting a settlement issue stays in merchant_operations; an
    # agent-side report routes to agent_operations regardless of case wording.
    if (req.user_type or "").lower() == "agent" and case_type == "agent_cash_in_issue":
        decision.department = "agent_operations"

    # --- human_review_required ---
    if case_type == "phishing_or_social_engineering":
        decision.human_review_required = True
    elif case_type in ("wrong_transfer", "duplicate_payment", "agent_cash_in_issue"):
        decision.human_review_required = decision.relevant_transaction_id is not None
    elif decision.evidence_verdict == "inconsistent":
        decision.human_review_required = True
    else:
        decision.human_review_required = False

    # --- confidence ---
    if case_type == "phishing_or_social_engineering":
        decision.confidence = 0.95
    elif decision.evidence_verdict == "consistent" and matched is not None:
        decision.confidence = 0.9
    elif decision.evidence_verdict == "inconsistent":
        decision.confidence = 0.75
    else:
        decision.confidence = 0.62

    # --- reason_codes ---
    decision.reason_codes = _build_reason_codes(decision, ambiguous)

    return decision


def _build_reason_codes(decision: Decision, ambiguous: bool) -> List[str]:
    codes: List[str] = [decision.case_type]
    if decision.relevant_transaction_id is not None:
        codes.append("transaction_match")
    if decision.evidence_verdict == "inconsistent":
        codes.append("evidence_inconsistent")
    if decision.evidence_verdict == "insufficient_data":
        codes.append("needs_clarification" if ambiguous else "insufficient_evidence")
    if ambiguous:
        codes.append("ambiguous_match")
    if decision.human_review_required:
        codes.append("human_review")
    # De-duplicate while keeping order.
    seen = set()
    unique = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique
