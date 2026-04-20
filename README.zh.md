<div align="center">

# bossify.claude · 新时代老板

> *"你们搞 AI 的天天说 agentic coding 多便宜 —— 那为什么我的 token 账单看起来像一轮 A 轮融资？"*

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://python.org)
[![Claude Code](https://img.shields.io/badge/Claude%20Code-Skill-blueviolet)](https://claude.ai/code)
[![Local-First](https://img.shields.io/badge/Local--First-100%25-brightgreen)](#)
[![Bilingual](https://img.shields.io/badge/i18n-EN%20%C2%B7%20%E4%B8%AD%E6%96%87-ff69b4)](README.md)

<br>

你的工程师花了 4 小时、烧了 200 万 token —— 就为了重命名一个变量？<br>
你的实习生的 Claude 会话读起来像陀思妥耶夫斯基的小说，却一次 commit 都没有？<br>
你的 `MEMORY.md` 不停变长，但没人再翻回去看过？<br>
你的「AI 加速」冲刺到头来，交付还不如上个老套路多？<br>
你的财务同学非常礼貌地问你："黑洞会话"到底是啥？<br>

**把 vibe coding 变成可问责的资本配置 —— 欢迎来到新时代老板！**

<br>

把它指向团队的 `~/.claude/projects/`<br>
让本地 LLM **严格无情**地评判每一条 prompt<br>
然后得到一个**敢点名的老板视角看板**

[是什么](#是什么) · [安装](#安装) · [快速开始](#个人审计--快速开始) · [示例](#示例) · [评分原理](#评分原理) · [架构](#架构)

[**English README**](README.md)

</div>

---

> 🆕 **2026.04 更新** — **团队审计模式已上线！** 每人一张卡片、浪费排行榜、下钻到具体烧了 token 却没产出的那几条 prompt。通过 `data/employees.json` 配置。

> 🔒 **始终本地优先。** 每一条 prompt、工具调用、LLM 判定都只留在你的机器上。仅通过 LM Studio / Ollama。没有云、没有遥测、没有「我们保证不偷看」。

---

作者：[@chinaharry](https://github.com/chinaharry)

## 是什么

Bossify 把 agentic token 消耗当作**资本配置**来看待，并让本地 LLM 毫不留情地评判每条 prompt 是否物有所值。

| 核心主张 | 翻译成人话 |
|--------|-------------|
| 👔 **老板优先** | 每人一张卡片、浪费排行榜、下钻到烧了 token 却没产出的那几条 prompt |
| 😈 **严格，不讨好** | 本地 LLM 以 **0.5** 为基线（而不是 0.8）。`HIGH_VALUE` 只留给真正出色的工作。每条判定都附带具体的 `wasteful_patterns` 证据 |
| 🔍 **本地优先且可审计** | 每条 prompt、工具调用、工具输出、memory 写入、retrieval 命中 → 无损追加到 JSONL。每一个 ROI 数字都能回溯到产生它的原始事件，绝不凭空捏造 |
| 🌏 **双语** | 英文和简体中文的 UI、LLM prompt、CLI 帮助全套支持（`--locale zh` 或 `TOKEN_ROI_LOCALE=zh`） |

## 看板

三个标签页，本地服务在 **http://127.0.0.1:8787**：

| 标签页 | 展示内容 |
|---|---|
| 📊 **团队总览** / Team Overview | 老板视角 KPI、主要浪费模式、按员工排行 |
| 🧑‍💻 **员工** / Employees | 卡片网格 —— 每人一张，含 ROI 分布、高价值会话、浪费示例 |
| 🔬 **高级** / Advanced | 时间维度 token 流、memory 有效性散点图、工具 treemap、会话下钻 |

---

## 安装

> **注意**：需要 Python 3.11+，以及运行在 `http://localhost:1234/v1` 上的本地 LLM 服务（LM Studio 或 Ollama）。

```bash
git clone https://github.com/chinaharry/bossify-with-claude
cd bossify-with-claude
python -m pip install -e '.[dashboard,embeddings]'
token-roi init
```

可选 extras：`otel`（OpenTelemetry 导出）、`anthropic`（Agent SDK 包装器）、`dev`（pytest）。

### 支持的数据源

`token-roi import <source>` 可从以下任意一种来源拉取会话历史：

- **claude-code** — `~/.claude/projects/` 下的 JSONL，完整记录了 token 用量、工具调用与文件编辑事件，是参考实现。
- **codex** — OpenAI Codex CLI 的会话日志（`~/.codex/sessions/`），将 `message` / `function_call` / `function_call_output` / `token_count` 映射为事件。各版本 schema 有差异，导入器对未知记录宽容跳过。
- **cursor** — Cursor IDE 用户目录下 `state.vscdb` SQLite 中的聊天历史。**注意：** 除非你启用了 Cursor 的 API-key / OpenRouter 自带计费模式，Cursor 通常不会记录 token 用量，导入后的事件 token 计为 0。
- **aider** — `.aider.chat.history.md` 会话文本，当同目录存在 `.aider.llm.history` 时自动补齐 token 数与模型名。带路径 info-string 的代码块会提升为 `FILE_WRITE` 事件。
- **openai-jsonl** — 通用 OpenAI Responses API / Chat Completions JSONL 日志；按行读取 `input` / `output` / `usage` 并生成对应的用户 / 助手 / 工具调用事件。

---

## 个人审计 — 快速开始

回溯导入你在 Claude Code 里做过的一切：

```bash
token-roi import claude-code                    # 拉取 ~/.claude/projects/*/*.jsonl
# 也支持：
# token-roi import codex                          # OpenAI Codex CLI（~/.codex/sessions）
# token-roi import cursor                         # Cursor IDE 聊天历史
# token-roi import aider --from ~/projects        # Aider .aider.chat.history.md
# token-roi import openai-jsonl --from log.jsonl
token-roi score                                  # 归因 + ROI 分类器
token-roi judge --model zai-org/glm-4.7-flash    # 每条 prompt 的本地 LLM 判定
token-roi name-sessions                          # 给会话起一个人类可读的短名字
token-roi dashboard                              # 打开 http://127.0.0.1:8787
```

没有任何 prompt 会离开你的机器。小拇指保证 —— 而且通过「代码里根本没有网络请求」来强制执行。

## 团队审计 — 管理者工作流

在 `data/employees.json` 里定义你的团队：

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

按团队习惯的语言跑完整流水线：

```bash
token-roi --locale zh import claude-code
token-roi --locale zh score
token-roi --locale zh judge
token-roi --locale zh dashboard
```

### 审计命令

| 命令 | 说明 |
|---------|-------------|
| `token-roi employees list` | 按员工的汇总数据 |
| `token-roi employees show alice` | 下钻到某个人 |
| `token-roi view black_holes.sql` | 每 token 价值最低的会话 |
| `token-roi view orphan_memory.sql` | 写进去就再也没被读过的 memory |
| `token-roi explain --kind session --id <ID>` | 某个评分的完整推导过程 |

---

## 示例

> 输入：一个 47 分钟、烧掉 180 万 token、0 次 commit 的会话

**场景 1：黑洞会话**

```
Bossify       ❯ 分类：WASTED · 分数 0.12
                wasteful_patterns:
                  - 23 次工具调用，没有产生任何 diff
                  - 助手写了 4,000 字的方案，用户从未批准
                  - MEMORY.md 增长 6KB，在后续会话中 0 次命中
                判定："冗长的规划性会话，没有任何可交付产物。
                        Token 没了。"
```

**场景 2：礼貌的反驳**

```
工程师        ❯ 但我是在探索架构啊，这是有价值的思考！

Bossify       ❯ 不产生任何产物、没有 commit、没有 retrieval 命中的探索 ——
                那是存储成本，不是价值。分类仍然是 WASTED。
                （见：设计原则 #3。）
```

---

## 评分原理

每条 prompt、会话、工具链、memory 写入都会被归为以下其一：

| 分类 | 含义 |
|---|---|
| 🟢 `HIGH_VALUE` | 产生了可持续输出，并在后续会话中被复用。按设计就很稀少。 |
| 🟡 `TRANSIENT_VALUE` | 有产出，但没被复用。仍然可以接受。 |
| 🟠 `LOW_VALUE` | 每 token 价值偏低 —— 大段叙述，可持续产出少。 |
| 🔴 `WASTED` | 零可持续回报。Token 没了。 |

数学公式只是起点。**本地 LLM 的判定会覆盖它** —— 如果 judge 读完实际内容后认为"这是助手在喋喋不休地叙述，并没有产出代码"，那么不管 token 比率说什么，分类直接降到 `WASTED`。

Judge 的 prompt 是刻意对抗性的：以 0.5 为锚（不是 0.8）、要求具体的 `wasteful_patterns` 证据、明确告知它不要讨好用户。即便是 `HIGH_VALUE` 的工作，也必须指出至少一个效率问题。

---

## 架构

```
  Claude Code hooks ─┐
  SDK wrapper        ├──▶ EventStore（追加式 JSONL）  ◀── 唯一真相源
  JSONL importer ────┘           │
                                  ▼
                         AnalyticsDB（SQLite，可重建）
                                  │
            ┌───────────────┬─────┴─────┬──────────────┐
            ▼               ▼           ▼              ▼
         归因           检索         压缩        LLM Judge (LM Studio)
       （逐 prompt）   （混合）    （MEMORY.md）    对抗式 rubric
            │                                          │
            └────────────▶ ROI 分类器 ◀────────────────┘
                          HIGH / TRANS / LOW / WASTED
                                    │
                                    ▼
                         看板（FastAPI + ECharts）
                         团队总览 · 员工 · 高级
```

完整 CLI 请见 [SKILL.md](SKILL.md)，架构笔记、schema 和 ROI 模型请见 [references/](references/)。

---

## 设计原则

1. 🧾 **无损存储是唯一真相源。** `data/raw_events/**/*.jsonl` 追加写入，永不原地重写。
2. 🧠 **压缩后的 memory 不是真相源。** `MEMORY.md` 只是缓存，可以从原始事件重建。
3. 🎯 **是否被检索决定实际价值。** 写进去却从未被读的 memory 只算存储成本，不算价值。
4. 😈 **LLM judge 必须严格。** 评分以 0.5 为锚。高于 0.8 只留给真正出色的工作。每个分数都必须指出具体的 `wasteful_patterns`。
5. 🔬 **每一个数字都能解释。** `token-roi explain --kind session --id <ID>` 返回背后的原始事件、memory 写入、retrieval 命中和 LLM 推理过程。

---

## 开发

```bash
python -m pip install -e '.[dev]'
pytest
```

22 个测试覆盖核心路径：事件存储、归因图、ROI 分类器、压缩、检索、hook 集成和 CLI。

---

## 注意事项

- **源数据质量 = 审计质量**：项目历史越丰富，判定越锋利。
- 运行 `token-roi judge` 之前，LM Studio / Ollama 必须已经启动。
- 目前仍是研究预览版 —— 发现 bug 请提 issue！

---

## 致谢

作为 Claude Code 技能构建。Claude 自己看的入口是 [SKILL.md](SKILL.md)；这份 README 是给人（和老板）看的。灵感来源于一个观察：几乎每个团队都有那么几个"token 黑洞" —— 超长会话，产出为零，而且没人在衡量它们。直到现在。

---

<div align="center">

MIT License © [chinaharry](https://github.com/chinaharry)

*先把 vibe 量化，再开账单。*

</div>
