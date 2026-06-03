"""
Evaluator mainframe — fetches ADO test cases, builds prompt, runs Claude Code.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


# ── Config ────────────────────────────────────────────────────────────────────
_EVALUATOR_MD   = Path(__file__).resolve().parent / "evaluator.md"
EVALUATOR_WORKDIR = str(Path(__file__).resolve().parent)
CLAUDE_BIN      = "claude"
CLAUDE_MODEL    = "claude-opus-4-8"  # opus-4-8 | sonnet-4-5 | haiku-4-5

# ── Logging ───────────────────────────────────────────────────────────────────
_log_queue: queue.Queue | None = None

def set_log_queue(q: queue.Queue | None) -> None:
    global _log_queue
    _log_queue = q

def _log(msg: str = "") -> None:
    """Print to stdout and push to SSE queue."""
    print(msg, flush=True)
    if _log_queue is not None:
        _log_queue.put(str(msg))

def _tlog(msg: str) -> None:
    """Timestamped print to stderr (terminal) and push to SSE queue."""
    stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(stamped, file=sys.stderr, flush=True)
    print("", file=sys.stderr, flush=True)
    if _log_queue is not None:
        _log_queue.put(stamped)

# ── Helpers ───────────────────────────────────────────────────────────────────
def _strip_html(text: Any) -> str:
    return re.sub(r"<[^>]+>", "", unescape(str(text or ""))).strip()

def _ado_get(url: str, pat: str, timeout: int = 30) -> dict:
    r = requests.get(url, headers={"Content-Type": "application/json"},
                     auth=HTTPBasicAuth("", pat), timeout=timeout)
    if r.status_code == 200:
        return r.json()
    raise Exception(f"ADO request failed (HTTP {r.status_code}): {url}")

def _parse_steps_xml(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    out = []
    for el in root:
        tag = (el.tag or "").lower()
        if tag == "step":
            ps = el.findall("parameterizedString")
            action = _strip_html(ps[0].text) if ps else ""
            expected = _strip_html(ps[1].text) if len(ps) > 1 else ""
            if action:
                out.append({"action": action, "expected": expected})
        elif tag == "compref":
            out.append({"action": f"[Shared steps ref: {el.get('ref', '')}]", "expected": ""})
    return out

# ── Pre-flight checks ─────────────────────────────────────────────────────────
def _check_prerequisites() -> None:
    """
    Verify Claude Code CLI and Playwright MCP are configured.
    """
    import shutil

    # 1. Claude Code CLI on PATH
    if not shutil.which(CLAUDE_BIN):
        raise EnvironmentError(
            f"Claude Code CLI not found: '{CLAUDE_BIN}' is not on PATH.\n"
            "Install:  npm install -g @anthropic-ai/claude-code\n"
            "Then run 'claude' once interactively to authenticate."
        )
    _log("  Claude Code CLI: found.")

    # 2. Playwright MCP — check via claude mcp list (works cross-platform)
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "mcp", "list"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        mcp_output = (result.stdout or "") + (result.stderr or "")
        if "playwright" in mcp_output.lower():
            _log("  Playwright MCP: found.")
        else:
            raise EnvironmentError(
                "Playwright MCP server is not configured in Claude Code.\n"
                "Add it with:\n"
                "  claude mcp add playwright -- npx @playwright/mcp@latest"
            )
    except subprocess.TimeoutExpired:
        _log("  Playwright MCP: check timed out — assuming configured.")

    # 3. evaluator.md prompt template
    if not _EVALUATOR_MD.exists():
        raise FileNotFoundError(
            f"Prompt template not found: {_EVALUATOR_MD}\n"
            "Create evaluator.md in the same directory as mainframe.py."
        )

    # 4. Working directory exists and is writable
    workdir = Path(EVALUATOR_WORKDIR)
    if not workdir.exists():
        raise FileNotFoundError(
            f"EVALUATOR_WORKDIR does not exist: {workdir}\n"
            "Create the directory or update the EVALUATOR_WORKDIR env var."
        )
    if not os.access(workdir, os.W_OK):
        raise PermissionError(
            f"EVALUATOR_WORKDIR is not writable: {workdir}\n"
            "Check directory permissions."
        )


# ── ADO fetch ─────────────────────────────────────────────────────────────────
def fetch_suite_test_cases(org: str, project: str, pat: str,
                           plan_id: str, suite_id: str) -> list[dict]:
    """Return [{id, title, steps}] for every TC in the given plan/suite."""
    base = f"https://dev.azure.com/{org}/{project}/_apis"

    _log(f"  Fetching TCs from plan #{plan_id}, suite #{suite_id}...")
    try:
        suite_resp = _ado_get(
            f"{base}/testplan/Plans/{plan_id}/Suites/{suite_id}/TestCase?api-version=7.0", pat)
    except Exception as e:
        raise Exception(
            f"Could not fetch test suite from ADO.\n"
            f"Check org/project/plan_id/suite_id and that your PAT has Test Management read access.\n"
            f"Detail: {e}"
        )

    refs = suite_resp.get("value", [])
    if not refs:
        raise Exception(
            f"Suite #{suite_id} in plan #{plan_id} is empty — no test cases found.\n"
            "Verify the suite ID and that it contains test cases in ADO."
        )

    ids = [str(r["workItem"]["id"]) for r in refs]
    _log(f"  {len(ids)} TC(s) found — hydrating...")

    try:
        detail = _ado_get(
            f"{base}/wit/workitems?ids={','.join(ids)}"
            f"&fields=System.Title,Microsoft.VSTS.TCM.Steps&api-version=7.0", pat)
    except Exception as e:
        raise Exception(f"Could not hydrate work items from ADO.\nDetail: {e}")

    tcs = []
    for wi in detail.get("value", []):
        ado_id = wi["id"]
        title  = wi["fields"].get("System.Title", "Untitled")
        steps  = _parse_steps_xml(wi["fields"].get("Microsoft.VSTS.TCM.Steps", ""))
        tcs.append({"id": ado_id, "title": title, "steps": steps})
        _log(f"    #{ado_id}  {title}  ({len(steps)} step(s))")

    _log(f"  Fetch complete — {len(tcs)} TC(s) ready.")
    return tcs

# ── Prompt assembly ───────────────────────────────────────────────────────────
def build_prompt(tcs: list[dict]) -> str:
    """Load evaluator.md and substitute {test_cases_json}."""
    template = _EVALUATOR_MD.read_text(encoding="utf-8")
    tcs_json = json.dumps(tcs, ensure_ascii=False, separators=(",", ":"))
    return template.replace("{test_cases_json}", tcs_json)

# ── stream-json parser ────────────────────────────────────────────────────────
def _parse_event(line: str) -> str | None:
    """
    Extract human-readable text from a Claude Code stream-json event line.
    Returns None for events that should not be shown (tool calls, metadata, etc).
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line  # raw stderr / startup text — pass through

    t = obj.get("type", "")

    if t == "assistant":
        parts = [
            block["text"].strip()
            for block in obj.get("message", {}).get("content", [])
            if block.get("type") == "text" and block.get("text", "").strip()
        ]
        return "\n".join(parts) or None

    if t == "result":
        cost = obj.get("total_cost_usd")
        return f"[DONE]  [cost: ${cost:.4f}]" if cost is not None else "[DONE]"

    if t == "system":
        sub = obj.get("subtype", "")
        if sub == "init":
            model = re.sub(r"\x1b\[[0-9;]*m", "", obj.get("model", "unknown"))
            return f"[Claude Code started — model: {model}]"
        if sub == "api_retry":
            return (f"[api_retry] attempt {obj.get('attempt','?')} "
                    f"— {obj.get('error','unknown')} "
                    f"— retrying in {obj.get('retry_delay_ms', 0)}ms")

    return None

