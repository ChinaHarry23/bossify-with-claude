<div align="center">

# bossify · 新时代老板

**The boss dashboard for your AI coding spend.**

Bossify reads your team's Claude Code sessions, asks a local LLM to judge every prompt, and shows you who's burning tokens and on what. 100% local. Names names.

![demo](docs/assets/hero.gif)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Local-First](https://img.shields.io/badge/Local--First-100%25-brightgreen)](#)
[![中文](https://img.shields.io/badge/i18n-EN%20%C2%B7%20%E4%B8%AD%E6%96%87-ff69b4)](README.zh.md)

</div>

```bash
git clone https://github.com/chinaharry/bossify-with-claude
cd bossify-with-claude
pip install -e '.[dashboard,embeddings]'
token-roi init
```

<div align="center">

[Quick Start](#first-run--full-pipeline-walkthrough) · [Demo](#demo) · [How Scoring Works](#how-scoring-works) · [Architecture](#architecture) · [中文 README](README.zh.md)

</div>

---

> Your engineer spent 4 hours and 2M tokens to rename a variable.
> Your intern's sessions read like Dostoyevsky with zero commits.
> Your "AI-accelerated" sprint somehow shipped less than the old one.
>
> Bossify turns agentic token usage into **capital allocation** and asks a local LLM to judge, without mercy, whether each prompt was worth what it cost.

![The People tab](docs/assets/people-tab.png)

*The People tab — one card per engineer, sorted by waste. Drill into any name to see the exact prompts that burned tokens for nothing.*
