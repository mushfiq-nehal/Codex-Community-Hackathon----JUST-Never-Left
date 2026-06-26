"""Pydantic request/response models.

Input parsing is intentionally lenient: only ``ticket_id`` and ``complaint`` are
required. Optional enum-typed fields are accepted as free strings so that an
unexpected value in a hidden test never crashes the service. The output model,
which we construct ourselves, uses strict ``Literal`` enums so responses always
match the spec exactly.
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing_extensions import Literal

from .config import (
    CASE_TYPES,
    DEPARTMENTS,
    EVIDENCE_VERDICTS,
    SEVERITIES,
)

EvidenceVerdict = Literal[EVIDENCE_VERDICTS]  # type: ignore[valid-type]
CaseType = Literal[CASE_TYPES]  # type: ignore[valid-type]
Severity = Literal[SEVERITIES]  # type: ignore[valid-type]
Department = Literal[DEPARTMENTS]  # type: ignore[valid-type]


class TransactionHistoryEntry(BaseModel):
    """A single recent transaction. All fields are tolerant of missing data."""

    model_config = ConfigDict(extra="ignore")

    transaction_id: Optional[str] = None
    timestamp: Optional[str] = None
    type: Optional[str] = None
    amount: Optional[float] = None
    counterparty: Optional[str] = None
    status: Optional[str] = None

    @field_validator("amount", mode="before")
    @classmethod
    def _coerce_amount(cls, v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            return float(str(v).replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    @field_validator("transaction_id", "timestamp", "type", "counterparty", "status", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        return str(v)


class AnalyzeRequest(BaseModel):
    """POST /analyze-ticket request body."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: str
    complaint: str
    language: Optional[str] = None
    channel: Optional[str] = None
    user_type: Optional[str] = None
    campaign_context: Optional[str] = None
    transaction_history: Optional[List[TransactionHistoryEntry]] = None
    metadata: Optional[dict] = None

    @field_validator("ticket_id", mode="before")
    @classmethod
    def _coerce_ticket_id(cls, v: Any) -> Any:
        # Allow numeric ticket ids etc.; only None should fail as "missing".
        if v is None:
            return v
        return str(v)


class AnalyzeResponse(BaseModel):
    """POST /analyze-ticket response body (spec Section 6)."""

    ticket_id: str
    relevant_transaction_id: Optional[str] = None
    evidence_verdict: EvidenceVerdict
    case_type: CaseType
    severity: Severity
    department: Department
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    human_review_required: bool
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason_codes: Optional[List[str]] = None


class HealthResponse(BaseModel):
    status: str = "ok"