# ── Claude Code invocation ────────────────────────────────────────────────────
def run_claude_code(prompt: str, log_q: queue.Queue) -> int:
    """Spawn claude -p, stream parsed output into log_q, return exit code."""
    cmd = [
        CLAUDE_BIN, "-p", prompt,
        "--model", CLAUDE_MODEL,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    _log(f"  Workdir : {EVALUATOR_WORKDIR}")
    _log(f"  Model   : {CLAUDE_MODEL}")

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=EVALUATOR_WORKDIR,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        raise EnvironmentError(
            f"Failed to launch Claude Code: '{CLAUDE_BIN}' not found.\n"
            "Ensure Claude Code is installed and on PATH, or set CLAUDE_BIN."
        )

    for raw_line in proc.stdout:
        text = _parse_event(raw_line)
        if text:
            for part in text.splitlines():
                if part.strip():
                    _tlog(part)
                    log_q.put(f"[{datetime.now().strftime('%H:%M:%S')}] {part}")

    proc.wait()
    return proc.returncode

# ── Orchestrator ──────────────────────────────────────────────────────────────
def _run_evaluation(org: str, project: str, pat: str,
                    plan_id: str, suite_id: str, job: dict) -> None:
    """Full pipeline. Runs on a background thread. Always pushes None sentinel."""
    q: queue.Queue = job["queue"]

    def log(msg: str = "") -> None:
        print(msg, flush=True)
        q.put(msg)

    hr = "-" * 50

    try:
        # Pre-flight
        log("Evaluator started.")
        log(f" Org: {org}  |  Project: {project}  |  Plan: {plan_id}  |  Suite: {suite_id}")
        log(hr)
        _check_prerequisites()

        # Step 1 — fetch
        log("Step 1/3: Fetching test cases from ADO...")
        tcs = fetch_suite_test_cases(org, project, pat, plan_id, suite_id)
        log(f" {len(tcs)} TC(s) fetched.")
        log(hr)

        # Step 2 — prompt
        log("Step 2/3: Assembling evaluator prompt...")
        prompt = build_prompt(tcs)
        log(f" Prompt: {len(prompt)} chars  |  TCs: {len(tcs)}")
        log(hr)

        # Step 3 — Claude Code
        log("Step 3/3: Invoking Claude Code...")
        log(hr)
        exit_code = run_claude_code(prompt, q)
        log(hr)

        if exit_code != 0:
            raise Exception(
                f"Claude Code exited with code {exit_code}.\n"
                "Check the logs above for errors. Common causes:\n"
                "  - Authentication expired (run 'claude' interactively to re-authenticate)\n"
                "  - Model not available for your subscription\n"
                "  - Playwright MCP failed to launch"
            )

        # Clean up Playwright MCP temp files
        import shutil
        pw_dir = Path(EVALUATOR_WORKDIR) / ".playwright-mcp"
        if pw_dir.exists():
            shutil.rmtree(pw_dir, ignore_errors=True)
            log(" Cleaned up .playwright-mcp temp files.")

        # Read result
        eval_path = Path(EVALUATOR_WORKDIR) / "evaluation.json"
        evaluation: list[dict] = []
        if eval_path.exists():
            try:
                evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
                log(f" evaluation.json — {len(evaluation)} result(s).")
            except Exception as e:
                log(f" Warning: evaluation.json exists but could not be parsed — {e}")
        else:
            log(" Warning: evaluation.json was not written.")
            log(" Claude Code may have exited before completing all test cases.")

        job["result"] = {
            "status":          "complete",
            "plan_id":         plan_id,
            "suite_id":        suite_id,
            "tc_count":        len(tcs),
            "evaluation":      evaluation,
            "evaluation_file": str(eval_path),
        }
        log("Evaluator complete.")

    except (EnvironmentError, FileNotFoundError, PermissionError) as exc:
        # Config/setup errors — clear actionable message
        job["error"] = str(exc)
        log(f"SETUP ERROR:\n{exc}")
    except Exception as exc:
        job["error"] = str(exc)
        log(f"ERROR: {exc}")
    finally:
        q.put(None)