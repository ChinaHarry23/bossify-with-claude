---
name: bossify-with-claude
description: 新时代老板 (Bossify with Claude) — local-first AI-productivity auditor that treats token spend like capital allocation and classifies it as HIGH_VALUE / TRANSIENT_VALUE / LOW_VALUE / WASTED. Use when a user (especially a manager) wants to audit Claude Code usage — their own or their team's — measure where tokens went, get a harsh content-aware verdict from a local LLM (LM Studio / Ollama) on whether the work was meaningful or wasteful, see a manager-oriented dashboard with per-employee cards and waste leaderboards, or replay/attribute a session down to individual events. Triggers include "bossify", "new-era boss", "新时代老板", "token ROI", "token attribution", "who is wasting tokens", "monitor my team's AI usage", "audit employee Claude usage", "which prompts were wasted", "compress my memory", "replay that session", "show me the dashboard", "团队 token 使用", "员工 AI 效率", "哪些会话在浪费 token". Supports English + Simplified Chinese (`--locale zh` or `TOKEN_ROI_LOCALE=zh`). Works in three modes: (A) wired into Claude Code via hooks to capture live sessions, (B) as an Agent SDK wrapper, or (C) as a retrospective importer for existing `~/.claude/projects/*.jsonl` history.
---

# Bossify with Claude · 新时代老板

A local-first platform that treats agentic token usage like **capital allocation** — records every session losslessly, asks a **local LLM to harshly judge** whether each prompt was worth its tokens, and surfaces the results in a **manager-oriented dashboard** with employee cards, waste leaderboards, and drill-down.

## What this skill does

1. **Records losslessly.** Every user prompt, assistant response, tool call, tool output, file I/O, memory read/write, and retrieval is appended to JSONL under `data/raw_events/`. Nothing is discarded.
2. **Imports existing history.** Retrospectively ingests `~/.claude/projects/<slug>/*.jsonl` so there's something to analyse on day one.
3. **Compresses memory separately.** A memory layer (`MEMORY.md` + `data/memory/topics/*.md`) is maintained as a *derived, lossy* index — never treated as ground truth.
4. **Retrieves over both layers.** Hybrid retrieval (embeddings + keyword) runs against raw events and compressed memory, with retrieval outcomes logged for ROI attribution.
5. **Attributes tokens.** Every token charged to a prompt is traced forward through the event graph: what it produced, what was kept, what was retrieved later.
6. **Asks a local LLM to judge.** Per-prompt and per-session calls into LM Studio / Ollama with a deliberately adversarial prompt that anchors scores around 0.5 (not 0.8), demands concrete waste evidence, and emits findings as `wasteful_patterns` chips. Outputs Chinese reasoning when `--locale zh`.
7. **Scores ROI.** Each prompt, session, tool-chain, and memory write gets classified as `HIGH_VALUE`, `TRANSIENT_VALUE`, `LOW_VALUE`, or `WASTED`. The LLM verdict overrides the math formula when present.
8. **Groups by employee.** A configurable `data/employees.json` maps Claude Code project slugs → employees (name / role / team). One card per employee in the manager dashboard.
9. **Surfaces it in 3 dashboard tabs:** **Team Overview** (boss-first KPIs + top waste patterns + leaderboard), **Employees** (card grid), **Advanced** (the original developer-facing technical charts).

## When to invoke

Invoke this skill when the user asks anything of the form:

**Personal token audit:**
- "Where did my tokens go last session?"
- "Which prompts were wasted?"
- "Why is my agent burning tokens?"
- "Replay session X"

**Management / team audit (primary use case now):**
- "Help me audit my team's Claude usage"
- "Which employee is most productive with AI?"
- "Show me the waste patterns across my team"
- "Monitor my engineers' token spend"
- "哪些员工的 AI 使用效率最低？"
- "帮我看看团队的 token 消耗情况"

**Operational:**
- "Compress my memory / rebuild MEMORY.md"
- "Show me the dashboard"

