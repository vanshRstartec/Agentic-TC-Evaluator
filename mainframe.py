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
from html import escape, unescape
from pathlib import Path
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


# ── Terminal colors ───────────────────────────────────────────────────────────
# Two-tone scheme:
#   CODE (cyan)  = deterministic Python pipeline — what *we* do in code
#   LLM  (green) = Claude Code / LLM output      — what the agent produced
# Head / dim / warn / err are accent tones within the pipeline (cyan) stream.
_USE_COLOR = True            # set False to disable all ANSI colors

_C_RESET = "\033[0m"
_C_CODE  = "\033[36m"        # cyan      — deterministic pipeline
_C_LLM   = "\033[32m"        # green     — Claude Code / LLM output
_C_HEAD  = "\033[1;36m"      # bold cyan — section / phase headers
_C_DIM   = "\033[90m"        # gray      — rules and hints
_C_WARN  = "\033[33m"        # yellow    — warnings / skips
_C_ERR   = "\033[31m"        # red       — errors

# Ensure unicode + ANSI render cleanly on Windows consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _paint(text: str, color: str) -> str:
    """Wrap text in an ANSI color for terminal output (no-op if disabled/blank)."""
    if not _USE_COLOR or not color or not str(text).strip():
        return str(text)
    return f"{color}{text}{_C_RESET}"


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

def _log(msg: str = "", color: str = _C_CODE) -> None:
    """Print a pipeline line (deterministic code) to stdout; queue plain text."""
    print(_paint(msg, color), flush=True)
    if _log_queue is not None:
        _log_queue.put(str(msg))

