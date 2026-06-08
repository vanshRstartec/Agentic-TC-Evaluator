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
_USE_COLOR = True
_C_RESET = "\033[0m"
_C_CODE  = "\033[36m"
_C_LLM   = "\033[32m"
_C_HEAD  = "\033[1;36m"
_C_DIM   = "\033[90m"
_C_WARN  = "\033[33m"
_C_ERR   = "\033[31m"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def _paint(text: str, color: str) -> str:
    if not _USE_COLOR or not color or not str(text).strip():
        return str(text)
    return f"{color}{text}{_C_RESET}"

# ── Config ────────────────────────────────────────────────────────────────────
_EVALUATOR_MD     = Path(__file__).resolve().parent / "evaluator.md"
EVALUATOR_WORKDIR = str(Path(__file__).resolve().parent)
CLAUDE_BIN        = "claude"
CLAUDE_MODEL      = "claude-opus-4-8"

# ── Logging ───────────────────────────────────────────────────────────────────
_log_queue: queue.Queue | None = None

def set_log_queue(q: queue.Queue | None) -> None:
    global _log_queue
    _log_queue = q

def _log(msg: str = "", color: str = _C_CODE) -> None:
    print(_paint(msg, color), flush=True)
    if _log_queue is not None:
        _log_queue.put(str(msg))

def _tlog(msg: str) -> None:
    stamped = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(_paint(stamped, _C_LLM), file=sys.stderr, flush=True)
    if _log_queue is not None:
        _log_queue.put(stamped)

# ── ADO helpers ───────────────────────────────────────────────────────────────
def _strip_html(text: Any) -> str:
    return re.sub(r"<[^>]+>", "", unescape(str(text or ""))).strip()

def _ado_get(url: str, pat: str, timeout: int = 30) -> dict:
    r = requests.get(url, headers={"Content-Type": "application/json"},
                     auth=HTTPBasicAuth("", pat), timeout=timeout)
    if r.status_code == 200:
        return r.json()
    raise Exception(f"ADO GET failed (HTTP {r.status_code}): {url}")

def _ado_req(method: str, url: str, pat: str, body=None, *,
             patch_json: bool = False, timeout: int = 30) -> dict:
    """Single helper for all ADO POST / PATCH / PUT calls."""
    ct = "application/json-patch+json" if patch_json else "application/json"
    r  = getattr(requests, method)(
        url, headers={"Content-Type": ct},
        auth=HTTPBasicAuth("", pat), json=body, timeout=timeout,
    )
    if r.status_code in (200, 201):
        return r.json()
    raise Exception(
        f"ADO {method.upper()} failed (HTTP {r.status_code}): {url}\n{r.text[:300]}"
    )

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
            ps       = el.findall("parameterizedString")
            action   = _strip_html(ps[0].text) if ps else ""
            expected = _strip_html(ps[1].text) if len(ps) > 1 else ""
            if action:
                out.append({"action": action, "expected": expected})
        elif tag == "compref":
            out.append({"action": f"[Shared steps ref: {el.get('ref', '')}]", "expected": ""})
    return out

# ── Pre-flight ────────────────────────────────────────────────────────────────
def _check_prerequisites() -> None:
    import shutil
    if not shutil.which(CLAUDE_BIN):
        raise EnvironmentError(
            f"Claude Code CLI not found: '{CLAUDE_BIN}' is not on PATH.\n"
            "Install:  npm install -g @anthropic-ai/claude-code\n"
            "Then run 'claude' once interactively to authenticate."
        )
    _log("  ✓ Claude Code CLI found")
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "mcp", "list"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        if "playwright" in ((result.stdout or "") + (result.stderr or "")).lower():
            _log("  ✓ Playwright MCP found")
        else:
            raise EnvironmentError(
                "Playwright MCP server is not configured in Claude Code.\n"
                "Add it with:\n"
                "  claude mcp add playwright -- npx @playwright/mcp@latest"
            )
    except subprocess.TimeoutExpired:
        _log("  ! Playwright MCP check timed out — assuming configured", _C_WARN)
    if not _EVALUATOR_MD.exists():
        raise FileNotFoundError(
            f"Prompt template not found: {_EVALUATOR_MD}\n"
            "Create evaluator.md in the same directory as mainframe.py."
        )
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
    template = _EVALUATOR_MD.read_text(encoding="utf-8")
    return template.replace("{test_cases_json}",
                            json.dumps(tcs, ensure_ascii=False, separators=(",", ":")))

