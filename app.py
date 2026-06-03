"""
Evaluator API — Flask app.

Endpoints
─────────
POST /evaluator
    Kick off an evaluation job.

    Request body (JSON)
    ───────────────────
    {
        "org":      "your-ado-org",          # required
        "project":  "your-ado-project",      # required
        "pat":      "your-pat-token",        # required
        "plan_id":  "123",                   # required  (ADO test plan ID)
        "suite_id": "456"                    # required  (ADO test suite ID)
    }

    Response
    ────────
    {"status": "started", "job_id": "<uuid>"}

GET /evaluator/logs/<job_id>
    Server-Sent Events stream of live Claude Code output.
    Final event is either:
        __RESULT__<json>   — success, contains evaluation list
        __ERROR__<message> — failure

GET /evaluator/result/<job_id>
    Poll for the result once the job is complete.
    Returns 202 while still running, 200 with result when done.

GET /
    Health check.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import uuid

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS

import mainframe as mf

app = Flask(__name__)
CORS(app)

# ── Job registry ──────────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_LOG_TIMEOUT_S = 18000


# ── Helpers ───────────────────────────────────────────────────────────────────
def _err(msg: str, code: int = 400):
    return jsonify({"status": "error", "message": msg}), code


def _start_job(org: str, project: str, pat: str,
               plan_id: str, suite_id: str) -> str:
    """
    Create a job entry, start the evaluation pipeline on a daemon thread,
    and return the job_id.
    """
    job_id = str(uuid.uuid4())
    q      = queue.Queue()
    job    = {"queue": q, "result": None, "error": None}
    _jobs[job_id] = job

    threading.Thread(
        target=mf._run_evaluation,
        args=(org, project, pat, plan_id, suite_id, job),
        daemon=True,
    ).start()

    return job_id


def _eval_payload(d: dict) -> tuple[dict, list[str]]:
    """Extract and validate required fields from the request body."""
    params = {
        "org":      str(d.get("org",      "")).strip(),
        "project":  str(d.get("project",  "")).strip(),
        "pat":      str(d.get("pat",      "")).strip(),
        "plan_id":  str(d.get("plan_id",  "")).strip(),
        "suite_id": str(d.get("suite_id", "")).strip(),
    }
    missing = [k for k, v in params.items() if not v]
    return params, missing


# ── POST /evaluator ───────────────────────────────────────────────────────────
@app.route("/evaluator", methods=["POST"])
def route_evaluator():
    body = request.get_json(silent=True) or {}
    params, missing = _eval_payload(body)
    if missing:
        return _err(f"Missing required field(s): {', '.join(missing)}")

    job_id = _start_job(**params)
    return jsonify({"status": "started", "job_id": job_id})


# ── GET /evaluator/logs/<job_id> — SSE stream ─────────────────────────────────
@app.route("/evaluator/logs/<job_id>", methods=["GET"])
def route_evaluator_logs(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("Job not found.", 404)

    def _stream():
        q = job["queue"]
        while True:
            try:
                msg = q.get(timeout=_LOG_TIMEOUT_S)
            except queue.Empty:
                yield "data: ⚠️ Timed out waiting for next log line.\n\n"
                break

            if msg is None:
                # Sentinel — pipeline finished
                if job.get("result"):
                    yield f"data: __RESULT__{json.dumps(job['result'])}\n\n"
                else:
                    err = job.get("error") or "Unknown error"
                    yield f"data: __ERROR__{err}\n\n"
                _jobs.pop(job_id, None)
                break

            clean = str(msg).replace("\n", " ").replace("\r", "")
            yield f"data: {clean}\n\n"

    return Response(
        stream_with_context(_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":               "no-cache",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── GET /evaluator/result/<job_id> — poll endpoint ───────────────────────────
@app.route("/evaluator/result/<job_id>", methods=["GET"])
def route_evaluator_result(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return _err("Job not found.", 404)

    if job["error"]:
        return jsonify({"status": "error", "message": job["error"]}), 500

    if job["result"] is None:
        return jsonify({"status": "running"}), 202

    return jsonify({"status": "complete", **job["result"]})


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def route_health():
    return jsonify({"status": "ok", "service": "tc-evaluator"})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)