def _tlog(msg: str) -> None:
    """Print a Claude Code / LLM line (green, timestamped) to stderr; queue plain."""
    stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(_paint(stamped, _C_LLM), file=sys.stderr, flush=True)
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
    _log("  ✓ Claude Code CLI found")

    # 2. Playwright MCP — check via claude mcp list (works cross-platform)
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "mcp", "list"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        mcp_output = (result.stdout or "") + (result.stderr or "")
        if "playwright" in mcp_output.lower():
            _log("  ✓ Playwright MCP found")
        else:
            raise EnvironmentError(
                "Playwright MCP server is not configured in Claude Code.\n"
                "Add it with:\n"
                "  claude mcp add playwright -- npx @playwright/mcp@latest"
            )
    except subprocess.TimeoutExpired:
        _log("  ! Playwright MCP check timed out — assuming configured", _C_WARN)

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

    _log(f"  Querying suite #{suite_id} in plan #{plan_id} …")
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
    _log(f"  Found {len(ids)} test case(s), loading details …")

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
        _log(f"    #{ado_id}  {title}  ·  {len(steps)} step(s)")

    _log(f"  ✓ {len(tcs)} test case(s) ready")
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
    _log(f"  Working dir : {EVALUATOR_WORKDIR}")
    _log(f"  Model       : {CLAUDE_MODEL}")

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

    def log(msg: str = "", color: str = _C_CODE) -> None:
        print(_paint(msg, color), flush=True)
        q.put(msg)

    rule = "─" * 52

    try:
        # ── Banner ──────────────────────────────────────────────
        log("")
        log(rule, _C_DIM)
        log("  TC EVALUATOR", _C_HEAD)
        log(rule, _C_DIM)
        log(f"  Org      : {org}")
        log(f"  Project  : {project}")
        log(f"  Plan     : {plan_id}     Suite : {suite_id}")
        log("")

        # ── Pre-flight ──────────────────────────────────────────
        log("▶ Pre-flight checks", _C_HEAD)
        _check_prerequisites()
        log("")

        # ── 1/5  Fetch test cases ───────────────────────────────
        log("▶ [1/5]  Fetch test cases from ADO", _C_HEAD)
        tcs = fetch_suite_test_cases(org, project, pat, plan_id, suite_id)
        log("")

        # ── 2/5  Assemble prompt ────────────────────────────────
        log("▶ [2/5]  Assemble evaluator prompt", _C_HEAD)
        prompt = build_prompt(tcs)
        log(f"  ✓ Prompt ready  ({len(prompt):,} chars · {len(tcs)} TC)")
        log("")

        # ── 3/5  Execute via Claude Code ────────────────────────
        log("▶ [3/5]  Execute via Claude Code", _C_HEAD)
        log("  (timestamped green lines below are Claude Code output)", _C_DIM)
        exit_code = run_claude_code(prompt, q)

        if exit_code != 0:
            raise Exception(
                f"Claude Code exited with code {exit_code}.\n"
                "Check the logs above for errors. Common causes:\n"
                "  - Authentication expired (run 'claude' interactively to re-authenticate)\n"
                "  - Model not available for your subscription\n"
                "  - Playwright MCP failed to launch"
            )
        log("  ✓ Execution finished")

        # Clean up Playwright MCP temp files
        import shutil
        pw_dir = Path(EVALUATOR_WORKDIR) / ".playwright-mcp"
        if pw_dir.exists():
            shutil.rmtree(pw_dir, ignore_errors=True)
            log("  ✓ Cleaned up temp files")

        # Read the evaluation Claude Code produced
        eval_path = Path(EVALUATOR_WORKDIR) / "evaluation.json"
        evaluation: list[dict] = []
        if eval_path.exists():
            try:
                evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
                log(f"  ✓ evaluation.json — {len(evaluation)} result(s)")
            except Exception as e:
                log(f"  ! evaluation.json could not be parsed — {e}", _C_WARN)
        else:
            log("  ! evaluation.json was not written", _C_WARN)
            log("    Claude Code may have stopped before finishing", _C_WARN)
        log("")

        # ── 4/5  Write results back to ADO ──────────────────────
        # Isolated in its own try/except so any failure here cannot
        # affect the result returned below.
        if evaluation:
            log("▶ [4/5]  Write results back to ADO", _C_HEAD)
            try:
                update_ado_results(org, project, pat, plan_id, suite_id, evaluation)
            except Exception as e:
                log(f"  ! ADO update failed — {e}", _C_WARN)
                log("    evaluation.json is saved; update ADO manually if needed", _C_WARN)
            log("")

        # ── 5/5  Create bugs for failed test cases ──────────────
        # Runs independently — a failure here never affects the
        # result object returned to the caller.
        if evaluation:
            log("▶ [5/5]  Create bugs for failed test cases", _C_HEAD)
            try:
                create_bugs_for_failures(org, project, pat, evaluation)
            except Exception as e:
                log(f"  ! Bug creation phase failed — {e}", _C_WARN)
                log("    evaluation.json is saved; create bugs manually if needed", _C_WARN)
            log("")

        job["result"] = {
            "status":          "complete",
            "plan_id":         plan_id,
            "suite_id":        suite_id,
            "tc_count":        len(tcs),
            "evaluation":      evaluation,
            "evaluation_file": str(eval_path),
        }

        log(rule, _C_DIM)
        log("  ✓ EVALUATOR COMPLETE", _C_HEAD)
        log(rule, _C_DIM)

    except (EnvironmentError, FileNotFoundError, PermissionError) as exc:
        # Config/setup errors — clear actionable message
        job["error"] = str(exc)
        log("")
        log("✗ SETUP ERROR", _C_ERR)
        log(f"{exc}", _C_ERR)
    except Exception as exc:
        job["error"] = str(exc)
        log("")
        log("✗ ERROR", _C_ERR)
        log(f"{exc}", _C_ERR)
    finally:
        q.put(None)


# ── ADO result updater ────────────────────────────────────────────────────────
# Maps evaluation "result" values to ADO outcome strings.
# Any value NOT in this map is treated as "other" → Active state, no outcome.
_OUTCOME_MAP = {
    "pass":   "Passed",
    "fail":   "Failed",
    "failed": "Failed",
}


def _ado_patch(url: str, pat: str, body, timeout: int = 30) -> dict:
    r = requests.patch(
        url,
        headers={"Content-Type": "application/json"},
        auth=HTTPBasicAuth("", pat),
        json=body,
        timeout=timeout,
    )
    if r.status_code in (200, 201):
        return r.json()
    raise Exception(f"ADO PATCH failed (HTTP {r.status_code}): {url}\n{r.text[:300]}")


