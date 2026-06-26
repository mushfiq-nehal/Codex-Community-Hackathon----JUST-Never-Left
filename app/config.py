"""Runtime configuration and shared enum/taxonomy constants.

Everything here is read lazily and never performs network calls at import time,
so the service module loads instantly and GET /health is ready well within the
60-second readiness window.
"""

from __future__ import annotations

import os

OPENROUTER_API_KEY: str = os.getenv("OPENROUTER_API_KEY", "").strip()
MODEL_NAME: str = os.getenv("MODEL_NAME", "google/gemini-2.0-flash-001").strip()
# Secondary model used only when the primary model is unavailable (rate limited,
# provider 5xx, or timeout). Set to empty to disable the fallback.
FALLBACK_MODEL_NAME: str = os.getenv(
    "FALLBACK_MODEL_NAME", "mistralai/mistral-small-2603"
).strip()
OPENROUTER_BASE_URL: str = os.getenv(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
).strip()

try:
    LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "12"))
except ValueError:
    LLM_TIMEOUT_SECONDS = 12.0

# Overall budget shared across the primary + fallback attempts. Kept safely under
# the 30s grader limit so a slow primary can still leave room for the fallback.
try:
    LLM_TOTAL_TIMEOUT_SECONDS: float = float(
        os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", str(2 * LLM_TIMEOUT_SECONDS + 1.0))
    )
except ValueError:
    LLM_TOTAL_TIMEOUT_SECONDS = 2 * LLM_TIMEOUT_SECONDS + 1.0


def model_chain() -> tuple[str, ...]:
    """Ordered list of models to try: primary first, then the fallback.

    De-duplicates and drops empties so configuration mistakes (e.g. fallback ==
    primary, or an unset fallback) never cause a redundant or empty attempt.
    """
    chain = []
    for name in (MODEL_NAME, FALLBACK_MODEL_NAME):
        name = (name or "").strip()
        if name and name not in chain:
            chain.append(name)
    return tuple(chain)

# Truncate very long complaints before sending to the LLM to keep latency and
# token cost bounded. The deterministic engine still sees the full text.
MAX_COMPLAINT_CHARS_FOR_LLM: int = 4000
MAX_TRANSACTIONS_FOR_LLM: int = 25


def llm_enabled() -> bool:
    """LLM drafting is only attempted when an API key is configured."""
    return bool(OPENROUTER_API_KEY)


# --- Allowed enum values (must match the problem statement EXACTLY) ---

LANGUAGES = ("en", "bn", "mixed")
CHANNELS = ("in_app_chat", "call_center", "email", "merchant_portal", "field_agent")
USER_TYPES = ("customer", "merchant", "agent", "unknown")
TRANSACTION_TYPES = ("transfer", "payment", "cash_in", "cash_out", "settlement", "refund")
TRANSACTION_STATUSES = ("completed", "failed", "pending", "reversed")

EVIDENCE_VERDICTS = ("consistent", "inconsistent", "insufficient_data")

CASE_TYPES = (
    "wrong_transfer",
    "payment_failed",
    "refund_request",
    "duplicate_payment",
    "merchant_settlement_delay",
    "agent_cash_in_issue",
    "phishing_or_social_engineering",
    "other",
)

SEVERITIES = ("low", "medium", "high", "critical")

DEPARTMENTS = (
    "customer_support",
    "dispute_resolution",
    "payments_ops",
    "merchant_operations",
    "agent_operations",
    "fraud_risk",
)

# Default routing of a case_type to a department. user_type can refine this.
CASE_TYPE_TO_DEPARTMENT = {
    "wrong_transfer": "dispute_resolution",
    "payment_failed": "payments_ops",
    "refund_request": "customer_support",
    "duplicate_payment": "payments_ops",
    "merchant_settlement_delay": "merchant_operations",
    "agent_cash_in_issue": "agent_operations",
    "phishing_or_social_engineering": "fraud_risk",
    "other": "customer_support",
}