# ── stream-json parser ────────────────────────────────────────────────────────
def _parse_event(line: str) -> str | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return line
    t = obj.get("type", "")
    if t == "assistant":
        parts = [
            b["text"].strip()
            for b in obj.get("message", {}).get("content", [])
            if b.get("type") == "text" and b.get("text", "").strip()
        ]
        return "\n".join(parts) or None
    if t == "result":
        cost = obj.get("total_cost_usd")
        return f"[DONE]  [cost: ${cost:.4f}]" if cost is not None else "[DONE]"
    if t == "system":
        sub = obj.get("subtype", "")
        if sub == "init":
            return f"[Claude Code started — model: {re.sub(chr(27) + r'\[[0-9;]*m', '', obj.get('model', 'unknown'))}]"
        if sub == "api_retry":
            return (f"[api_retry] attempt {obj.get('attempt','?')} "
                    f"— {obj.get('error','unknown')} "
                    f"— retrying in {obj.get('retry_delay_ms', 0)}ms")
    return None

# ── Claude Code invocation ────────────────────────────────────────────────────
def run_claude_code(prompt: str, log_q: queue.Queue) -> int:
    """Spawn claude -p, stream parsed output into log_q, return exit code."""
    cmd = [CLAUDE_BIN, "-p", prompt, "--model", CLAUDE_MODEL,
           "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]
    _log(f"  Working dir : {EVALUATOR_WORKDIR}")
    _log(f"  Model       : {CLAUDE_MODEL}")
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=EVALUATOR_WORKDIR,
            encoding="utf-8", errors="replace", bufsize=1,
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

# ── Report folder discovery ───────────────────────────────────────────────────
_REPORT_DIR_RE = re.compile(r"^report_(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$")


def _latest_report_dir(workdir: Path) -> Path | None:
    """Return the newest reports/report_<timestamp> directory, or None if absent.

    Folders are named report_YYYY-MM-DD_HH-MM-SS, so for conforming names the
    embedded timestamp sorts chronologically. Non-conforming folders fall back to
    mtime so a stray directory can't masquerade as the newest one.
    """
    reports_root = workdir / "reports"
    if not reports_root.is_dir():
        return None
    candidates = [d for d in reports_root.iterdir()
                  if d.is_dir() and d.name.startswith("report_")]
    if not candidates:
        return None

    def _key(d: Path) -> tuple[str, float]:
        m = _REPORT_DIR_RE.match(d.name)
        return (m.group(1) if m else "", d.stat().st_mtime)

    return max(candidates, key=_key)

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
        log("")
        log(rule, _C_DIM)
        log("  TC EVALUATOR", _C_HEAD)
        log(rule, _C_DIM)
        log(f"  Org      : {org}")
        log(f"  Project  : {project}")
        log(f"  Plan     : {plan_id}     Suite : {suite_id}")
        log("")

        log("▶ Pre-flight checks", _C_HEAD)
        _check_prerequisites()
        log("")

        log("▶ [1/5]  Fetch test cases from ADO", _C_HEAD)
        tcs = fetch_suite_test_cases(org, project, pat, plan_id, suite_id)
        log("")

        log("▶ [2/5]  Assemble evaluator prompt", _C_HEAD)
        prompt = build_prompt(tcs)
        log(f"  ✓ Prompt ready  ({len(prompt):,} chars · {len(tcs)} TC)")
        log("")

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

        import shutil
        pw_dir = Path(EVALUATOR_WORKDIR) / ".playwright-mcp"
        if pw_dir.exists():
            shutil.rmtree(pw_dir, ignore_errors=True)
            log("  ✓ Cleaned up temp files")

        report_dir = _latest_report_dir(Path(EVALUATOR_WORKDIR))
        eval_path: Path | None = (report_dir / "evaluation.json") if report_dir else None
        evaluation: list[dict] = []
        if report_dir is None:
            log("  ! No reports/report_* folder was created", _C_WARN)
            log("    Claude Code may have stopped before finishing", _C_WARN)
        elif eval_path.exists():
            log(f"  Using latest report: {report_dir.name}")
            try:
                evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
                log(f"  ✓ evaluation.json — {len(evaluation)} result(s)")
            except Exception as e:
                log(f"  ! evaluation.json could not be parsed — {e}", _C_WARN)
        else:
            log(f"  ! evaluation.json not found in {report_dir.name}", _C_WARN)
            log("    Claude Code may have stopped before finishing", _C_WARN)
        log("")

        if evaluation:
            log("▶ [4/5]  Write results back to ADO", _C_HEAD)
            try:
                # Pass report_dir so evidence files can be attached to TC comments
                update_ado_results(org, project, pat, plan_id, suite_id,
                                   evaluation, report_dir)
            except Exception as e:
                log(f"  ! ADO update failed — {e}", _C_WARN)
                log("    evaluation.json is saved; update ADO manually if needed", _C_WARN)
            log("")

        if evaluation:
            log("▶ [5/5]  Create bugs for failed test cases", _C_HEAD)
            try:
                # Pass report_dir so evidence files are embedded in bug ReproSteps
                create_bugs_for_failures(org, project, pat, evaluation, report_dir)
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
            "report_dir":      str(report_dir) if report_dir else None,
            "evaluation_file": str(eval_path) if eval_path else None,
        }
        log(rule, _C_DIM)
        log("  ✓ EVALUATOR COMPLETE", _C_HEAD)
        log(rule, _C_DIM)

    except (EnvironmentError, FileNotFoundError, PermissionError) as exc:
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