Also invoke proactively when you observe obvious capital destruction in a live session — same large file read 6 times, memory writes never retrieved across 20 sessions, etc.

## How to use this skill

The skill is a Python 3.11+ package. Claude should drive it by running the CLI, not by reimplementing logic inline.

### First-time setup

```bash
cd bossify-with-claude
python -m pip install -e '.[dashboard,embeddings]'
token-roi init
```

### Import Claude Code history (retrospective audit)

```bash
token-roi import claude-code                    # pulls ~/.claude/projects/*/*.jsonl
token-roi score                                  # runs attribution + ROI classifier
token-roi judge --model zai-org/glm-4.7-flash    # local-LLM judges every prompt
token-roi name-sessions                          # short human-readable session names
token-roi dashboard                              # opens http://127.0.0.1:8787
```

### Management workflow (Chinese boss)

```bash
# 1. Define your team in data/employees.json (see below).
# 2. Import + score + judge everything in Chinese:
token-roi --locale zh import claude-code
token-roi --locale zh score
token-roi --locale zh judge
token-roi --locale zh name-sessions
# 3. Open the boss dashboard:
token-roi --locale zh dashboard
```

`data/employees.json` format:

```json
{
  "employees": {
    "alice":     {"name": "王丽", "role": "高级工程师", "team": "平台组"},
    "chinaharry":{"name": "陈俊", "role": "全栈工程师", "team": "平台组"}
  },
  "project_to_employee": {
    "-Users-alice-work-project":    "alice",
    "-Users-chinaharry-...-Bossify":"chinaharry"
  },
  "default_employee": "chinaharry"
}
```

If no config file exists, one employee is synthesized from `getpass.getuser()`.

### Live capture (Mode A — Claude Code hooks)

```bash
token-roi install-hooks --settings ~/.claude/settings.json
```

Registers `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` hooks that emit events into `data/raw_events/<date>/session_<id>.jsonl` as you use Claude Code. Idempotent + backs up the existing settings.json.

### Live capture (Mode B — Agent SDK wrapper)

```python
from token_roi.sdk_wrapper import InstrumentedClient
client = InstrumentedClient(data_dir="./data")
# Use `client.messages.create(...)` like the Anthropic SDK — events are captured transparently.
```

### Full CLI surface

```bash
# Pipeline
token-roi init
token-roi import claude-code [--from PATH] [--project SLUG_SUBSTRING]
token-roi score [--session ID | --since 7d]
token-roi judge [--model ID] [--force] [--limit N] [--session ID]
token-roi name-sessions [--force]
token-roi compress [--max-topics N] [--since DUR]
token-roi index-memory [--include-events]

# Query / audit
token-roi query "TEXT..." [--top-k N]
token-roi replay --session ID [--from EVENT_ID]
token-roi explain --kind {prompt|session|memory_write} --id ID
token-roi attribute --event EVENT_ID

# Lists
token-roi sessions                           # per-session table
token-roi roi-summary                        # count by ROI class
token-roi view {top_spenders.sql|orphan_memory.sql|black_holes.sql}
token-roi employees list                     # team rollup
token-roi employees show <id>                # employee drill-down

# Dashboard
token-roi dashboard [--host HOST] [--port PORT]

# Cross-cutting flags (work with any subcommand)
token-roi --locale {en|zh} ...               # switch UI + LLM output language
token-roi --data-dir PATH ...                # point at non-default data dir
token-roi --otel ...                         # export OpenTelemetry spans
```

## Design principles (do not violate)