def _ado_post(url: str, pat: str, body, timeout: int = 30) -> dict:
    r = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        auth=HTTPBasicAuth("", pat),
        json=body,
        timeout=timeout,
    )
    if r.status_code in (200, 201):
        return r.json()
    raise Exception(f"ADO POST failed (HTTP {r.status_code}): {url}\n{r.text[:300]}")


def _ado_post_wi(url: str, pat: str, body: list, timeout: int = 30) -> dict:
    """
    POST a new work item using the JSON-Patch document format required by ADO.

    Differs from _ado_post in that it sends Content-Type: application/json-patch+json,
    which the ADO work-item creation endpoint mandates.
    """
    r = requests.post(
        url,
        headers={"Content-Type": "application/json-patch+json"},
        auth=HTTPBasicAuth("", pat),
        json=body,
        timeout=timeout,
    )
    if r.status_code in (200, 201):
        return r.json()
    raise Exception(
        f"ADO work-item POST failed (HTTP {r.status_code}): {url}\n{r.text[:300]}"
    )


def _ado_post_wi_comment(base: str, pat: str, wi_id: str, text: str) -> None:
    """
    POST a comment to the Discussion section of a work item.

    Uses the Work Item Comments API (preview).  The comment appears in the
    Discussion tab of the test case — exactly where the evaluator note
    should be visible to testers and reviewers.

    Non-fatal: logs a warning on failure so the rest of the update continues.
    """
    url = f"{base}/wit/workItems/{wi_id}/comments?api-version=7.0-preview.3"
    r = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        auth=HTTPBasicAuth("", pat),
        json={"text": text},
        timeout=30,
    )
    if r.status_code not in (200, 201):
        raise Exception(
            f"Work-item comment POST failed (HTTP {r.status_code}): {url}\n{r.text[:300]}"
        )