# ── Evidence helpers ──────────────────────────────────────────────────────────
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
_VIDEO_EXTS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}


def _get_tc_evidence(report_dir: Path | None, tc_id: str) -> list[Path]:
    """Return sorted evidence files (images + videos) from TC_<tc_id> folder.

    Returns an empty list if report_dir is None, the folder doesn't exist,
    or the folder contains no recognised image/video files.
    """
    if report_dir is None:
        return []
    tc_folder = report_dir / f"TC_{tc_id}"
    if not tc_folder.is_dir():
        return []
    return sorted(
        f for f in tc_folder.iterdir()
        if f.is_file() and f.suffix.lower() in (_IMAGE_EXTS | _VIDEO_EXTS)
    )


def _upload_attachment(org: str, project: str, pat: str, file_path: Path) -> str:
    """Upload a local file to the ADO Attachments API.

    Returns the attachment URL that ADO assigned to the uploaded blob.
    This URL can be used as an <img src> in HTML fields or as the target
    of an AttachedFile work-item relation.
    """
    url = (
        f"https://dev.azure.com/{org}/{project}/_apis/wit/attachments"
        f"?fileName={file_path.name}&api-version=7.0"
    )
    r = requests.post(
        url,
        headers={"Content-Type": "application/octet-stream"},
        auth=HTTPBasicAuth("", pat),
        data=file_path.read_bytes(),
        timeout=120,
    )
    if r.status_code in (200, 201):
        return r.json()["url"]
    raise Exception(
        f"Attachment upload failed (HTTP {r.status_code}): {r.text[:300]}"
    )


def _attach_file_to_wi(base: str, pat: str, wi_id: str,
                        att_url: str, filename: str) -> None:
    """Add a pre-uploaded ADO attachment as an AttachedFile relation on a work item.

    Uses the JSON Patch endpoint so the existing work item is not replaced.
    """
    _ado_req(
        "patch", f"{base}/wit/workItems/{wi_id}?api-version=7.0",
        pat,
        [{"op": "add", "path": "/relations/-", "value": {
            "rel": "AttachedFile",
            "url": att_url,
            "attributes": {"comment": f"Evidence: {filename}"},
        }}],
        patch_json=True,
    )


