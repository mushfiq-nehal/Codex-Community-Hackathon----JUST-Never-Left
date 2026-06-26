"""FastAPI application: GET /health and POST /analyze-ticket.

Design goals reflected here:
- /health is trivial and synchronous so readiness is immediate.
- Malformed input -> 400, semantically-invalid input -> 422, unexpected
  failures -> 500, and the process never crashes (every path returns JSON).
- No secrets or stack traces ever appear in a response body.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .llm import draft_response
from .reasoning import investigate
from .safety import assemble_response
from .schemas import AnalyzeRequest, AnalyzeResponse, HealthResponse

logger = logging.getLogger("queuestorm")

app = FastAPI(
    title="QueueStorm Investigator",
    version="1.0.0",
    description="AI/API SupportOps copilot for digital finance support agents.",
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness/readiness probe. Must return {"status":"ok"} quickly."""
    return HealthResponse(status="ok")


@app.post("/analyze-ticket", response_model=AnalyzeResponse)
async def analyze_ticket(payload: AnalyzeRequest) -> JSONResponse:
    """Investigate one ticket and return a structured, safe analysis."""
    # Semantically invalid: schema is fine but complaint is empty.
    if not payload.complaint or not payload.complaint.strip():
        return JSONResponse(
            status_code=422,
            content={"error": "The 'complaint' field must be a non-empty string."},
        )

    try:
        decision = investigate(payload)
        llm_text = await draft_response(payload, decision)
        response = assemble_response(payload, decision, llm_text)
        return JSONResponse(status_code=200, content=response.model_dump())
    except Exception:  # noqa: BLE001 - last-resort guard so we never crash
        logger.exception("Unhandled error while analyzing ticket")
        return JSONResponse(
            status_code=500,
            content={"error": "Internal error while analyzing the ticket."},
        )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Treat malformed bodies / missing required fields / bad JSON as 400."""
    return JSONResponse(
        status_code=400,
        content={"error": "Malformed request: invalid JSON or missing required fields."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all so a bug never returns a stack trace or takes down the process."""
    logger.exception("Unhandled application error")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error."},
    )