def update_ado_results(
    org: str,
    project: str,
    pat: str,
    plan_id: str,
    suite_id: str,
    evaluation: list[dict],
) -> None:
    """
    Write each evaluation entry back to ADO as a brand-new test run AND
    post the evaluator reason as a comment in the test case Discussion tab.

    Rules (applied unconditionally for every entry):
    ─────────────────────────────────────────────────
    • Comment text — both "Result: X" and "Reason: Y" are always included,
      in both the Discussion tab and the execution-history comment field.
    • result == "pass"        → new run result, outcome = Passed, state = Completed
    • result == "fail/failed" → new run result, outcome = Failed, state = Completed
    • anything else           → NO run result created; test case stays Active.
                                Only a Discussion comment is posted.
    • Discussion comment is posted for EVERY entry (pass, fail, or other).
    • Existing test runs are never modified — a new run is always created.
    """
    if not evaluation:
        _log("  No evaluation entries — nothing to update", _C_WARN)
        return

    base = f"https://dev.azure.com/{org}/{project}/_apis"
    updated = 0
    skipped = 0

    # ── Phase 1: resolve every evaluation entry to its test point ─────────────
    # Each entry becomes (wi_id, point_id, comment, ado_outcome_or_None)
    # comment   — always set; prefixed with "Evaluator Agent: "
    # ado_outcome — "Passed"/"Failed" for pass/fail; None for anything else
    resolved: list[tuple[str, int, str, str | None]] = []

    for entry in evaluation:
        wi_id   = str(entry.get("id", ""))
        result  = (entry.get("result") or "").strip()
        reason  = (entry.get("reason") or "").strip()

        # Comment is built unconditionally — every entry gets one.
        # Both the result label and the reason are always included so that
        # anyone reading the Discussion tab or execution history has full context.
        result_label = result if result else "N/A"
        reason_text  = reason if reason else "(no reason provided)"
        comment = (
            f"Evaluator Agent\n"
            f"Result: {result_label}\n"
            f"Reason: {reason_text}"
        )

        # Outcome is None for anything that is not an explicit pass or fail.
        ado_outcome: str | None = _OUTCOME_MAP.get(result.lower())

        try:
            resp = _ado_get(
                f"{base}/test/Plans/{plan_id}/Suites/{suite_id}/points"
                f"?testCaseId={wi_id}&$top=1&api-version=7.0",
                pat,
            )
        except Exception as e:
            _log(f"    #{wi_id}  skipped — could not fetch test point: {e}", _C_WARN)
            skipped += 1
            continue

        points = resp.get("value", [])
        if not points:
            _log(f"    #{wi_id}  skipped — no test point in suite #{suite_id}", _C_WARN)
            skipped += 1
            continue

        point_id: int = int(points[0]["id"])
        resolved.append((wi_id, point_id, comment, ado_outcome))

    if not resolved:
        _log("  No resolvable test points — nothing to write to ADO", _C_WARN)
        _log(f"  ✓ ADO update — 0 updated, {skipped} skipped")
        return

    # ── Phase 2: create ONE new run — pass/fail test cases only ──────────────
    # Non-pass/fail test cases are intentionally excluded from the run so they
    # stay Active in ADO Test Plans.  They still receive a Discussion comment
    # (Phase 6 below).
    run_entries  = [(wi, pid, cmt, out) for wi, pid, cmt, out in resolved if out is not None]
    other_entries = [(wi, pid, cmt, out) for wi, pid, cmt, out in resolved if out is None]

    if other_entries:
        _log(
            f"  {len(other_entries)} non-pass/fail TC(s) will be left Active "
            f"(Discussion comment only)",
            _C_DIM,
        )

    if not run_entries:
        _log("  No pass/fail results — skipping test run creation")
        updated = 0
    else:
        point_ids = [pid for _, pid, _, _ in run_entries]
        _log(f"  Creating new test run for {len(point_ids)} pass/fail test case(s) …")

        try:
            run = _ado_post(
                f"{base}/test/runs?api-version=7.0",
                pat,
                {
                    "name":     f"Evaluator Agent - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    "plan":     {"id": str(plan_id)},
                    "pointIds": point_ids,
                },
            )
        except Exception as e:
            _log(f"  ! could not create test run — {e}", _C_WARN)
            _log("    Discussion comments will still be posted below", _C_WARN)
            updated = 0
            run_entries = []   # prevent further run-related steps
        else:
            new_run_id: int = run["id"]
            _log(f"  ✓ Created run #{new_run_id}")

            # Fetch the auto-created result stubs so we have their IDs.
            try:
                res = _ado_get(
                    f"{base}/test/runs/{new_run_id}/results?api-version=7.0",
                    pat,
                )
            except Exception as e:
                _log(f"  ! could not fetch results for run #{new_run_id} — {e}", _C_WARN)
                updated = 0
                run_entries = []
            else:
                # Build lookup: test-case ID → (comment, ado_outcome)
                # Only pass/fail entries are in run_entries so every outcome is non-None.
                by_tc: dict[str, tuple[str, str]] = {
                    wi_id: (comment, ado_outcome)          # type: ignore[misc]
                    for wi_id, _, comment, ado_outcome in run_entries
                }

                # ── Phase 3: build patches ─────────────────────────────────────────
                # All stubs in this run are pass/fail → all set to Completed.
                patches: list[dict] = []
                for r in res.get("value", []):
                    tc_id = str(r.get("testCase", {}).get("id", ""))
                    if tc_id not in by_tc:
                        continue
                    comment, ado_outcome = by_tc[tc_id]
                    patches.append({
                        "id":      r["id"],
                        "outcome": ado_outcome,
                        "state":   "Completed",
                        "comment": comment,   # result + reason in execution history
                    })
                    _log(f"    #{tc_id}  → {ado_outcome}  (comment recorded, state = Completed)")

                if not patches:
                    _log("  ! No matching result stubs — nothing patched", _C_WARN)
                    updated = 0
                else:
                    # ── Phase 4: write all patches in one PATCH call ───────────────
                    try:
                        _ado_patch(
                            f"{base}/test/runs/{new_run_id}/results?api-version=7.0",
                            pat,
                            patches,
                        )
                        updated = len(patches)
                    except Exception as e:
                        _log(f"  ! could not patch results for run #{new_run_id} — {e}", _C_WARN)
                        updated = 0

                    # ── Phase 5: close run as Completed ───────────────────────────
                    # Run only contains pass/fail results, all Completed.
                    try:
                        _ado_patch(
                            f"{base}/test/runs/{new_run_id}?api-version=7.0",
                            pat,
                            {"state": "Completed"},
                        )
                        _log(f"  ✓ Run #{new_run_id} state → Completed")
                    except Exception as e:
                        _log(f"  ! could not close run #{new_run_id} — {e}", _C_WARN)

    # ── Phase 6: post comment to test case Discussion (Work Item Comments) ──────
    # Posted for EVERY resolved entry — pass, fail, and non-pass/fail alike.
    # Contains both the result label and the reason so the Discussion tab is
    # self-contained without needing to open the test run.
    wi_commented = 0
    wi_comment_failed = 0
    _log("  Posting Discussion comments to test case work items …")
    for wi_id, _, comment, ado_outcome in resolved:
        tag = ado_outcome if ado_outcome else "Active (no run result)"
        try:
            _ado_post_wi_comment(base, pat, wi_id, comment)
            _log(f"    #{wi_id}  ✓ Discussion comment posted  [{tag}]")
            wi_commented += 1
        except Exception as e:
            _log(f"    #{wi_id}  ! Discussion comment failed — {e}", _C_WARN)
            wi_comment_failed += 1

    if wi_comment_failed:
        _log(
            f"  ! {wi_comment_failed} Discussion comment(s) failed "
            f"(run results were still written above)",
            _C_WARN,
        )

    _log(
        f"  ✓ ADO update — {updated} run result(s) updated, "
        f"{wi_commented} Discussion comment(s) posted, "
        f"{skipped} entry/entries skipped"
    )


