"""HTTP front end for the monitor. Thin on purpose -- all behaviour is in service.py.

    .venv/bin/uvicorn http_app:app --port 8080
    curl -sX POST localhost:8080/run -H 'content-type: application/json' \
         -d '{"prompt":"Check for settlement mismatches."}' | jq

Concurrency note: each request spawns its own CLI subprocess via query(). That
is fine for a monitor doing a handful of runs an hour; it is not fine as a
chat backend. See the README section "Which client for which mode".
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from service import DEFAULT_PROMPT, TaskOutcome, run_task

app = FastAPI(title="Operations Monitor")


class RunRequest(BaseModel):
    prompt: str = DEFAULT_PROMPT


@app.post("/run", response_model=TaskOutcome)
async def run(req: RunRequest) -> TaskOutcome:
    # run_task never raises -- failures come back as ok=false with an error
    # string, so a bad run returns 200 with a structured body rather than a 500.
    return await run_task(req.prompt)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