def _process_evidence_files(
        org: str, project: str, pat: str,
        evidence_files: list[Path],
) -> tuple[str, list[tuple[str, str]]]:
    """Upload evidence files and build the HTML snippet to embed in ADO HTML fields.

    For **images**: uploaded to ADO Attachments and embedded inline as <img> tags.
    For **videos**: uploaded to ADO Attachments but *not* attached here — the
    caller receives them as ``pending_videos`` and is responsible for adding
    the AttachedFile relation to the correct work item (which may not exist yet
    when this function runs, e.g. a bug that hasn't been created yet).

    Returns:
        html_snippet    — Ready-to-embed HTML block. Starts with an
                          "<b>Evidence</b>" heading, followed by <img> tags
                          for images and <p> text notes for videos.
                          Returns "" if all uploads fail and nothing was added.
        pending_videos  — List of (att_url, filename) tuples for video files
                          that were uploaded but need an AttachedFile relation
                          added to a work item by the caller.
    """
    parts:          list[str]             = ["<p><b>Evidence</b></p>"]
    pending_videos: list[tuple[str, str]] = []

    for f in evidence_files:
        try:
            att_url = _upload_attachment(org, project, pat, f)
            if f.suffix.lower() in _IMAGE_EXTS:
                parts.append(f'<img src="{att_url}" alt="{escape(f.name)}">')
            else:
                # Video: add a text pointer now; caller will attach the file
                pending_videos.append((att_url, f.name))
                parts.append(
                    f'<p>📎 Video evidence attached: {escape(f.name)}</p>'
                )
        except Exception as e:
            _log(f"    ! Evidence upload failed ({f.name}): {e}", _C_WARN)
            parts.append(f'<p>⚠️ Evidence upload failed: {escape(f.name)}</p>')

    # Return empty string if nothing was appended beyond the heading
    html_snippet = "".join(parts) if len(parts) > 1 else ""
    return html_snippet, pending_videos


# ── ADO result updater ────────────────────────────────────────────────────────
_OUTCOME_MAP = {"pass": "Passed", "fail": "Failed", "failed": "Failed"}


