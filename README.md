# QueueStorm Investigator

An AI/API SupportOps copilot for digital finance, built for the **SUST CSE Carnival 2026 · Codex Community Hackathon** (Online Preliminary).

It exposes two endpoints. Given one customer complaint plus a short snippet of that customer's recent transactions, it **investigates** what actually happened, decides who should handle it, and drafts a **safe** reply that never asks for credentials and never promises an unauthorized refund.

- `GET /health` -> `{"status":"ok"}`
- `POST /analyze-ticket` -> structured JSON analysis (schema in Section 6 of the problem statement)

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Web framework | **FastAPI** | Async, tiny, automatic JSON parsing/validation, great for a single JSON endpoint. |
| Validation | **Pydantic v2** | Exact enum (`Literal`) typing on the output so responses always match the spec. |
| HTTP client | **httpx** (async) | Precise per-request timeout control for the OpenRouter call. |
| Server | **uvicorn** | Local + Docker runtime. (Vercel provides its own ASGI server.) |
| LLM (optional) | **OpenRouter** | Text drafting only; the service is fully functional without it. |

Dependencies are intentionally minimal (4 packages) to keep cold starts fast and the Docker image small.

---

## Architecture: hybrid rules + LLM

The core design principle: **a deterministic engine is the source of truth for every scored decision; the LLM only drafts prose.** This guarantees a valid, safe, in-spec response within the 30s limit even if OpenRouter is slow, rate-limited, errors out, or returns garbage.

```
POST /analyze-ticket
   -> validate (Pydantic)         -> 400 on malformed / missing required fields
   -> empty complaint check       -> 422
   -> deterministic engine        (relevant_transaction_id, evidence_verdict,
                                    case_type, severity, department, human_review)
   -> LLM drafting (optional)     (agent_summary, recommended_next_action, customer_reply)
        |__ timeout/error/no key -> deterministic templated text
   -> safety sanitizer            (hard guardrails on the final text)
   -> 200 JSON
```

Files:
- [`app/main.py`](app/main.py) — FastAPI app, routes, exception handlers (never crashes).
- [`app/schemas.py`](app/schemas.py) — lenient request model, strict-enum response model.
- [`app/reasoning.py`](app/reasoning.py) — deterministic evidence engine (the 35-point core).
- [`app/llm.py`](app/llm.py) — OpenRouter client; returns `None` on any failure.
- [`app/safety.py`](app/safety.py) — safety sanitizer + templated fallbacks + final assembly.
- [`app/config.py`](app/config.py) — env vars + enum/taxonomy constants.
- [`api/index.py`](api/index.py) — Vercel serverless entrypoint.

### Evidence reasoning (how it investigates)

- **Amount extraction** from the complaint, including Bangla numerals (`০-৯`); phone-length numbers are excluded so they aren't read as amounts.
- **Transaction matching** by amount, refined by the transaction `type` expected for the case. 
  - Exactly one plausible match -> that transaction.
  - Several same-amount matches -> **ambiguous**: `relevant_transaction_id = null`, verdict `insufficient_data` (we never guess and risk a wrong dispute).
  - Duplicate payments (same amount + counterparty) -> the **later** transaction is flagged as the duplicate.
- **evidence_verdict**: `consistent` when a transaction supports the claim; `inconsistent` when the data contradicts it (e.g. repeated prior transfers to the same recipient contradicting a "wrong transfer" claim); `insufficient_data` when vague, ambiguous, or unmatched.
- **case_type** via prioritized multilingual keyword rules (safety first: phishing > duplicate > payment_failed > agent_cash_in > settlement > wrong_transfer > refund > other).
- **department** is mapped deterministically from `case_type` (+ `user_type`), so routing always matches the classification.
- **severity** and **human_review_required** follow rules calibrated to the published samples (e.g. phishing = `critical` + review; consistent wrong-transfer = `high` + review; failed payment = `high` but no review; vague = `low`, no review).

---

## MODELS

| Model | Where it runs | Role | Why |
|---|---|---|---|
| **Rule engine** (this repo) | In-process (CPU only) | Source of truth for all decision fields + safety + fallback text | Deterministic, instant, free, always available. Carries the evidence-reasoning and safety scores on its own. |
| `google/gemini-2.0-flash-001` (default, via **OpenRouter**) | OpenRouter API | Drafts `agent_summary`, `recommended_next_action`, `customer_reply` in natural, language-matched prose | Fast (~1-3s) and cheap, keeps p95 latency low. Swappable via `MODEL_NAME`. |

The LLM is **optional**: with no `OPENROUTER_API_KEY` set, the service runs fully on the rule engine using safe templated text (English/Bangla). The LLM never overrides the rule engine's evidence findings; it is also instructed to ignore any instructions embedded in the complaint (prompt-injection defense), and its output is passed through the same safety sanitizer.