1. **Lossless storage is ground truth.** `data/raw_events/**/*.jsonl` is append-only and never rewritten in place.
2. **Compressed memory is not the source of truth.** `MEMORY.md` and topic files are caches — rebuildable from raw events.
3. **Retrieval determines practical value.** Memory writes that are never retrieved count as storage cost, not value.
4. **ROI is not token count.** A cache-dominated 30M-token flow may cost less than a 500K-token turn of pure assistant narration.
5. **The LLM judge must be harsh.** Scoring anchors default around 0.5; above 0.8 is reserved for genuinely exceptional work. The judge **must** name specific `wasteful_patterns` — even HIGH_VALUE work has inefficiencies.
6. **Every score is explainable.** Every ROI number traces to events, memory writes, retrieval hits, outcomes, and (where present) the LLM's own reasoning. If you can't trace it, it's invalid.
7. **Local-first.** No cloud. Embeddings via sentence-transformers / ollama / hash fallback. Dashboard = localhost. LLM judge = LM Studio / Ollama (`http://localhost:1234/v1` by default).
8. **Auditor-grade.** The skill exists to support management audit. It refuses to invent numbers when raw logs are missing, and it doesn't sugar-coat the LLM's verdict.

## Architecture

```
  Claude Code hooks ─┐
  SDK wrapper        ├──▶ EventStore (append-only JSONL)  ◀── canonical truth
  JSONL importer ────┘           │
                                  ▼
                         AnalyticsDB (SQLite, rebuildable)
                                  │
            ┌───────────────┬─────┴─────┬──────────────┐
            ▼               ▼           ▼              ▼
      Attribution    Retrieval    Compression    LLM Judge (LM Studio)
      (per-prompt)   (hybrid)     (MEMORY.md)   • meaningful_value
            │                                    • code_quality
            ▼                                    • output_durability
      ROI Classifier  ◀── LLM verdict override  • efficiency
      HIGH/TRANS/LOW/WASTED                     • wasteful_patterns[]
            │
            ├─ per prompt
            ├─ per session          ┌──────────────────────────────┐
            └─ per memory write     │ data/employees.json overlay  │
                                    │ project_slug → employee_id   │
                                    └──────────────────────────────┘
                                                 │
                                                 ▼
                                    Dashboard (FastAPI + ECharts, i18n)
                                    ├── Team Overview  (boss landing)
                                    ├── Employees      (card grid)
                                    └── Advanced       (developer charts)
```

## Progressive disclosure

These files live in `references/` and should be read on demand, not eagerly:
- [references/architecture.md](references/architecture.md) — module boundaries, data flow, failure modes
- [references/schemas.md](references/schemas.md) — every event shape + SQLite DDL
- [references/roi-model.md](references/roi-model.md) — ROI formula + LLM override bands
- [references/examples.md](references/examples.md) — worked examples with real numbers
- [references/hooks.md](references/hooks.md) — hook integration details for Claude Code

## Code layout

```
bossify-with-claude/
├── SKILL.md                         (this file)
├── README.md · README.zh.md · pyproject.toml
├── src/token_roi/
│   ├── events.py  storage.py  db.py
│   ├── memory.py  retrieval.py  compression.py
│   ├── attribution.py  roi.py   replay.py
│   ├── llm_judge.py                 (LM Studio / Ollama judge)
│   ├── employees.py                 (employee registry + config loader)
│   ├── i18n.py                      (en + zh translation dicts)
│   ├── hooks.py  sdk_wrapper.py  telemetry.py
│   ├── cli.py                       (all CLI subcommands)
│   ├── importers/claude_code.py     (~/.claude/projects/ importer)
│   └── dashboard/
│       ├── server.py                (FastAPI + all /api/* endpoints)
│       ├── templates/index.html     (3 tabs: team / employees / advanced)
│       └── static/ app.css · app.js · app.manager.js · echarts.min.js
├── hooks/                           (installed into Claude Code settings.json)
├── references/                      (progressive-disclosure docs)
└── tests/                           (pytest; 22 tests covering core paths)
```

## What this skill does NOT do

- It does not call cloud APIs for scoring. All scoring is local (LM Studio / Ollama).
- It does not sugar-coat the LLM verdict. The prompt is adversarial; scores anchor around 0.5, not 0.8.
- It does not invent scores. If raw events are missing for a session, the skill refuses rather than approximates.
- It does not modify `MEMORY.md` without an explicit `compress` invocation.
- It does not send any data over the network (other than to the configured local LLM endpoint).
