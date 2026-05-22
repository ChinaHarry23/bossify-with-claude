<div align="center">

# bossify.claude · 新时代老板

> *"You AI guys keep telling me agentic coding is cheap — then why does my token bill look like a Series A?"*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Skill-blueviolet)](https://claude.ai/code)
[![Local-First](https://img.shields.io/badge/Local--First-100%25-brightgreen)](#)
[![Bilingual](https://img.shields.io/badge/i18n-EN%20%C2%B7%20%E4%B8%AD%E6%96%87-ff69b4)](README.zh.md)

<br>

Your engineer spent 4 hours and 2M tokens to rename a variable?<br>
Your intern's Claude sessions read like a Dostoyevsky novel with zero commits?<br>
Your MEMORY.md keeps growing but nobody ever reads it back?<br>
Your "AI-accelerated" sprint somehow shipped less than the old one?<br>
Your finance team is asking, very politely, what a "black hole session" is?<br>

**Turn vibe-coding into accountable capital allocation — welcome to the new boss era!**

<br>

Point it at your team's `~/.claude/projects/`<br>
let a local LLM judge every prompt *harshly*<br>
and get a **boss-facing dashboard that names names**

[What It Is](#what-it-is) · [Install](#install) · [Quick Start](#personal-audit--quick-start) · [Demo](#demo) · [How Scoring Works](#how-scoring-works) · [Architecture](#architecture)

[**中文 README**](README.zh.md)

</div>

---

> 🆕 **2026.04 Update** — **Team Audit mode is live!** Per-employee cards, waste leaderboards, and drill-down to the exact prompts that burned tokens for nothing. Configure via `data/employees.json`.

> 🔒 **Local-first, always.** Every prompt, tool call, and LLM verdict stays on your machine. LM Studio / Ollama only. No cloud, no telemetry, no "we promise we won't peek."

---

Created by [@chinaharry](https://github.com/chinaharry) 

## What it is

Bossify treats agentic token usage as **capital allocation** and asks a local LLM to judge, without mercy, whether each prompt was worth what it cost.

| Pillar | Translation |
|--------|-------------|
| 👔 **Boss-first** | Per-employee cards, waste leaderboards, drill-down to the exact prompts that burned tokens for nothing |
| 😈 **Harsh, not flattering** | Local LLM anchors scores around **0.5**, not 0.8. `HIGH_VALUE` is reserved for genuinely exceptional work. Every verdict carries concrete `wasteful_patterns` evidence |
| 🔍 **Local-first & auditable** | Every user prompt, tool call, tool output, memory write, retrieval hit → appended losslessly to JSONL. Every ROI number traces back to the events that produced it. Nothing is invented |
| 🌏 **Bilingual** | Full EN and 简体中文 UI, LLM prompts, and CLI help (`--locale zh` or `TOKEN_ROI_LOCALE=zh`) |

## The dashboard

Four tabs, served locally at **http://127.0.0.1:8787**:

| Tab | What it shows |
|---|---|
| 📊 **Overview** / 总览 | Hero KPIs in **USD** (priced per-model), cost-per-KB-shipped, team ROI distribution, top waste patterns, biggest-money-burner leaderboard |
| 🧑‍💻 **People** / 成员 | Card grid — one per person with ROI mix, top sessions, waste examples |
| 📁 **Projects** / 项目 | One card per workspace, ranked by USD spend, with **model-mix pills** (Opus / Sonnet / Haiku share), cost-per-KB, and ROI distribution |
| 🔬 **Technical detail** / 技术细节 | Token flow over time, memory effectiveness scatter, tool treemap, per-prompt LLM verdicts, session drill-down |

Click any project card, employee card, or leaderboard row to drill into a full session-by-session breakdown with USD cost + per-session model mix.

---

## Install

> **Prerequisites**
> - Python **3.11+**
> - A local LLM server — [LM Studio](https://lmstudio.ai/) or [Ollama](https://ollama.com/) — running on `http://localhost:1234/v1` with at least one instruct model loaded (a 4–8B model is plenty for the judge; GLM-4.x and Gemma-3 work well).
> - (Optional) **Claude Code** already installed and in use — Bossify reads its session history from `~/.claude/projects/`.

```bash
git clone https://github.com/chinaharry/bossify-with-claude
cd bossify-with-claude
python -m pip install -e '.[dashboard,embeddings]'
token-roi init
```

Optional extras: `otel` (OpenTelemetry export), `anthropic` (Agent SDK wrapper), `dev` (pytest).

### Supported sources

`token-roi import <source>` pulls session history from any of:

- **claude-code** — JSONL under `~/.claude/projects/`. Full token usage, tool calls, and file-edit events. This is the reference importer.
- **codex** — OpenAI Codex CLI session logs under `~/.codex/sessions/`. Maps `message` / `function_call` / `function_call_output` / `token_count` records into typed events. Schema varies across Codex versions; the importer is lenient and skips unknown record types.
- **cursor** — Cursor IDE chat history from the `state.vscdb` SQLite stores under Cursor's user directory. **Caveat:** Cursor sessions typically lack token usage unless you run Cursor in API-key / OpenRouter mode that records usage — imported events will have zero token attribution otherwise.
- **aider** — `.aider.chat.history.md` transcripts, enriched with token counts from a sibling `.aider.llm.history` when present. Fenced code blocks whose info-string looks like a file path are promoted to `FILE_WRITE` events.
- **openai-jsonl** — Generic OpenAI Responses API / Chat Completions JSONL logs. Reads `input` / `output` / `usage` per line and emits the corresponding user / assistant / tool-call events.

---

## First run — full pipeline walkthrough

Brand new user? Run these five commands in order. The whole thing is local-first — no prompts, code, or tokens leave your machine. Expect roughly 15–30 seconds of LLM work per prompt on a mid-range laptop, so the `judge` step is the long pole on a big history.

```bash
# 1. Pull every Claude Code session you've ever run into the event store.
#    Idempotent — safe to re-run any time new sessions land.
token-roi import claude-code

# Also supported:
token-roi import codex                    # OpenAI Codex CLI (~/.codex/sessions)
token-roi import cursor                   # Cursor IDE chat history
token-roi import aider --from ~/projects  # Aider .aider.chat.history.md files
token-roi import openai-jsonl --from path/to/log.jsonl

# 2. Re-build the analytics index from raw events, run attribution, and
#    classify every prompt + session (HIGH_VALUE / TRANSIENT / LOW_VALUE /
#    WASTED). Also auto-cleans out Claude Code's slash-command plumbing
#    and re-computes LLM aggregates under the latest formula.
token-roi score

# 3. Ask the local LLM to read each prompt + its output and rate
#    meaningful value, durability, code quality, and efficiency.
#    Replace the --model id with whatever you have loaded in LM Studio /
#    Ollama. --list-models will print what's available.
token-roi judge --model glm-4.7-flash

# 4. Roll the judgments back into ROI scores (the judge's verdict
#    overrides the pure-math score when present).
token-roi score

# 5. Two LLM labelling passes — one per session, one per project.
#    Both are idempotent; re-running skips already-named rows.
token-roi name-sessions                  # human-readable session titles
token-roi name-projects                  # LLM names your Claude Code workspaces

# 6. Open the boss dashboard.
token-roi dashboard                      # → http://127.0.0.1:8787
```

After this you'll land on the Overview tab with your real USD spend, cost-per-KB-shipped, ROI distribution, and the biggest-money-burner leaderboard. Switch to the Projects tab to see how your spend splits across workspaces + models.

> **Prefer 中文 UI?** Prefix any command with `--locale zh` (or set `TOKEN_ROI_LOCALE=zh`) — the LLM judge and dashboard both switch to Simplified Chinese.

### After a new coding session

When you've done more Claude Code work and want to refresh the dashboard:

```bash
token-roi import claude-code && token-roi score && token-roi judge && token-roi score
token-roi name-sessions && token-roi name-projects
```

`import` and every LLM step are content-addressed — anything already done gets skipped.

### Starting over from scratch

If the derived data is in a broken state (corrupt DB, stale judgments from an older model, etc.), `token-roi nuclear` wipes everything under `data/` and re-runs the full pipeline. It preserves `employees.json` (briefly backed up to `/tmp`) and your Claude Code history (it lives in `~/.claude/projects/`, which Bossify never writes to).

```bash
token-roi nuclear --model glm-5.1-ram-420gb-mlx          # prompts "Type 'yes' to proceed"
token-roi nuclear --yes --model glm-5.1-ram-420gb-mlx    # skip the confirmation
token-roi nuclear --yes --skip-judge --skip-name         # import + score only (fast)
```

Expect ~40 min of LLM work on a full history; the `--skip-judge` variant returns in seconds.

---

## Team audit — manager workflow

Once you have more than one person's history to review, define the team in `data/employees.json`:

```json
{
  "employees": {
    "alice": {"name": "Alice Wang", "role": "Senior Engineer", "team": "Platform"},
    "bob":   {"name": "Bob Chen",   "role": "Full-stack",       "team": "Platform"}
  },
  "project_to_employee": {
    "-Users-alice-work-project": "alice",
    "-Users-bob-side-project":   "bob"
  },
  "default_employee": "alice"
}
```

Point the importer at a directory that contains every teammate's `~/.claude/projects/` tree (e.g. a shared drive mount or a git-synced dir):

```bash
token-roi --locale zh import claude-code --from /path/to/team/claude-projects
token-roi --locale zh score
token-roi --locale zh judge
token-roi --locale zh name-sessions
token-roi --locale zh name-projects
token-roi --locale zh dashboard
```

### Audit commands

| Command | Description |
|---------|-------------|
| `token-roi employees list` | Per-employee rollup (USD, sessions, ROI mix) |
| `token-roi employees show alice` | Drill into one person |
| `token-roi view black_holes.sql` | Sessions with the lowest value-per-token |
| `token-roi view orphan_memory.sql` | Memory writes that are never re-read |
| `token-roi explain --kind session --id <ID>` | Full derivation of one score — the exact events, memory writes, and LLM reasoning behind a verdict |
| `token-roi name-projects --force` | Re-generate project names (e.g. after editing the `PROJECT_SUMMARY_SYSTEM_PROMPT`) |

---

## Demo

> Input: a 47-minute session, 1.8M tokens, zero commits

**Scenario 1: The Black Hole**

```
Bossify       ❯ Class: WASTED · score 0.12
                wasteful_patterns:
                  - 23 tool calls produced no diff
                  - assistant wrote 4,000 words of plan, user never approved
                  - MEMORY.md grew by 6KB, never retrieved in subsequent sessions
                verdict: "Long rambling planning session with no durable output.
                          Tokens are gone."
```

**Scenario 2: The Polite Disagreement**

```
Engineer      ❯ But I was exploring the architecture, that's valuable thinking!

Bossify       ❯ Exploration that produces no artifact, no commit, and no
                retrieval hit is storage cost, not value. Class stays WASTED.
                (See: Design Principle #3.)
```

---

## How scoring works

Each prompt, session, tool-chain, and memory write is classified as one of:

| Class | Meaning |
|---|---|
| 🟢 `HIGH_VALUE` | Durable output that was reused in later sessions. Rare by design. |
| 🟡 `TRANSIENT_VALUE` | Produced output, but it wasn't reused. Still OK. |
| 🟠 `LOW_VALUE` | Poor value per token — long narration, little durable output. |
| 🔴 `WASTED` | Zero durable return. The tokens are gone. |

The math formula is a starting point. **The local LLM verdict overrides it** — if the judge reads the actual content and decides "this is rambling assistant narration with no code produced," the class drops to `WASTED` regardless of what the token ratios say.

The judge prompt is deliberately adversarial: anchored around 0.5 (not 0.8), demands concrete `wasteful_patterns` evidence, told not to be charitable. Even `HIGH_VALUE` work must surface at least one efficiency finding.

---

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
      (per-prompt)   (hybrid)     (MEMORY.md)     adversarial rubric
            │                                          │
            └────────────▶ ROI Classifier ◀────────────┘
                          HIGH / TRANS / LOW / WASTED
                                    │
                                    ▼
                         Dashboard (FastAPI + ECharts)
                         团队总览 · 员工 · 高级
```

See [SKILL.md](SKILL.md) for the full CLI surface and [references/](references/) for architecture notes, schemas, and the ROI model.

---

## Design principles

1. 🧾 **Lossless storage is ground truth.** `data/raw_events/**/*.jsonl` is append-only and never rewritten in place.
2. 🧠 **Compressed memory is not the source of truth.** `MEMORY.md` is a cache, rebuildable from raw events.
3. 🎯 **Retrieval determines practical value.** Memory writes never retrieved count as storage cost, not value.
4. 😈 **The LLM judge must be harsh.** Scores anchor around 0.5. Above 0.8 is reserved for exceptional work. Every score names specific `wasteful_patterns`.
5. 🔬 **Every number is explainable.** `token-roi explain --kind session --id <ID>` returns the raw events, memory writes, retrieval hits, and LLM reasoning behind any score.

---

## Troubleshooting

**Projects tab is empty.** You're probably running a dashboard started before a recent code update. Stop the dashboard (Ctrl-C), restart (`token-roi dashboard`), and hard-refresh your browser (Cmd-Shift-R / Ctrl-Shift-R).

**Session names are blank on the dashboard.** Run `token-roi name-sessions`. If it says "0 new sessions named" but names still look empty, the importer pre-populated placeholder rows — a fresh run on the latest code picks those up correctly.

**Dashboard shows "$0.00" for a real session.** That session's events were imported without `model` metadata (e.g. from a pre-usage-field SDK wrapper). Bossify falls back to Opus 4.x pricing on unknown-model events; if a big chunk of events truly lack a model, the per-session total is just that fallback total.

**The LLM judge is too slow / crashes.** The judge needs roughly 15–30 s per prompt on a mid-range laptop. Options: pass `--limit 20` for a quick smoke test, run `token-roi judge --session <id>` to judge just one session, or switch to a smaller/faster model (`--model gemma-3-4b` is a reasonable floor for the adversarial rubric).

**"LLM endpoint not reachable."** LM Studio / Ollama isn't running, or it's not serving an OpenAI-compatible endpoint on port 1234. Use `--endpoint http://localhost:11434/v1` for a default Ollama install.

---

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

54 tests cover the core paths: event storage, attribution graph, ROI classifier, pricing lookup, synthetic-prompt filtering, aggregate formula, cost roll-ups, hook integration, project grouping, and the CLI.

---

## Notes

- **Source data quality = audit quality**: richer project history → sharper verdicts.
- LM Studio / Ollama must be running before `token-roi judge`.
- Still a research preview — please file issues if you find bugs!

---

## Acknowledgement

Built as a Claude Code skill. The entry point for Claude itself is [SKILL.md](SKILL.md); this README is for humans (and bosses). Inspired by the observation that every team has a few "token black holes" — long sessions that produce no durable output — and no one was measuring them. Until now.

---

<div align="center">

MIT License © [chinaharry](https://github.com/chinaharry)

*Measure the vibes. Then bill them.*

</div>
