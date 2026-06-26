"""Safety guardrails and final response assembly.

This is the last line of defense. Whatever the LLM produced (or if it produced
nothing), the text that leaves the service here is sanitized so it can never:
  - ask the customer for PIN / OTP / password / card number,
  - promise a refund/reversal/unblock the service has no authority to confirm,
  - direct the customer to a suspicious third party.

It also builds safe, deterministic templated text used as a fallback whenever the
LLM is unavailable or its output fails sanitation.
"""

from __future__ import annotations

import re
from typing import Optional

from .reasoning import Decision
from .schemas import AnalyzeRequest, AnalyzeResponse

# --- Constants ---------------------------------------------------------------

_CREDENTIAL_WORDS = ["otp", "pin", "password", "cvv", "card number", "ওটিপি", "পিন", "পাসওয়ার্ড"]
_NEGATION_MARKERS = [
    "do not", "don't", "dont", "never", "won't", "wont", "not share", "without sharing",
    "do not share", "শেয়ার করবেন না", "দেবেন না", "কখনো", "কখনই", "চাইবে না", "চাই না",
]

_PROMISE_PATTERNS = [
    r"we\s+(?:will|'ll|have|'ve|are going to|are about to)\s+(?:refund|reverse|return|reimburse|unblock|recover|credit)\b[^.!?।]*",
    r"you\s+(?:will|'ll)\s+(?:be\s+)?(?:refunded|reimbursed|reversed|credited|paid back)\b[^.!?।]*",
    r"your\s+(?:refund|reversal|money)\s+(?:has|have|is|will)\s+[^.!?।]*",
    r"(?:refund|reversal)\s+(?:is|has been|will be)\s+(?:approved|processed|completed|done)[^.!?।]*",
    r"account\s+(?:has been|is|will be)\s+unblock(?:ed)?[^.!?।]*",
    r"we\s+guarantee[^.!?।]*",
    r"আপনাকে\s+(?:টাকা\s+)?ফেরত\s+(?:দেব|দিব|দিয়ে দেব|করে দেব)[^.!?।]*",
    r"রিফান্ড\s+(?:করে\s+)?দেব[^.!?।]*",
]

_THIRD_PARTY_PATTERNS = [
    r"https?://\S+",
    r"\bwhatsapp\b", r"\btelegram\b", r"\bimo\b", r"\bviber\b", r"\bmessenger\b",
    r"call this number", r"contact this number", r"call back on", r"dial\s+\d",
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?।\n])\s+")

_SAFE_PROMISE_EN = "our team will review your case and any eligible amount will be returned through official channels"
_SAFE_PROMISE_BN = "আমাদের টিম আপনার বিষয়টি পর্যালোচনা করবে এবং প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে"


def credential_note(lang: str) -> str:
    if lang == "bn":
        return "অনুগ্রহ করে কারো সাথে আপনার পিন, ওটিপি বা পাসওয়ার্ড শেয়ার করবেন না।"
    return "Please do not share your PIN, OTP, or password with anyone."


# --- Sanitizers --------------------------------------------------------------


def _contains_credential_request(sentence: str) -> bool:
    low = sentence.lower()
    if not any(w in low for w in _CREDENTIAL_WORDS):
        return False
    # A warning ("do not share your OTP") is allowed; a request is not.
    if any(m in low for m in _NEGATION_MARKERS):
        return False
    return True


def _contains_third_party(sentence: str) -> bool:
    low = sentence.lower()
    return any(re.search(p, low) for p in _THIRD_PARTY_PATTERNS)


def _strip_unsafe_promises(text: str, lang: str) -> str:
    safe = _SAFE_PROMISE_BN if lang == "bn" else _SAFE_PROMISE_EN
    out = text
    for pat in _PROMISE_PATTERNS:
        out = re.sub(pat, safe, out, flags=re.IGNORECASE)
    return out


def sanitize_customer_reply(text: Optional[str], lang: str) -> Optional[str]:
    """Remove credential requests, third-party redirects, and unsafe promises."""
    if not text:
        return None
    text = _strip_unsafe_promises(text, lang)
    sentences = _SENTENCE_SPLIT.split(text.strip())
    kept = [
        s
        for s in sentences
        if s.strip()
        and not _contains_credential_request(s)
        and not _contains_third_party(s)
    ]
    cleaned = " ".join(s.strip() for s in kept).strip()
    if not cleaned:
        return None
    # Always reinforce the credential-safety message.
    note = credential_note(lang)
    if not _has_credential_note(cleaned, lang):
        cleaned = f"{cleaned} {note}".strip()
    return cleaned