# ── Bug creator ───────────────────────────────────────────────────────────────

def _fetch_pat_owner(pat: str) -> str | None:
    """
    Return the email address of the identity that owns the PAT.

    Calls the VSTS Profile API (org-agnostic) which is the most reliable
    way to resolve 'who am I' from a PAT token alone.  Falls back to
    displayName if emailAddress is absent, and returns None on any failure
    so callers can skip System.AssignedTo gracefully rather than crashing.
    """
    try:
        r = requests.get(
            "https://app.vssps.visualstudio.com/_apis/profile/profiles/me"
            "?api-version=6.0",
            headers={"Content-Type": "application/json"},
            auth=HTTPBasicAuth("", pat),
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            email   = (data.get("emailAddress") or "").strip()
            display = (data.get("displayName")  or "").strip()
            return email or display or None
    except Exception:
        pass
    return None


def _build_repro_steps_html(
    description: str,
    steps: list[str],
    expected_result: str,
    actual_result: str,
) -> str:
    """
    Build the HTML value for the Microsoft.VSTS.TCM.ReproSteps field.

    Matches the ADO bug template layout shown in the screenshot:

        Description,
        <description text>

        Steps to repro,
        1. <step 1>
        2. <step 2>
        ...

        Expected result,
        <expected result text>

        Actual result,
        <actual result text>
    """
    steps_li = "".join(f"<li>{escape(s)}</li>" for s in (steps or []))
    steps_block = f"<ol>{steps_li}</ol>" if steps_li else "<p></p>"

    return (
        f"<p><b>Description,</b></p>"
        f"<p>{escape(description or '')}</p>"
        f"<p><b>Steps to repro,</b></p>"
        f"{steps_block}"
        f"<p><b>Expected result,</b></p>"
        f"<p>{escape(expected_result or '')}</p>"
        f"<p><b>Actual result,</b></p>"
        f"<p>{escape(actual_result or '')}</p>"
    )


def _fetch_parent_story_ids(base: str, pat: str, tc_id: str) -> list[str]:
    """
    Return the work-item IDs of every parent story linked to the test case.

    ADO represents the parent→child hierarchy with two complementary relation
    types on each end of the link:
        System.LinkTypes.Hierarchy-Forward  — on the PARENT pointing at the child
        System.LinkTypes.Hierarchy-Reverse  — on the CHILD pointing at the parent

    We expand the TC's relations and collect every Hierarchy-Reverse URL,
    then extract the numeric ID from the trailing segment.
    """
    resp = _ado_get(
        f"{base}/wit/workitems/{tc_id}?$expand=relations&api-version=7.0",
        pat,
    )
    parent_ids: list[str] = []
    for rel in resp.get("relations", []):
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            url = rel.get("url", "")
            m = re.search(r"/workItems/(\d+)$", url, re.IGNORECASE)
            if m:
                parent_ids.append(m.group(1))
    return parent_ids


def _fetch_iteration_path(base: str, pat: str, wi_id: str) -> str | None:
    """
    Return System.IterationPath for a work item, or None on any error.
    Used to copy the parent story's iteration onto the created bug.
    """
    try:
        resp = _ado_get(
            f"{base}/wit/workitems/{wi_id}"
            f"?fields=System.IterationPath&api-version=7.0",
            pat,
        )
        return resp.get("fields", {}).get("System.IterationPath") or None
    except Exception:
        return None


def create_bugs_for_failures(
    org: str,
    project: str,
    pat: str,
    evaluation: list[dict],
) -> None:
    """
    For every evaluation entry where result == 'fail'/'failed' AND a
    'bug_details' key is present, create a Bug work item in ADO and link
    it to the corresponding test case work item.

    Bug fields written
    ──────────────────
    System.Title                   → "[Evaluator Agent] <bug_details.title>"
    Microsoft.VSTS.TCM.ReproSteps  → HTML matching the ADO bug template
    Microsoft.VSTS.Common.Priority → bug_details.priority (int, defaults to 2)

    Area and Iteration are intentionally omitted so ADO inherits the
    project defaults — consistent with manually filed bugs.

    The bug is related to the originating test-case work item via
    the System.LinkTypes.Related link type, so it appears in the
    "Related Work" section of both items.

    Pass / NA entries are untouched — no change to existing behaviour.
    """
    if not evaluation:
        return

    base        = f"https://dev.azure.com/{org}/{project}/_apis"
    # Work-item URL root used when building relation links.
    # ADO normalises the URL, so we use the canonical org-scoped form.
    wi_url_root = f"https://dev.azure.com/{org}/_apis/wit/workItems"

    # Resolve the identity that owns the PAT once — reused for every bug.
    assigned_to = _fetch_pat_owner(pat)
    if assigned_to:
        _log(f"  Assigning bugs to : {assigned_to}")
    else:
        _log("", _C_WARN)

    created = 0
    skipped = 0

    for entry in evaluation:
        result = (entry.get("result") or "").strip().lower()

        # Only process explicit failures.
        if result not in ("fail", "failed"):
            continue

        tc_id      = str(entry.get("id", ""))
        bug_details = entry.get("bug_details")

        if not bug_details:
            _log(
                f"    #{tc_id}  skipped bug creation — "
                f"result is '{result}' but no bug_details key found",
                _C_WARN,
            )
            skipped += 1
            continue

        # ── Extract bug_details fields ─────────────────────────────────────
        title        = (bug_details.get("title") or "").strip()
        description  = (bug_details.get("description") or "").strip()
        steps        = bug_details.get("steps_to_reproduce") or []
        expected     = (bug_details.get("expected_result") or "").strip()
        actual       = (bug_details.get("actual_result") or "").strip()
        priority_raw = bug_details.get("priority", "2")

        try:
            priority = int(str(priority_raw).strip())
        except (ValueError, TypeError):
            priority = 2

        if not title:
            _log(
                f"    #{tc_id}  skipped bug creation — bug_details.title is empty",
                _C_WARN,
            )
            skipped += 1
            continue

        full_title = f"[Evaluator Agent] {title}"
        repro_html = _build_repro_steps_html(description, steps, expected, actual)

        # ── Resolve parent story IDs and iteration path ────────────────────
        # Fetch the TC's Hierarchy-Reverse relations to find parent stories.
        # The bug will be Related-linked to every parent story found, and its
        # IterationPath will be copied from the first parent story's iteration.
        # All failures here are non-fatal — we log and carry on without the
        # story links / iteration rather than aborting the bug creation.
        parent_story_ids: list[str] = []
        iteration_path: str | None = None

        if tc_id:
            try:
                parent_story_ids = _fetch_parent_story_ids(base, pat, tc_id)
                if parent_story_ids:
                    _log(
                        f"    #{tc_id}  parent story(ies): "
                        f"{', '.join('#' + s for s in parent_story_ids)}"
                    )
                    # Copy iteration from the first parent story (almost always
                    # only one parent, but we use the first if there are multiple).
                    iteration_path = _fetch_iteration_path(
                        base, pat, parent_story_ids[0]
                    )
                    if iteration_path:
                        _log(f"    #{tc_id}  iteration path  : {iteration_path}")
                    else:
                        _log(
                            f"    #{tc_id}  ! could not read parent story iteration "
                            f"— bug will use project default",
                            _C_WARN,
                        )
                else:
                    _log(
                        f"    #{tc_id}  ! no parent story found "
                        f"— bug will not be linked to a story",
                        _C_WARN,
                    )
            except Exception as e:
                _log(
                    f"    #{tc_id}  ! parent story lookup failed — {e} "
                    f"— continuing without story link",
                    _C_WARN,
                )

        # ── Build JSON-Patch document ──────────────────────────────────────
        patch_doc: list[dict] = [
            {
                "op":    "add",
                "path":  "/fields/System.Title",
                "value": full_title,
            },
            {
                "op":    "add",
                "path":  "/fields/Microsoft.VSTS.TCM.ReproSteps",
                "value": repro_html,
            },
            {
                "op":    "add",
                "path":  "/fields/Microsoft.VSTS.Common.Priority",
                "value": priority,
            },
        ]

        if assigned_to:
            patch_doc.append(
                {
                    "op":    "add",
                    "path":  "/fields/System.AssignedTo",
                    "value": assigned_to,
                }
            )

        # Set the bug's iteration to match the parent story so it lands in
        # the correct sprint/iteration without needing manual triage.
        if iteration_path:
            patch_doc.append(
                {
                    "op":    "add",
                    "path":  "/fields/System.IterationPath",
                    "value": iteration_path,
                }
            )

        # ── Relate the bug back to the originating test-case ──────────────
        if tc_id:
            patch_doc.append(
                {
                    "op":   "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Related",
                        "url": f"{wi_url_root}/{tc_id}",
                        "attributes": {
                            "comment": "Linked by Evaluator Agent",
                        },
                    },
                }
            )

        # ── Relate the bug to every parent story ──────────────────────────
        # Usually exactly one story, but we handle multiples gracefully.
        for story_id in parent_story_ids:
            patch_doc.append(
                {
                    "op":   "add",
                    "path": "/relations/-",
                    "value": {
                        "rel": "System.LinkTypes.Related",
                        "url": f"{wi_url_root}/{story_id}",
                        "attributes": {
                            "comment": "Linked by Evaluator Agent (parent story)",
                        },
                    },
                }
            )

        # ── POST the new Bug work item ─────────────────────────────────────
        try:
            bug = _ado_post_wi(
                f"{base}/wit/workitems/$Bug?api-version=7.0",
                pat,
                patch_doc,
            )
            bug_id = bug.get("id", "?")
            _log(
                f"    #{tc_id}  → Bug #{bug_id} created"
                f"  (priority {priority})  {full_title}"
            )
            created += 1
        except Exception as e:
            _log(f"    #{tc_id}  ! Bug creation failed — {e}", _C_WARN)
            skipped += 1

    _log(
        f"  ✓ Bug creation — {created} bug(s) created, {skipped} skipped"
    )