def update_ado_results(org: str, project: str, pat: str,
                       plan_id: str, suite_id: str, evaluation: list[dict],
                       report_dir: Path | None = None) -> None:
    """Write evaluation results back to ADO test runs and post Discussion comments.

    When *report_dir* is provided, evidence files from each TC's
    ``TC_<id>`` sub-folder are uploaded and embedded in the Discussion
    comment: images inline as <img>, videos as AttachedFile relations on
    the TC work item with a text pointer in the comment body.
    """
    if not evaluation:
        _log("  No evaluation entries — nothing to update", _C_WARN)
        return

    base    = f"https://dev.azure.com/{org}/{project}/_apis"
    updated = skipped = 0

    # Resolve every evaluation entry to its ADO test point
    resolved: list[tuple[str, int, str, str | None]] = []
    for entry in evaluation:
        wi_id   = str(entry.get("id", ""))
        result  = (entry.get("result") or "").strip()
        reason  = (entry.get("reason") or "").strip()
        comment = (
            f"Evaluator Agent\n"
            f"Result: {result or 'N/A'}\n"
            f"Reason: {reason or '(no reason provided)'}"
        )
        ado_outcome: str | None = _OUTCOME_MAP.get(result.lower())
        try:
            resp = _ado_get(
                f"{base}/test/Plans/{plan_id}/Suites/{suite_id}/points"
                f"?testCaseId={wi_id}&$top=1&api-version=7.0", pat)
        except Exception as e:
            _log(f"    #{wi_id}  skipped — could not fetch test point: {e}", _C_WARN)
            skipped += 1
            continue
        points = resp.get("value", [])
        if not points:
            _log(f"    #{wi_id}  skipped — no test point in suite #{suite_id}", _C_WARN)
            skipped += 1
            continue
        resolved.append((wi_id, int(points[0]["id"]), comment, ado_outcome))

    if not resolved:
        _log("  No resolvable test points — nothing to write to ADO", _C_WARN)
        _log(f"  ✓ ADO update — 0 updated, {skipped} skipped")
        return

    run_entries   = [(wi, pid, cmt, out) for wi, pid, cmt, out in resolved if out is not None]
    other_entries = [(wi, pid, cmt, out) for wi, pid, cmt, out in resolved if out is None]

    if other_entries:
        _log(f"  {len(other_entries)} non-pass/fail TC(s) will be left Active "
             f"(Discussion comment only)", _C_DIM)

    if not run_entries:
        _log("  No pass/fail results — skipping test run creation")
    else:
        point_ids = [pid for _, pid, _, _ in run_entries]
        _log(f"  Creating new test run for {len(point_ids)} pass/fail test case(s) …")
        try:
            run = _ado_req("post", f"{base}/test/runs?api-version=7.0", pat, {
                "name":     f"Evaluator Agent - {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "plan":     {"id": str(plan_id)},
                "pointIds": point_ids,
            })
        except Exception as e:
            _log(f"  ! could not create test run — {e}", _C_WARN)
            _log("    Discussion comments will still be posted below", _C_WARN)
            run_entries = []
        else:
            new_run_id: int = run["id"]
            _log(f"  ✓ Created run #{new_run_id}")
            try:
                res = _ado_get(f"{base}/test/runs/{new_run_id}/results?api-version=7.0", pat)
            except Exception as e:
                _log(f"  ! could not fetch results for run #{new_run_id} — {e}", _C_WARN)
                run_entries = []
            else:
                by_tc = {wi: (cmt, out) for wi, _, cmt, out in run_entries}
                patches = []
                for r in res.get("value", []):
                    tc_id = str(r.get("testCase", {}).get("id", ""))
                    if tc_id not in by_tc:
                        continue
                    cmt, out = by_tc[tc_id]
                    patches.append({"id": r["id"], "outcome": out,
                                    "state": "Completed", "comment": cmt})
                    _log(f"    #{tc_id}  → {out}  (comment recorded, state = Completed)")
                if not patches:
                    _log("  ! No matching result stubs — nothing patched", _C_WARN)
                else:
                    try:
                        _ado_req("patch",
                                 f"{base}/test/runs/{new_run_id}/results?api-version=7.0",
                                 pat, patches)
                        updated = len(patches)
                    except Exception as e:
                        _log(f"  ! could not patch results for run #{new_run_id} — {e}", _C_WARN)
                    try:
                        _ado_req("patch", f"{base}/test/runs/{new_run_id}?api-version=7.0",
                                 pat, {"state": "Completed"})
                        _log(f"  ✓ Run #{new_run_id} state → Completed")
                    except Exception as e:
                        _log(f"  ! could not close run #{new_run_id} — {e}", _C_WARN)

    # Post Discussion comment (with inline evidence) to every TC regardless of outcome
    wi_commented = wi_comment_failed = 0
    _log("  Posting Discussion comments to test case work items …")
    for wi_id, _, comment, ado_outcome in resolved:
        tag = ado_outcome if ado_outcome else "Active (no run result)"
        try:
            ev_html: str                          = ""
            pending_videos: list[tuple[str, str]] = []
            evidence_files = _get_tc_evidence(report_dir, wi_id)
            if evidence_files:
                _log(f"    #{wi_id}  uploading {len(evidence_files)} evidence file(s) …")
                ev_html, pending_videos = _process_evidence_files(
                    org, project, pat, evidence_files)
                # Attach video files to the TC work item now that we know the wi_id
                for att_url, filename in pending_videos:
                    try:
                        _attach_file_to_wi(base, pat, wi_id, att_url, filename)
                        _log(f"    #{wi_id}  📎 video attached: {filename}")
                    except Exception as ve:
                        _log(f"    #{wi_id}  ! video attach failed ({filename}): {ve}", _C_WARN)

            # Build final comment: plain-text header + HTML evidence block
            comment_html = comment.replace("\n", "<br>") + ev_html
            _ado_req("post",
                     f"{base}/wit/workItems/{wi_id}/comments?api-version=7.0-preview.3",
                     pat, {"text": comment_html})
            ev_note = f"  (+{len(evidence_files)} evidence)" if evidence_files else ""
            _log(f"    #{wi_id}  ✓ Discussion comment posted  [{tag}]{ev_note}")
            wi_commented += 1
        except Exception as e:
            _log(f"    #{wi_id}  ! Discussion comment failed — {e}", _C_WARN)
            wi_comment_failed += 1

    if wi_comment_failed:
        _log(f"  ! {wi_comment_failed} Discussion comment(s) failed "
             f"(run results were still written above)", _C_WARN)
    _log(f"  ✓ ADO update — {updated} run result(s) updated, "
         f"{wi_commented} Discussion comment(s) posted, "
         f"{skipped} entry/entries skipped")