def _has_credential_note(text: str, lang: str) -> bool:
    low = text.lower()
    if lang == "bn":
        return ("পিন" in text or "ওটিপি" in text) and ("শেয়ার করবেন না" in text or "দেবেন না" in text)
    return ("pin" in low or "otp" in low) and ("do not share" in low or "don't share" in low or "never share" in low or "not share" in low)


def sanitize_action(text: Optional[str], lang: str) -> Optional[str]:
    """Recommended next action is also checked for unauthorized promises."""
    if not text:
        return None
    cleaned = _strip_unsafe_promises(text, lang).strip()
    return cleaned or None


def sanitize_summary(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return text.strip() or None


# --- Templated fallbacks -----------------------------------------------------


def _fmt_amount(amount: Optional[float]) -> str:
    if amount is None:
        return "the reported amount"
    if float(amount).is_integer():
        return f"{int(amount)} BDT"
    return f"{amount} BDT"


def build_templates(req: AnalyzeRequest, decision: Decision) -> dict:
    """Deterministic, always-safe text for all three free-text fields."""
    ct = decision.case_type
    lang = decision.reply_language
    txn = decision.relevant_transaction_id
    txn_ref = txn or "the reported transaction"
    matched = decision.matched_transaction
    amount = _fmt_amount(matched.amount if matched else None)
    counterparty = (matched.counterparty if matched and matched.counterparty else "the recipient")
    note_en = credential_note("en")
    note_bn = credential_note("bn")

    # Phishing / social engineering
    if ct == "phishing_or_social_engineering":
        summary = (
            "Customer reports a suspicious call/message attempting to obtain "
            "credentials (likely social engineering). No transaction confirmed."
        )
        action = (
            "Escalate to the fraud_risk team. Reassure the customer that the "
            "company never asks for PIN, OTP, or password, and log the reported "
            "contact for fraud pattern analysis."
        )
        reply = (
            "Thank you for reaching out before sharing any information. We never ask "
            "for your PIN, OTP, or password under any circumstances, even if someone "
            "claims to be from us. Our fraud team has been notified. " + note_en
        )
        if lang == "bn":
            reply = (
                "কোনো তথ্য শেয়ার করার আগে আমাদের জানানোর জন্য ধন্যবাদ। আমরা কখনোই "
                "আপনার পিন, ওটিপি বা পাসওয়ার্ড চাই না, এমনকি কেউ আমাদের প্রতিনিধি দাবি "
                "করলেও নয়। আমাদের ফ্রড টিমকে বিষয়টি জানানো হয়েছে। " + note_bn
            )
        return {"agent_summary": summary, "recommended_next_action": action, "customer_reply": reply}

    # Vague / insufficient -> ask for details
    if ct == "other" and decision.evidence_verdict == "insufficient_data":
        summary = (
            "Customer raised a vague concern without specifying a transaction, "
            "amount, or issue. Insufficient detail to identify a relevant transaction."
        )
        action = (
            "Reply to the customer asking for specifics: transaction ID, amount, "
            "what went wrong, and approximate time."
        )
        reply = (
            "Thank you for reaching out. To help you faster, please share the "
            "transaction ID, the amount involved, and a short description of what "
            "went wrong. " + note_en
        )
        if lang == "bn":
            reply = (
                "যোগাযোগ করার জন্য ধন্যবাদ। আপনাকে দ্রুত সহায়তা করতে অনুগ্রহ করে "
                "লেনদেন আইডি, সংশ্লিষ্ট পরিমাণ এবং সমস্যাটি সংক্ষেপে জানান। " + note_bn
            )
        return {"agent_summary": summary, "recommended_next_action": action, "customer_reply": reply}

    # Wrong transfer, ambiguous match -> ask to disambiguate
    if ct == "wrong_transfer" and txn is None:
        summary = (
            "Customer reports a transfer issue, but multiple transactions plausibly "
            "match and the correct one cannot be determined without more detail."
        )
        action = (
            "Ask the customer for the intended recipient's number to identify the "
            "correct transaction. Do not initiate a dispute until confirmed."
        )
        reply = (
            "Thank you for reaching out. We can see more than one transaction that "
            "could match. Could you share the recipient's number so we can identify "
            "the correct transaction? " + note_en
        )
        if lang == "bn":
            reply = (
                "যোগাযোগ করার জন্য ধন্যবাদ। একাধিক লেনদেন মিলে যেতে পারে। সঠিক "
                "লেনদেনটি শনাক্ত করতে অনুগ্রহ করে প্রাপকের নম্বরটি জানান। " + note_bn
            )
        return {"agent_summary": summary, "recommended_next_action": action, "customer_reply": reply}

    # Case-specific defaults
    summaries = {
        "wrong_transfer": (
            f"Customer reports {amount} sent via {txn_ref} to {counterparty} as a "
            f"wrong transfer."
            + (
                " History shows prior transfers to the same recipient, which is "
                "inconsistent with a wrong-transfer claim."
                if decision.evidence_verdict == "inconsistent"
                else ""
            )
        ),
        "payment_failed": (
            f"Customer attempted a {amount} payment ({txn_ref}) that failed but "
            f"reports the balance was deducted. Requires payments operations review."
        ),
        "duplicate_payment": (
            f"Customer reports a duplicate payment of {amount}. {txn_ref} appears to "
            f"be the duplicate charge to {counterparty}."
        ),
        "refund_request": (
            f"Customer requests a refund of {amount} for {txn_ref}. Eligibility "
            f"depends on policy; not a confirmed service failure."
        ),
        "merchant_settlement_delay": (
            f"Merchant reports settlement {txn_ref} of {amount} delayed beyond the "
            f"expected window. Settlement status appears pending."
        ),
        "agent_cash_in_issue": (
            f"Customer reports an agent cash-in of {amount} ({txn_ref}) not reflected "
            f"in their balance. Requires agent operations review."
        ),
        "other": (
            f"Customer reports an issue regarding {txn_ref}. Routed for standard "
            f"support review."
        ),
    }

    actions = {
        "wrong_transfer": (
            f"Verify {txn_ref} with the customer and proceed with the wrong-transfer "
            f"dispute workflow per policy."
            if decision.evidence_verdict == "consistent"
            else f"Flag for human review. Verify with the customer whether {txn_ref} "
            f"was genuinely a wrong transfer given the established recipient pattern."
        ),
        "payment_failed": (
            f"Investigate the ledger status of {txn_ref}. If the balance was deducted "
            f"on a failed payment, initiate the automatic reversal flow within SLA."
        ),
        "duplicate_payment": (
            f"Verify the duplicate with payments_ops. If the biller confirms a single "
            f"charge, initiate reversal of {txn_ref}."
        ),
        "refund_request": (
            "Inform the customer that refund eligibility depends on the merchant's "
            "policy and guide them on contacting the merchant directly."
        ),
        "merchant_settlement_delay": (
            f"Route to merchant_operations to verify the settlement batch status for "
            f"{txn_ref} and communicate a revised ETA if delayed."
        ),
        "agent_cash_in_issue": (
            f"Investigate the pending status of {txn_ref} with agent operations and "
            f"resolve within the standard cash-in SLA."
        ),
        "other": (
            "Review the case and follow up with the customer through official "
            "support channels."
        ),
    }

    replies_en = {
        "wrong_transfer": (
            f"We have noted your concern about transaction {txn_ref}. Our dispute "
            f"team will review the case and contact you through official support "
            f"channels. " + note_en
        ),
        "payment_failed": (
            f"We have noted that transaction {txn_ref} may have caused an unexpected "
            f"balance deduction. Our payments team will review the case and any "
            f"eligible amount will be returned through official channels. " + note_en
        ),
        "duplicate_payment": (
            f"We have noted the possible duplicate payment for transaction {txn_ref}. "
            f"Our payments team will verify with the biller and any eligible amount "
            f"will be returned through official channels. " + note_en
        ),
        "refund_request": (
            "Thank you for reaching out. Refunds for completed merchant payments "
            "depend on the merchant's own policy. We recommend contacting the "
            "merchant directly, and we are happy to guide you through official "
            "support channels. " + note_en
        ),
        "merchant_settlement_delay": (
            f"We have noted your concern about settlement {txn_ref}. Our merchant "
            f"operations team will check the batch status and update you on the "
            f"expected settlement time through official channels."
        ),
        "agent_cash_in_issue": (
            f"We have noted your concern about transaction {txn_ref}. Our agent "
            f"operations team will verify it promptly and update you through official "
            f"channels. " + note_en
        ),
        "other": (
            "Thank you for reaching out. Our support team will review your case and "
            "follow up through official support channels. " + note_en
        ),
    }

    replies_bn = {
        "wrong_transfer": (
            f"আপনার লেনদেন {txn_ref} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের ডিসপিউট টিম "
            f"বিষয়টি পর্যালোচনা করে অফিসিয়াল চ্যানেলের মাধ্যমে আপনার সাথে যোগাযোগ করবে। "
            + note_bn
        ),
        "payment_failed": (
            f"লেনদেন {txn_ref} এর কারণে অপ্রত্যাশিতভাবে ব্যালেন্স কেটে যেতে পারে বলে আমরা "
            f"লক্ষ্য করেছি। আমাদের পেমেন্ট টিম বিষয়টি যাচাই করবে এবং প্রযোজ্য কোনো অর্থ "
            f"অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে। " + note_bn
        ),
        "duplicate_payment": (
            f"লেনদেন {txn_ref} এর সম্ভাব্য দ্বৈত পেমেন্টের বিষয়ে আমরা অবগত হয়েছি। আমাদের "
            f"পেমেন্ট টিম বিলারের সাথে যাচাই করবে এবং প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের "
            f"মাধ্যমে ফেরত দেওয়া হবে। " + note_bn
        ),
        "refund_request": (
            f"যোগাযোগ করার জন্য ধন্যবাদ। সম্পন্ন মার্চেন্ট পেমেন্টের রিফান্ড মার্চেন্টের নিজস্ব "
            f"নীতির উপর নির্ভর করে। আমরা সরাসরি মার্চেন্টের সাথে যোগাযোগের পরামর্শ দিচ্ছি এবং "
            f"অফিসিয়াল চ্যানেলের মাধ্যমে সহায়তা করতে প্রস্তুত। " + note_bn
        ),
        "merchant_settlement_delay": (
            f"সেটেলমেন্ট {txn_ref} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের মার্চেন্ট অপারেশন্স টিম "
            f"ব্যাচ স্ট্যাটাস যাচাই করে অফিসিয়াল চ্যানেলে আপনাকে জানাবে।"
        ),
        "agent_cash_in_issue": (
            f"আপনার লেনদেন {txn_ref} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের এজেন্ট অপারেশন্স দল "
            f"এটি দ্রুত যাচাই করবে এবং অফিসিয়াল চ্যানেলে আপনাকে জানাবে। " + note_bn
        ),
        "other": (
            f"যোগাযোগ করার জন্য ধন্যবাদ। আমাদের সাপোর্ট টিম আপনার বিষয়টি পর্যালোচনা করে "
            f"অফিসিয়াল চ্যানেলের মাধ্যমে আপনার সাথে যোগাযোগ করবে। " + note_bn
        ),
    }

    reply = (replies_bn if lang == "bn" else replies_en).get(ct, replies_en["other"])
    return {
        "agent_summary": summaries.get(ct, summaries["other"]),
        "recommended_next_action": actions.get(ct, actions["other"]),
        "customer_reply": reply,
    }


# --- Final assembly ----------------------------------------------------------


def assemble_response(
    req: AnalyzeRequest, decision: Decision, llm_text: Optional[dict]
) -> AnalyzeResponse:
    """Combine rule-locked decision fields with sanitized text (LLM or template)."""
    lang = decision.reply_language
    templates = build_templates(req, decision)

    summary = templates["agent_summary"]
    action = templates["recommended_next_action"]
    reply = templates["customer_reply"]

    if llm_text:
        llm_summary = sanitize_summary(llm_text.get("agent_summary"))
        llm_action = sanitize_action(llm_text.get("recommended_next_action"), lang)
        llm_reply = sanitize_customer_reply(llm_text.get("customer_reply"), lang)
        if llm_summary:
            summary = llm_summary
        if llm_action:
            action = llm_action
        if llm_reply:
            reply = llm_reply

    # Final safety pass on whatever we settled on (covers template edge cases too).
    action = sanitize_action(action, lang) or templates["recommended_next_action"]
    reply = sanitize_customer_reply(reply, lang) or templates["customer_reply"]

    return AnalyzeResponse(
        ticket_id=req.ticket_id,
        relevant_transaction_id=decision.relevant_transaction_id,
        evidence_verdict=decision.evidence_verdict,
        case_type=decision.case_type,
        severity=decision.severity,
        department=decision.department,
        agent_summary=summary,
        recommended_next_action=action,
        customer_reply=reply,
        human_review_required=decision.human_review_required,
        confidence=round(decision.confidence, 2),
        reason_codes=decision.reason_codes,
    )