**Cost reasoning:** one short LLM call per ticket (~600 max output tokens, temperature 0.2). A flash-class model on OpenRouter costs a fraction of a cent per ticket. Because every failure falls back to free rules, cost and availability risk are bounded.

---

## Safety logic

Enforced in [`app/safety.py`](app/safety.py) on the final `customer_reply` and `recommended_next_action`, regardless of whether the text came from the LLM or templates:

1. **No credential requests.** Sentences that *ask* for PIN/OTP/password/card are removed; warning sentences ("do not share your OTP") are kept, and a credential-safety note is always present.
2. **No unauthorized promises.** Phrases like "we will refund you", "we have reversed", "your account has been unblocked" are rewritten to "...any eligible amount will be returned through official channels". (Internal ops instructions like "initiate the reversal flow" are allowed — they are guidance for the agent, not a promise to the customer.)
3. **No third-party redirection.** URLs and external channels (WhatsApp/Telegram/"call this number") are stripped; customers are pointed only to official support channels.
4. **Prompt-injection resistant.** Decision fields are computed by deterministic rules from data, never from instructions in the complaint; the LLM is told to ignore embedded instructions; the sanitizer is the final backstop.

---

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Optional: enable the LLM drafting layer
cp .env.example .env               # then set OPENROUTER_API_KEY

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Smoke test:

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl -X POST http://localhost:8000/analyze-ticket \
  -H "Content-Type: application/json" \
  -d '{"ticket_id":"TKT-001","complaint":"I sent 5000 taka to a wrong number around 2pm today.","transaction_history":[{"transaction_id":"TXN-9101","timestamp":"2026-04-14T14:08:22Z","type":"transfer","amount":5000,"counterparty":"+8801719876543","status":"completed"}]}'
```

Run the test suites (no API key needed — they exercise the rule path):

```bash
python tests/test_samples.py      # 10/10 public sample cases match expected fields
python tests/test_api.py          # HTTP, error codes, injection, multilingual, edge cases
```

A pre-generated [`sample_output.json`](sample_output.json) contains the service output for all 10 public sample cases.

---

## Deploy

### Path A — Vercel (recommended)

The repo is Vercel-ready: [`api/index.py`](api/index.py) exposes the ASGI app and [`vercel.json`](vercel.json) routes all paths to it with `maxDuration: 60`.

```bash
npm i -g vercel
vercel            # first deploy (links the project)
# In the Vercel dashboard -> Project -> Settings -> Environment Variables, add:
#   OPENROUTER_API_KEY = <your key>      (optional but recommended)
#   MODEL_NAME         = google/gemini-2.0-flash-001
vercel --prod     # production deploy
```

Then verify externally:

```bash
curl https://<your-app>.vercel.app/health
curl -X POST https://<your-app>.vercel.app/analyze-ticket -H "Content-Type: application/json" -d '{...}'
```

> Note on serverless: keep `MODEL_NAME` on a fast model. Cold start + one flash-class LLM call stays comfortably under 30s, and the rule-only fallback guarantees a response even if the LLM call is slow.

### Path B — Docker fallback

```bash
docker build -t queuestorm-team .
docker run -p 8000:8000 --env-file judging.env queuestorm-team
# /health and /analyze-ticket on http://localhost:8000  (binds 0.0.0.0)
```

`judging.env` (not committed) may contain `OPENROUTER_API_KEY` / `MODEL_NAME`. The service runs without them too. Image is based on `python:3.12-slim` and stays well under 500MB.

### Path C — Code + runbook

Follow "Run locally" above; it is a complete, copy-pasteable runbook.

---

## Assumptions

- Public sample cases reflect the calibration target for severity/escalation; rules are tuned to match them and to generalize to the documented taxonomy.
- A "wrong transfer" to a recipient the customer has repeatedly transferred to before is treated as `inconsistent` (likely established recipient) and flagged for human review rather than auto-disputed.
- Agent-facing fields (`agent_summary`, `recommended_next_action`) are written in English; the customer-facing `customer_reply` is written in the customer's language (English/Bangla), matching the sample pack.
- All data is synthetic; no real payment integration is performed.

## Known limitations

- Case classification is keyword-driven for determinism; very unusual phrasings with no recognized signal fall back to `other` + `insufficient_data` (safe, asks for clarification). The LLM helps phrasing but does not override classification.
- Latin-script Banglish detection of the reply language is heuristic; replies default to English when no Bangla script is present.
- Free-text quality depends on the LLM when enabled; without a key, replies are correct and safe but more templated.
- No persistence/caching across requests beyond the process (stateless by design for serverless).

## Security / secrets

No secrets are committed. Configuration is via environment variables only ([`.env.example`](.env.example) lists names with placeholder values). Responses, logs, and errors never include secrets or stack traces; unexpected errors return a generic `500` message.
