# TC Evaluator

Fetches test cases from Azure DevOps and executes them via Claude Code + Playwright MCP.

---

## Code Flow

```
POST /evaluator
      │
      ├─ Pre-flight    →  Claude Code installed? @playwright/mcp configured?
      ├─ ADO Fetch     →  Pull test cases (title + steps) from plan/suite
      ├─ Prompt        →  Inject TCs into evaluator.md
      ├─ Claude Code   →  Runs prompt headlessly, drives Playwright MCP in browser
      ├─ Result        →  evaluation.json written → returned via SSE / poll
      └─ ADO Write-back
            ├─ Pass / Fail  →  New test run created, outcome recorded,
            │                  run closed as Completed
            └─ All TCs      →  Discussion comment posted to each test case
                               (Result + Reason, regardless of outcome)
```

---

## Setup

### 1. Python

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # Mac/Linux
pip install -r requirements.txt
```

### 2. Claude Code

```bash
npm install -g @anthropic-ai/claude-code
claude          # run once to authenticate
```

### 3. Playwright MCP

```bash
claude mcp add playwright -- npx @playwright/mcp@latest
```

---

## Run

```bash
python app.py   # starts on http://localhost:5001
```

---

## API

```bash
# Start job
curl -X POST http://localhost:5001/evaluator \
  -H "Content-Type: application/json" \
  -d '{"org":"…","project":"…","pat":"…","plan_id":"123","suite_id":"456"}'

# Stream live logs
curl -N http://localhost:5001/evaluator/logs/<job_id>

# Poll result
curl http://localhost:5001/evaluator/result/<job_id>
```

---

## Config

Edit at the top of `mainframe.py`:

| `CLAUDE_MODEL` | `claude-opus-4-8` | `sonnet-4-5` for faster/cheaper |

---

## ADO PAT Permissions

| Scope | Permission |
|---|---|
| **Test Management** | Read & Write → fetch test cases · create runs · record outcomes |
| **Work Items** | Read & Write → fetch titles & steps · post Discussion comments |