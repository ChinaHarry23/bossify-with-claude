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

Three tabs, served locally at **http://127.0.0.1:8787**:

| Tab | What it shows |
|---|---|
| 📊 **Team Overview** / 团队总览 | Boss-first KPIs, top waste patterns, leaderboard by employee |
| 🧑‍💻 **Employees** / 员工 | Card grid — one per person with ROI mix, top sessions, waste examples |
| 🔬 **Advanced** / 高级 | Token flow over time, memory effectiveness scatter, tool treemap, session drill-down |

---

## Install

> **Important**: requires Python 3.11+ and a local LLM server (LM Studio or Ollama) running on `http://localhost:1234/v1`.

```bash
git clone https://github.com/chinaharry/bossify-with-claude
cd bossify-with-claude
python -m pip install -e '.[dashboard,embeddings]'
token-roi init
```

Optional extras: `otel` (OpenTelemetry export), `anthropic` (Agent SDK wrapper), `dev` (pytest).

---

## Personal audit — quick start

Retrospectively ingest everything you've ever done in Claude Code:

```bash
token-roi import claude-code                    # pulls ~/.claude/projects/*/*.jsonl
token-roi score                                  # attribution + ROI classifier
token-roi judge --model zai-org/glm-4.7-flash    # local-LLM verdict per prompt
token-roi name-sessions                          # short human-readable session names
token-roi dashboard                              # open http://127.0.0.1:8787
```

No prompts leave your machine. Pinky promise, and it's enforced by the fact that there's no network code.

## Team audit — manager workflow

Define your team in `data/employees.json`:

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

Run the full pipeline in your team's preferred language:

```bash
token-roi --locale zh import claude-code
token-roi --locale zh score
token-roi --locale zh judge
token-roi --locale zh dashboard
```

### Audit commands

| Command | Description |
|---------|-------------|
| `token-roi employees list` | Per-employee rollup |
| `token-roi employees show alice` | Drill into one person |
| `token-roi view black_holes.sql` | Sessions with the lowest value-per-token |
| `token-roi view orphan_memory.sql` | Memory writes that are never re-read |
| `token-roi explain --kind session --id <ID>` | Full derivation of one score |

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

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

22 tests cover the core paths: event storage, attribution graph, ROI classifier, compression, retrieval, hook integration, and the CLI.

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