# ── Bug creator ───────────────────────────────────────────────────────────────

def _fetch_pat_owner(org: str, pat: str) -> str | None:
    # Primary: connectionData — works with any valid PAT scope
    try:
        r = requests.get(f"https://dev.azure.com/{org}/_apis/connectionData",
                         headers={"Accept": "application/json"},
                         auth=HTTPBasicAuth("", pat), timeout=15)
        if r.status_code == 200:
            name = (r.json().get("authenticatedUser", {})
                    .get("providerDisplayName") or "").strip()
            if name:
                return name
        else:
            _log(f"  ! connectionData returned HTTP {r.status_code}", _C_WARN)
    except Exception as e:
        _log(f"  ! connectionData lookup failed — {e}", _C_WARN)
    # Fallback: VSTS Profile API (requires User Profile scope)
    try:
        r = requests.get(
            "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=6.0",
            headers={"Accept": "application/json"},
            auth=HTTPBasicAuth("", pat), timeout=15)
        if r.status_code == 200:
            data = r.json()
            return ((data.get("emailAddress") or data.get("displayName") or "") or None)
        else:
            _log(f"  ! profile API returned HTTP {r.status_code}", _C_WARN)
    except Exception as e:
        _log(f"  ! profile API lookup failed — {e}", _C_WARN)
    return None


def _build_repro_steps_html(description: str, steps: list[str],
                             expected_result: str, actual_result: str) -> str:
    steps_block = (f"<ol>{''.join(f'<li>{escape(s)}</li>' for s in steps)}</ol>"
                   if steps else "<p></p>")
    return (
        f"<p><b>Description,</b></p><p>{escape(description or '')}</p>"
        f"<p><b>Steps to repro,</b></p>{steps_block}"
        f"<p><b>Expected result,</b></p><p>{escape(expected_result or '')}</p>"
        f"<p><b>Actual result,</b></p><p>{escape(actual_result or '')}</p>"
    )


def _fetch_parent_story_ids(base: str, pat: str, tc_id: str) -> list[str]:
    resp = _ado_get(f"{base}/wit/workitems/{tc_id}?$expand=relations&api-version=7.0", pat)
    ids = []
    for rel in resp.get("relations", []):
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            m = re.search(r"/workItems/(\d+)$", rel.get("url", ""), re.IGNORECASE)
            if m:
                ids.append(m.group(1))
    return ids


def _fetch_iteration_path(base: str, pat: str, wi_id: str) -> str | None:
    try:
        resp = _ado_get(
            f"{base}/wit/workitems/{wi_id}?fields=System.IterationPath&api-version=7.0", pat)
        return resp.get("fields", {}).get("System.IterationPath") or None
    except Exception:
        return None


def _related_link(url: str, comment: str) -> dict:
    return {"op": "add", "path": "/relations/-", "value": {
        "rel": "System.LinkTypes.Related", "url": url,
        "attributes": {"comment": comment},
    }}


def create_bugs_for_failures(org: str, project: str, pat: str,
                              evaluation: list[dict],
                              report_dir: Path | None = None) -> None:
    """Create ADO Bug work items for every failed test case in *evaluation*.

    When *report_dir* is provided, evidence files from the TC's
    ``TC_<id>`` sub-folder are appended to the bug's ReproSteps HTML field:
    images inline as <img>, videos as AttachedFile relations on the bug with
    a text pointer in the ReproSteps body.
    """
    if not evaluation:
        return

    base        = f"https://dev.azure.com/{org}/{project}/_apis"
    wi_url_root = f"https://dev.azure.com/{org}/_apis/wit/workItems"

    assigned_to = _fetch_pat_owner(org, pat)
    if assigned_to:
        _log(f"  Assigning bugs to : {assigned_to}")
    else:
        _log("  ! Could not resolve PAT owner — bugs will be unassigned", _C_WARN)

    created = skipped = 0

    for entry in evaluation:
        result = (entry.get("result") or "").strip().lower()
        if result not in ("fail", "failed"):
            continue

        tc_id       = str(entry.get("id", ""))
        bug_details = entry.get("bug_details")
        if not bug_details:
            _log(f"    #{tc_id}  skipped bug creation — "
                 f"result is '{result}' but no bug_details key found", _C_WARN)
            skipped += 1
            continue

        title = (bug_details.get("title") or "").strip()
        if not title:
            _log(f"    #{tc_id}  skipped bug creation — bug_details.title is empty", _C_WARN)
            skipped += 1
            continue

        try:
            priority = int(str(bug_details.get("priority", "2")).strip())
        except (ValueError, TypeError):
            priority = 2

        full_title = f"[Evaluator Agent] {title}"
        repro_html = _build_repro_steps_html(
            (bug_details.get("description") or "").strip(),
            bug_details.get("steps_to_reproduce") or [],
            (bug_details.get("expected_result") or "").strip(),
            (bug_details.get("actual_result") or "").strip(),
        )

        # Upload evidence now (before the bug exists) so inline images can be
        # embedded in repro_html.  Videos are deferred: we store their upload
        # URLs in bug_videos and attach them to the bug after it is created.
        evidence_files              = _get_tc_evidence(report_dir, tc_id)
        bug_videos: list[tuple[str, str]] = []
        if evidence_files:
            _log(f"    #{tc_id}  uploading {len(evidence_files)} evidence file(s) for bug …")
            ev_html, bug_videos = _process_evidence_files(
                org, project, pat, evidence_files)
            repro_html += ev_html

        # Fetch parent stories for iteration path + linkage
        parent_story_ids: list[str] = []
        iteration_path: str | None = None
        if tc_id:
            try:
                parent_story_ids = _fetch_parent_story_ids(base, pat, tc_id)
                if parent_story_ids:
                    _log(f"    #{tc_id}  parent story(ies): "
                         f"{', '.join('#' + s for s in parent_story_ids)}")
                    iteration_path = _fetch_iteration_path(base, pat, parent_story_ids[0])
                    if iteration_path:
                        _log(f"    #{tc_id}  iteration path  : {iteration_path}")
                    else:
                        _log(f"    #{tc_id}  ! could not read parent story iteration "
                             f"— bug will use project default", _C_WARN)
                else:
                    _log(f"    #{tc_id}  ! no parent story found "
                         f"— bug will not be linked to a story", _C_WARN)
            except Exception as e:
                _log(f"    #{tc_id}  ! parent story lookup failed — {e} "
                     f"— continuing without story link", _C_WARN)

        patch_doc: list[dict] = [
            {"op": "add", "path": "/fields/System.Title",                   "value": full_title},
            {"op": "add", "path": "/fields/Microsoft.VSTS.TCM.ReproSteps",  "value": repro_html},
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority},
        ]
        if assigned_to:
            patch_doc.append({"op": "add", "path": "/fields/System.AssignedTo",    "value": assigned_to})
        if iteration_path:
            patch_doc.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})
        if tc_id:
            patch_doc.append(_related_link(f"{wi_url_root}/{tc_id}", "Linked by Evaluator Agent"))
        for story_id in parent_story_ids:
            patch_doc.append(_related_link(f"{wi_url_root}/{story_id}",
                                           "Linked by Evaluator Agent (parent story)"))

        try:
            bug    = _ado_req("post", f"{base}/wit/workitems/$Bug?api-version=7.0",
                              pat, patch_doc, patch_json=True)
            bug_id = bug.get("id", "?")
            _log(f"    #{tc_id}  → Bug #{bug_id} created"
                 f"  (priority {priority})  {full_title}")
            created += 1
            # Now that the bug exists, attach any video evidence files to it
            for att_url, filename in bug_videos:
                try:
                    _attach_file_to_wi(base, pat, str(bug_id), att_url, filename)
                    _log(f"    #{tc_id}  → Bug #{bug_id}  📎 video attached: {filename}")
                except Exception as ve:
                    _log(f"    #{tc_id}  ! video attach to Bug #{bug_id} "
                         f"failed ({filename}): {ve}", _C_WARN)
        except Exception as e:
            _log(f"    #{tc_id}  ! Bug creation failed — {e}", _C_WARN)
            skipped += 1

    _log(f"  ✓ Bug creation — {created} bug(s) created, {skipped} skipped")