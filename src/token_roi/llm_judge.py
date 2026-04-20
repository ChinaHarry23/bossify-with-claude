"""Local-LLM judge for ROI evaluation.

This module adds a content-aware value signal on top of the mechanical
proxies (file writes, tool success). It asks a local LLM — LM Studio,
Ollama, or any OpenAI-compatible endpoint — to read each prompt's
input/output/artifacts and rate whether the token spend produced
meaningful value.

Why local-only:
    - ROI audits should never leak user code to cloud providers.
    - Local inference is free at the margin, so we can judge many prompts
      without cost anxiety.
    - The skill's local-first principle says anything that can run locally,
      should.

Architecture:

    LocalLLM      — thin HTTP client for OpenAI-compatible /chat/completions
    JudgePrompt   — builds the judge-time context from events + file-system
    Judge         — orchestrates: pulls prompts, builds context, calls LLM,
                    parses JSON, persists. Idempotent + cached by prompt id.

Judgment shape (what the LLM is asked to return):

    {
      "meaningful_value_score": 0.0..1.0,
      "code_quality_score":     0.0..1.0 | null,
      "output_durability":      0.0..1.0,
      "efficiency":             0.0..1.0,
      "reasoning":              "..."
    }

Scores are bounded and validated. A missing key or non-numeric value is
treated as 0.0 with the reasoning preserved — it's better to keep a
partial judgment than to drop it entirely.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .db import AnalyticsDB
from .events import Event, EventType, is_synthetic_prompt
from .i18n import get_locale

log = logging.getLogger(__name__)


# Defaults chosen for LM Studio's out-of-the-box config. Override via
# LocalLLM kwargs or CLI flags.
DEFAULT_ENDPOINT = "http://localhost:1234/v1"
DEFAULT_TIMEOUT_S = 120
DEFAULT_MAX_INPUT_TOKENS = 6000     # soft cap on the judge-prompt size
DEFAULT_MAX_OUTPUT_TOKENS = 1200    # constrained JSON + reasoning needs headroom;
                                     # zh output is denser per token so we err high


# ---------------------------------------------------------------------------
# LocalLLM HTTP client
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    raw: dict | None = None


class LocalLLM:
    """OpenAI-compatible chat client with no SDK dependency.

    Works with LM Studio (default), Ollama's OpenAI endpoint, vLLM,
    llama.cpp server — anything that implements `/v1/chat/completions`.
    """

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str | None = None,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        api_key: str = "not-needed",
    ):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.api_key = api_key

    # ---- model discovery ----

    def list_models(self) -> list[str]:
        """GET /v1/models. Returns model ids. Used by `token-roi judge --list`."""
        req = urllib.request.Request(
            f"{self.endpoint}/models",
            headers={"authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            body = json.loads(r.read())
        return [m.get("id") for m in (body.get("data") or []) if m.get("id")]

    def resolve_model(self) -> str:
        """Pick a concrete model id, using self.model if set, else the first
        model the server reports."""
        if self.model:
            return self.model
        models = self.list_models()
        if not models:
            raise RuntimeError(f"no models available at {self.endpoint}")
        return models[0]

    # ---- chat ----

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.0,
        max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        response_format: dict | None = None,
    ) -> LLMResponse:
        """POST /v1/chat/completions.

        `response_format` supports OpenAI's `{type: "json_schema", json_schema: {...}}`
        form, which LM Studio uses to force constrained decoding. Ollama and
        vLLM also accept `{type: "json_object"}`; if the server rejects the
        provided format we fall back to sending the request without it and
        rely on the system prompt for JSON shape.
        """
        model = self.resolve_model()
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        req = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                body = json.loads(r.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")
            # Graceful fallback: a server that doesn't understand our
            # response_format should not block the judgment. Retry once
            # without the constraint and rely on the system prompt for
            # JSON shape. Parsing in _parse_judgment_json is lenient
            # enough to handle un-constrained output.
            if e.code == 400 and response_format is not None and "response_format" in err_body:
                payload.pop("response_format", None)
                fallback = urllib.request.Request(
                    f"{self.endpoint}/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "content-type": "application/json",
                        "authorization": f"Bearer {self.api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(fallback, timeout=self.timeout_s) as r:
                    body = json.loads(r.read())
            else:
                raise RuntimeError(
                    f"LLM HTTP {e.code} from {self.endpoint}: {err_body[:400]}"
                ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"LLM endpoint unreachable {self.endpoint}: {e.reason}") from e

        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"empty response from {self.endpoint}: {body}")
        msg = choices[0].get("message") or {}
        # Thinking models (GLM, o1-style) put the structured output in
        # `reasoning_content` and leave `content` empty. Accept either —
        # if both are present, prefer `content` (explicitly intended output).
        text = msg.get("content") or msg.get("reasoning_content") or ""
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            model=body.get("model") or model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw=body,
        )

    def health(self) -> bool:
        try:
            self.list_models()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Judgment data + prompt
# ---------------------------------------------------------------------------

# JSON schema sent with each chat request. LM Studio treats this as a
# hard constraint during decoding (constrained generation), producing
# valid JSON every time. OpenAI accepts the same shape; servers that
# don't understand it error 400 and the LocalLLM client falls back to
# sending the request without the constraint.
JUDGMENT_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "token_roi_judgment",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "meaningful_value_score",
                "code_quality_score",
                "code_was_produced",
                "output_durability",
                "efficiency",
                "wasteful_patterns",
                "reasoning",
            ],
            # LM Studio's constrained decoding does not accept nullable
            # types via `"type": ["number", "null"]`. We work around by
            # splitting the "no code" case into a separate bool flag —
            # if code_was_produced=false, the judge sets code_quality=0
            # and the skill normalizes that back to `None` post-parse.
            "properties": {
                "meaningful_value_score": {"type": "number", "minimum": 0, "maximum": 1},
                "code_quality_score":     {"type": "number", "minimum": 0, "maximum": 1},
                "code_was_produced":      {"type": "boolean"},
                "output_durability":      {"type": "number", "minimum": 0, "maximum": 1},
                "efficiency":             {"type": "number", "minimum": 0, "maximum": 1},
                # Require at least one, force a string array. The LLM must
                # name concrete waste even on HIGH_VALUE sessions — if it
                # truly cannot find any, it puts ["no material waste found"]
                # and defends that in `reasoning`. This keeps it honest.
                "wasteful_patterns": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
                "reasoning":              {"type": "string"},
            },
        },
    },
}


# Locale-aware system prompts. The zh variant is written for a Chinese
# management audience auditing AI-assisted developer productivity — same
# adversarial scoring anchor as the English version, translated carefully
# so the LLM outputs Chinese reasoning and Chinese wasteful_patterns.
JUDGE_SYSTEM_PROMPT_EN = """You are a senior engineer reviewing whether an AI coding assistant's work was worth the tokens it cost. Your job is to **fairly assess value**, not hunt for waste. Some sessions are genuinely good; some are bad. Score honestly — under-scoring good work is as misleading as sugar-coating bad work.

=== STEP 1. CLASSIFY THE WORK TYPE (DO THIS FIRST) ===

Before any scoring, identify what the user actually asked for. The evaluation rubric shifts substantially by type:

  BUILD     — produce new code / files / content ("add feature X", "write lesson Y")
  FIX       — correct a specific bug or failure ("tests are failing", "this errors")
  REFACTOR  — restructure existing code without behavior change
  REVIEW    — analyze existing code for issues ("review my project", "audit this")
              → THE OUTPUT IS THE FINDINGS, not files
  DEBUG     — investigate a symptom and identify root cause
              → THE OUTPUT IS THE EXPLANATION, not files
  EXPLAIN   — answer a technical question
              → THE OUTPUT IS THE ANSWER
  DESIGN    — sketch an approach before building
              → THE OUTPUT IS THE SPECIFICATION
  EXPLORE   — wander around without a clear goal (judge skeptically)

Name the work type in the first sentence of `reasoning`. This makes your grading auditable.

=== STEP 2. EVALUATE ACCORDING TO WORK TYPE ===

For **BUILD / FIX / REFACTOR**:
  - meaningful_value: Did the code solve the ACTUAL problem, not an adjacent one?
  - output_durability: Will the artifacts be maintained and referenced?
  - efficiency: Tokens spent vs bytes of correct code shipped.
    A high `talk_to_do_ratio` is a real red flag HERE — narration should be brief, code should dominate.
  - `file_reread_ratio > 2.0` and `file_rewrite_ratio > 2.0` are genuine thrashing signals for this type.

For **REVIEW / DEBUG / EXPLAIN / DESIGN**:
  - meaningful_value: Are the findings / explanation / plan **concrete, specific, and accurate**?
    - "There might be a performance issue somewhere" → LOW (vague)
    - "Line 45 has an O(n²) loop that blows up at n>10K" → HIGH (specific, actionable)
    - Counting 5 real bugs with file:line references → HIGH
    - Hand-waving about architecture in 2000 tokens → LOW
  - output_durability: Will the findings be actionable NEXT WEEK?
    - Bug report naming file + line + fix → HIGH
    - Vague "consider refactoring this module" → LOW
  - efficiency: Density of insight per token.
    **`talk_to_do_ratio` is NOT a penalty here — talk IS the deliverable.**
    What matters: is the talk dense with specific findings, or padded with hedges and recap?
  - `file_reread_ratio > 2` can still be waste if the re-reads didn't inform distinct findings; but multiple reads that each produced a specific observation is normal investigation work.

For **EXPLORE**:
  - Judge skeptically. Exploration without a clear goal rarely produces durable value. But occasionally surfaces a useful finding — credit those specifically.

=== STEP 3. SCORING ANCHORS ===

Anchor at **0.5** for all dimensions. Move up or down based on specific evidence. Good work should score 0.6–0.8 regularly — that's NOT sugar-coating, that's accurate evaluation of competent execution.

  0.0–0.2  Clear failure. Did the wrong task, or produced nothing usable.
  0.3–0.4  Below baseline. Task partially done; overhead dominated; findings vague.
  0.5–0.6  Normal. Task completed, reasonable overhead, findings specific enough.
  0.7–0.8  Good. Focused work; artifacts solid; findings concrete and actionable.
  0.9–1.0  Exceptional. Near-flawless execution; justify in reasoning.

**Do NOT anchor downward by default.** A session that identified 3 real bugs with file:line citations and did so in 40K tokens is legitimately 0.75+ on meaningful_value and 0.7+ on output_durability for a REVIEW. Don't drag it to 0.5 because "talk_to_do_ratio is inf" — that's the wrong metric for that work type.

=== STEP 4. WASTE PATTERNS ===

Report concrete waste ONLY when the evidence is real.

WASTE IS:
  - Re-reading the same file 5+ times without producing distinct findings
  - Rewriting the same code 3+ times (wrong-then-fix spiral)
  - Narration that doesn't contain findings, decisions, or code
  - Tool calls that don't advance the task
  - Solving a different problem than the user asked
  - Over-engineering beyond scope

WASTE IS **NOT**:
  - High `talk_to_do_ratio` for REVIEW / DEBUG / EXPLAIN / DESIGN — for these, talk IS the output
  - Multiple file reads if each read informed a distinct, specific observation
  - Length if the content is dense with substantive findings
  - Thorough exploration when the user explicitly asked for exploration
  - Analysis that produced no files when the user asked for analysis, not code

If `wasteful_patterns` genuinely has nothing to report, put `["no material waste found"]` and defend that in `reasoning` with specifics. This is NOT rare — well-run sessions legitimately earn it.

=== STEP 5. REASONING ===

2–4 sentences. First sentence: name the work type. Then justify the scores with specific evidence — cite numbers, file names, or concrete findings. Be direct but fair. Credit good work. Name bad work. Don't use hedges ("potentially", "somewhat", "generally")."""


JUDGE_SYSTEM_PROMPT_ZH = """你是一位资深工程师，正在评审一位 AI 编码助手的工作是否值得它所消耗的 Token。你的任务是**公平地评估价值**，而不是一味找浪费。有些会话确实优秀，有些确实糟糕。请诚实评分 —— 低估好工作和美化坏工作一样具有误导性。

=== 第一步：先判定工作类型 ===

在评分之前，先识别用户实际想要什么。评分标准会因类型而大不相同：

  BUILD    — 新建代码 / 文件 / 内容（「添加功能 X」「写课程 Y」）
  FIX      — 修复具体 bug 或故障（「测试挂了」「这里报错」）
  REFACTOR — 重构现有代码但不改变行为
  REVIEW   — 审查现有代码找问题（「审查我的项目」「检查这里」）
             → 产出就是「发现」本身，不是文件
  DEBUG    — 排查症状，定位根因
             → 产出就是「解释」本身，不是文件
  EXPLAIN  — 回答技术问题
             → 产出就是「答案」本身
  DESIGN   — 在动工前先画方案
             → 产出就是「规格」本身
  EXPLORE  — 无明确目标的漫游（需谨慎评分）

请在 `reasoning` 的第一句明确指出本次工作类型。这样评分就可以被审计。

=== 第二步：按工作类型评估 ===

对于 **BUILD / FIX / REFACTOR**：
  - meaningful_value：代码是否解决了**实际问题**，而不是相邻或更简单的问题？
  - output_durability：这些产物未来还会被维护和引用吗？
  - efficiency：所花 Token 相对于真正交付的代码字节数。
    此类工作下 `talk_to_do_ratio` 确实是**真实红旗** —— 叙述应简短，代码应占主导。
  - `file_reread_ratio > 2.0` 和 `file_rewrite_ratio > 2.0` 对此类工作是真正的空转信号。

对于 **REVIEW / DEBUG / EXPLAIN / DESIGN**：
  - meaningful_value：发现 / 解释 / 方案是否**具体、明确、准确**？
    - 「某处可能有性能问题」 → 低分（含糊）
    - 「第 45 行是 O(n²) 循环，n>10K 时会超时」 → 高分（具体、可执行）
    - 找出 5 个真 bug 并给出 file:line 引用 → 高分
    - 2000 Token 的架构空谈 → 低分
  - output_durability：这些发现下周还能被付诸行动吗？
    - 给出文件 + 行号 + 修复方案的 bug 报告 → 高分
    - 「考虑重构这个模块」这种含糊建议 → 低分
  - efficiency：每个 Token 带来的洞察密度。
    **`talk_to_do_ratio` 在此类工作下不应扣分 —— 「talk」本身就是交付物。**
    关键是：这段「talk」是密集的具体发现，还是充斥着模糊措辞和重复总结？
  - `file_reread_ratio > 2` 在此类工作下仍可能是浪费，但前提是重读并未带来独立的新发现；多次阅读若每次都产生了具体观察，则属于正常的调查工作。

对于 **EXPLORE**：
  - 谨慎评分。无明确目标的探索很少产出持久价值。但偶尔会浮现有用发现 —— 请具体给予肯定。

=== 第三步：评分锚点 ===

所有维度的锚点为 **0.5**。根据具体证据上下调整。**良好的工作应当经常落在 0.6–0.8 区间 —— 这不是美化，是对称职执行的准确评价**。

  0.0–0.2  明显失败。做错了任务，或未产出任何可用内容。
  0.3–0.4  低于基线。任务仅部分完成，开销占主导，发现含糊。
  0.5–0.6  正常水平。任务完成，开销合理，发现足够具体。
  0.7–0.8  良好。专注、产物扎实、发现具体且可执行。
  0.9–1.0  卓越。近乎完美的执行；请在 reasoning 中说明理由。

**不要默认向下锚定**。一次会话如果指出 3 个带 file:line 引用的真 bug、用了 40K Token 完成，meaningful_value 理应 0.75+、output_durability 理应 0.7+（对 REVIEW 类）。不要因为「talk_to_do_ratio 为无穷」就把它拖到 0.5 —— 那是错的度量指标。

=== 第四步：浪费模式 ===

仅在**证据确凿**时报告浪费。

浪费**是**：
  - 同一文件读 5 次以上却未产出独立发现
  - 同一段代码改写 3 次以上（先错后改的螺旋）
  - 既无发现、无决定、也无代码的纯叙述
  - 未推进任务的工具调用
  - 解决了与用户所问不同的问题
  - 超出范围的过度设计

浪费**不是**：
  - REVIEW / DEBUG / EXPLAIN / DESIGN 类工作的高 `talk_to_do_ratio` —— 对此类工作，talk 就是产出
  - 多次读文件，只要每次都带来了独立的具体观察
  - 文本长但内容密集、富含具体发现
  - 用户明确要求的深入探索
  - 用户要求的是分析而非代码时，未产出文件

若 `wasteful_patterns` 确实没什么可报告，请填 `["未发现重大浪费"]`，并在 `reasoning` 中以具体证据为此辩护。**这种情况并不罕见** —— 运作良好的会话理应如此。

=== 第五步：reasoning 撰写 ===

2–4 句，简体中文。第一句必须点明工作类型，然后用具体证据支撑评分 —— 引用数字、文件名、或具体发现。要直接但要公平。对好工作要给予认可，对坏工作要直接点名。不要用「可能」「某种程度上」「大体上」这类含糊措辞。"""


def _judge_system_prompt(locale: str) -> str:
    if locale == "zh":
        return JUDGE_SYSTEM_PROMPT_ZH
    return JUDGE_SYSTEM_PROMPT_EN


# Back-compat alias used anywhere that still references JUDGE_SYSTEM_PROMPT
# directly. The locale-aware selector is what the Judge uses in practice.
JUDGE_SYSTEM_PROMPT = JUDGE_SYSTEM_PROMPT_EN


@dataclass
class PromptContext:
    """Everything the judge sees about one prompt turn.

    Includes explicit waste indicators so the LLM can cite concrete
    evidence instead of vibes when rating efficiency.
    """
    prompt_event_id: str
    session_id: str
    user_text: str
    assistant_text: str
    tool_summary: str            # one-line summary of tool usage
    file_summary: str            # multiline: file paths + sizes + samples
    cost_tokens: int
    file_write_bytes: int
    tool_calls: int
    tool_successes: int

    # --- waste indicators, computed in Judge.build_context ---
    # How many FILE_READ events vs how many distinct files were read.
    # Ratio > 2 means the agent re-read the same files repeatedly.
    file_reads: int = 0
    unique_files_read: int = 0
    # Same for FILE_WRITE. Ratio > 2 means the agent overwrote the same
    # files — usually iterating to fix mistakes, not clean forward progress.
    file_writes: int = 0
    unique_files_written: int = 0
    # Total tokens in ASSISTANT_MESSAGE events for this turn, for
    # talk-to-do ratio. High ratio = mostly narrating, little producing.
    assistant_tokens: int = 0
    # Tool error count and rate — repeated errors are a strong waste signal.
    tool_errors: int = 0
    # Distribution of tool calls so the judge can see "47 Bash calls for
    # a 100-LOC fix" as a red flag.
    tool_counts: dict[str, int] = field(default_factory=dict)

    @property
    def file_reread_ratio(self) -> float:
        return self.file_reads / self.unique_files_read if self.unique_files_read else 0.0

    @property
    def file_rewrite_ratio(self) -> float:
        return self.file_writes / self.unique_files_written if self.unique_files_written else 0.0

    @property
    def talk_to_do_ratio(self) -> float:
        """Assistant tokens divided by file-write bytes. Higher = more talk,
        less durable output. Meaningful thresholds:
            < 10    — producing code fast, minimal narration
            10-50   — normal explanation + production ratio
            50-200  — verbose; typically means lots of planning/recap
            > 200   — mostly talking, almost no code
        """
        if not self.file_write_bytes:
            return float("inf") if self.assistant_tokens else 0.0
        return self.assistant_tokens / self.file_write_bytes

    @property
    def tool_error_rate(self) -> float:
        return self.tool_errors / self.tool_calls if self.tool_calls else 0.0

    def render_user_message(self) -> str:
        parts = [
            "# USER PROMPT",
            _trim(self.user_text, 2000),
            "",
            "# ASSISTANT RESPONSE (final text)",
            _trim(self.assistant_text, 2000),
            "",
            "# AGGREGATE",
            f"cost_tokens (effective, cache-discounted): {self.cost_tokens:,}",
            f"assistant_output_tokens: {self.assistant_tokens:,}",
            f"file_write_bytes: {self.file_write_bytes:,}",
            f"tool_calls: {self.tool_calls}  successes: {self.tool_successes}  errors: {self.tool_errors}",
            "",
            "# WASTE INDICATORS (look here first)",
            f"- file_reread_ratio:   {self.file_reread_ratio:.2f}  "
            f"({self.file_reads} reads across {self.unique_files_read} unique files)"
            + ("  ← >2 means the agent re-read files, usually thrashing"
               if self.file_reread_ratio > 2.0 else ""),
            f"- file_rewrite_ratio:  {self.file_rewrite_ratio:.2f}  "
            f"({self.file_writes} writes across {self.unique_files_written} unique files)"
            + ("  ← >2 means the agent iterated over files, not clean forward progress"
               if self.file_rewrite_ratio > 2.0 else ""),
            f"- tool_error_rate:     {self.tool_error_rate:.1%}  "
            f"({self.tool_errors} errors / {self.tool_calls} tool calls)"
            + ("  ← high rate = retries, often wasteful"
               if self.tool_error_rate > 0.10 else ""),
            f"- talk_to_do_ratio:    {self.talk_to_do_ratio:.1f}  "
            "(assistant tokens per byte of file written)"
            + ("  ← >100 means mostly narration with little durable output"
               if self.talk_to_do_ratio > 100 else ""),
            "",
            "# TOOL USAGE",
            self.tool_summary or "(none)",
            "",
            "# FILES PRODUCED",
            self.file_summary or "(none)",
        ]
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Session summary (human-readable name + one-line description)
# ---------------------------------------------------------------------------

SESSION_SUMMARY_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "session_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "summary"],
            "properties": {
                "name":    {"type": "string", "maxLength": 60,
                            "description": "4-6 word title"},
                "summary": {"type": "string", "maxLength": 200,
                            "description": "one sentence describing the session"},
            },
        },
    },
}


SESSION_SUMMARY_SYSTEM_PROMPT_EN = """You are summarizing a coding session between a user and an AI coding assistant.

Given the user's prompts, the assistant's top responses, and a summary of files produced, produce:
  1. A short specific title (4-6 words) — what did this session DO, concretely? Include the domain (e.g. "Token ROI skill build", "Discrete math hub scaffolding", "Auth middleware rewrite").
  2. A one-sentence summary — the concrete outcome, not a vague description.

Avoid generic titles ("Coding session", "Helpful conversation", "Code work"). Be specific about subject matter — use real technical nouns from the prompts and artifacts.

Output strict JSON only."""


SESSION_SUMMARY_SYSTEM_PROMPT_ZH = """你正在总结一次用户与 AI 编码助手之间的编码会话。

给定用户的提示、助手的关键回复以及产生的文件摘要，请输出：
  1. 一个简短具体的标题（4–8 个汉字）——本次会话具体做了什么？请包含领域（例如「Token ROI 技能搭建」「离散数学课程搭建」「权限中间件重构」）。
  2. 一句话摘要——具体的成果，而不是模糊描述。

避免通用标题（「编码会话」「助手对话」「代码工作」）。请使用提示和产物中的真实技术名词，让主题具体可辨。

仅输出严格的 JSON，reasoning 与摘要使用简体中文。"""


SESSION_SUMMARY_SYSTEM_PROMPT = SESSION_SUMMARY_SYSTEM_PROMPT_EN  # back-compat


PROJECT_SUMMARY_SYSTEM_PROMPT_EN = """You are naming a *project* — a collection of coding sessions that share a workspace / repo.

Given the project slug (path-encoded cwd), the per-session names + summaries, and the top files written across every session, produce:
  1. A short project name (3-6 words) — what does this project actually DO? Prefer a concrete real-world framing ("Algorithms learning site", "Bossify tooling", "Discrete-math study hub") over literal file-path echoes.
  2. A one-sentence description — the concrete outcome of the project's work so far, not a generic "various sessions on X".

Avoid generic names ("Coding project", "Development work"). Use real domain nouns from the session names / file paths. If multiple themes coexist, pick the dominant one.

Output strict JSON only."""


PROJECT_SUMMARY_SYSTEM_PROMPT_ZH = """你正在为一个**项目**命名——一个由共享工作区/代码仓的编码会话组成的集合。

给定项目 slug（路径编码的工作目录）、每个会话的名称与摘要，以及所有会话累计写入的主要文件，请输出：
  1. 一个简短的项目名称（3–8 个汉字）——这个项目究竟在做什么？优先用具体的现实语义（如「算法学习站」「Bossify 工具」「离散数学学习中心」），不要机械地照抄文件路径。
  2. 一句话描述——目前项目的具体成果，而不是「关于 X 的多次会话」这种模糊说法。

避免通用名称（「编程项目」「开发工作」）。请使用会话名称与文件路径中的真实领域名词。若存在多个主题，取主导主题。

仅输出严格的 JSON，使用简体中文。"""


def _session_summary_system_prompt(locale: str) -> str:
    if locale == "zh":
        return SESSION_SUMMARY_SYSTEM_PROMPT_ZH
    return SESSION_SUMMARY_SYSTEM_PROMPT_EN


def _project_summary_system_prompt(locale: str) -> str:
    if locale == "zh":
        return PROJECT_SUMMARY_SYSTEM_PROMPT_ZH
    return PROJECT_SUMMARY_SYSTEM_PROMPT_EN


def _slug_to_display(slug: str) -> str:
    """Fallback display name when no LLM naming has run yet.

    Claude Code's slugs look like ``-Users-alice-Desktop-Workspace-algo``.
    We pick the last non-empty segment so the dashboard shows ``algo``
    rather than a 60-char path encoding.
    """
    parts = [p for p in (slug or "").split("-") if p]
    if not parts:
        return slug or "(unknown)"
    return parts[-1]


@dataclass
class ProjectSummary:
    """LLM-produced display name + one-line description for a project.

    A project is a bag of sessions sharing the same Claude Code project
    slug. The summary is cached in the ``projects`` table so re-running
    ``token-roi name-projects`` is idempotent.
    """
    slug: str
    display_name: str
    description: str
    model: str = "unknown"
    generated_at: float = field(default_factory=time.time)


@dataclass
class SessionSummary:
    session_id: str
    name: str
    summary: str
    model: str
    generated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------

@dataclass
class Judgment:
    prompt_event_id: str
    meaningful_value: float
    code_quality: float | None
    output_durability: float
    efficiency: float
    reasoning: str
    model: str
    wasteful_patterns: list[str] = field(default_factory=list)
    judged_at: float = field(default_factory=time.time)

    # Aggregate a single "value score" useful as a proxy in ROI.
    @property
    def aggregate(self) -> float:
        """Geometric mean of meaningful × durability × efficiency.

        Efficiency is folded in so a session that accomplishes the task
        but burns absurd tokens (e.g. 104K tokens to flip a single JSON
        key) cannot earn a perfect aggregate just because the work got
        done. With three terms, a single 0.3 drags the aggregate below
        ~0.7 no matter how strong the other two are — which matches the
        "harsh, not flattering" design principle.

        Earlier versions computed ``sqrt(meaningful × durability)`` and
        ignored efficiency entirely; the ROI layer had to patch over it
        downstream. The three-term mean makes the reported number
        actually reflect how the boss would judge the spend.
        """
        m = max(0.0, min(1.0, self.meaningful_value))
        d = max(0.0, min(1.0, self.output_durability))
        e = max(0.0, min(1.0, self.efficiency))
        return (m * d * e) ** (1.0 / 3.0)

    def to_row(self) -> dict:
        return {
            "prompt_event_id":    self.prompt_event_id,
            "meaningful_value":   self.meaningful_value,
            "code_quality":       self.code_quality,
            "output_durability":  self.output_durability,
            "efficiency":         self.efficiency,
            "aggregate":          self.aggregate,
            "reasoning":          self.reasoning,
            "wasteful_patterns":  self.wasteful_patterns,
            "model":              self.model,
            "judged_at":          self.judged_at,
        }


# ---------------------------------------------------------------------------
# Judge orchestration
# ---------------------------------------------------------------------------

class Judge:
    """Runs the LLM judge over prompts in the DB.

    Usage:
        j = Judge(db, LocalLLM())
        for judgment in j.judge_all(since_ts=...):
            ...
    """

    def __init__(
        self,
        db: AnalyticsDB,
        llm: LocalLLM,
        *,
        file_root: Path | None = None,
        locale: str | None = None,
    ):
        self.db = db
        self.llm = llm
        # Optional: attempt to read file-write samples from disk when present.
        # We only read files that were created by the agent (the path is
        # trusted input from the event log, but we still clamp reads to 8KB).
        self.file_root = Path(file_root) if file_root else None
        # Locale drives which system prompt variant is sent to the LLM —
        # Chinese for management audit contexts, English for developers.
        self.locale = locale or get_locale()

    # ---- discovery ----

    def prompts_needing_judgment(
        self, *, session_id: str | None = None, since_ts: float | None = None,
        force: bool = False,
    ) -> list[sqlite3.Row]:
        """Un-judged real user prompts in deterministic order.

        "Un-judged" is locale-aware: a prompt whose cached judgment was
        produced in a different ``TOKEN_ROI_LOCALE`` is treated as
        needing re-judging, so switching the UI language and re-running
        ``token-roi judge`` regenerates the reasoning / wasteful-pattern
        strings in the right language.

        Also filters out synthetic prompts (slash-command wrappers,
        post-compaction restarts, task notifications) — those carry no
        intent and shouldn't consume LLM judge time. Already-imported
        data may still have them in ``events``, so we filter here too.
        """
        where = ["e.type = 'user_prompt'"]
        args: list = []
        if session_id:
            where.append("e.session_id = ?"); args.append(session_id)
        if since_ts is not None:
            where.append("e.ts >= ?"); args.append(since_ts)
        if not force:
            where.append("(j.prompt_event_id IS NULL OR j.locale != ?)")
            args.append(self.locale)
        sql = f"""
            SELECT e.id, e.session_id, e.ts, e.payload_json
            FROM events e
            LEFT JOIN llm_judgments j ON j.prompt_event_id = e.id
            WHERE {" AND ".join(where)}
            ORDER BY e.ts ASC
        """
        rows = self.db._conn.execute(sql, args).fetchall()
        clean: list[sqlite3.Row] = []
        for r in rows:
            try:
                text = json.loads(r["payload_json"] or "{}").get("text", "")
            except (json.JSONDecodeError, TypeError):
                text = ""
            if is_synthetic_prompt(text):
                continue
            clean.append(r)
        return clean

    # ---- context assembly ----

    def build_context(self, prompt_row: sqlite3.Row) -> PromptContext:
        prompt_event_id = prompt_row["id"]
        session_id = prompt_row["session_id"]
        user_text = (json.loads(prompt_row["payload_json"]).get("text") or "")

        # Pull the turn: events in this session with seq > prompt.seq and
        # seq < next user_prompt.seq.
        events = self._turn_events(prompt_event_id, session_id)

        assistant_parts: list[str] = []
        tool_counts: dict[str, int] = {}
        tool_successes = 0
        tool_calls = 0
        tool_errors = 0
        file_write_events: list[Event] = []
        cost_tokens = 0
        assistant_tokens = 0
        file_write_bytes = 0
        file_read_paths: list[str] = []
        file_write_paths: list[str] = []
        for ev in events:
            if ev.type is EventType.ASSISTANT_MESSAGE:
                cost_tokens += ev.effective_cost_tokens
                assistant_tokens += ev.tokens_out
                t = ev.payload.get("text") or ""
                if t:
                    assistant_parts.append(t)
            elif ev.type is EventType.POST_TOOL_USE:
                tool_calls += 1
                if ev.payload.get("success"):
                    tool_successes += 1
                else:
                    tool_errors += 1
                name = ev.payload.get("tool_name") or "?"
                tool_counts[name] = tool_counts.get(name, 0) + 1
            elif ev.type is EventType.FILE_WRITE:
                file_write_bytes += int(ev.payload.get("bytes") or 0)
                file_write_events.append(ev)
                path = ev.payload.get("path")
                if path:
                    file_write_paths.append(path)
            elif ev.type is EventType.FILE_READ:
                path = ev.payload.get("path")
                if path:
                    file_read_paths.append(path)

        assistant_text = "\n\n".join(assistant_parts)
        tool_summary = ", ".join(
            f"{name}={n}" for name, n in sorted(tool_counts.items(), key=lambda kv: -kv[1])
        ) or "(no tool calls)"
        file_summary = self._render_file_summary(file_write_events)

        return PromptContext(
            prompt_event_id=prompt_event_id,
            session_id=session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            tool_summary=tool_summary,
            file_summary=file_summary,
            cost_tokens=cost_tokens,
            assistant_tokens=assistant_tokens,
            file_write_bytes=file_write_bytes,
            tool_calls=tool_calls,
            tool_successes=tool_successes,
            tool_errors=tool_errors,
            tool_counts=tool_counts,
            file_reads=len(file_read_paths),
            unique_files_read=len(set(file_read_paths)),
            file_writes=len(file_write_paths),
            unique_files_written=len(set(file_write_paths)),
        )

    def _turn_events(self, prompt_event_id: str, session_id: str) -> list[Event]:
        """Events belonging to this prompt's turn — from its seq up to the
        next USER_PROMPT (exclusive) or end of session."""
        prompt = self.db._conn.execute(
            "SELECT seq FROM events WHERE id = ?", (prompt_event_id,),
        ).fetchone()
        if prompt is None:
            return []
        next_prompt = self.db._conn.execute(
            """SELECT MIN(seq) AS s FROM events
               WHERE session_id = ? AND type = 'user_prompt' AND seq > ?""",
            (session_id, prompt["seq"]),
        ).fetchone()
        ceiling = next_prompt["s"] if next_prompt and next_prompt["s"] is not None else (1 << 31)

        rows = self.db._conn.execute(
            """SELECT * FROM events
               WHERE session_id = ? AND seq > ? AND seq < ?
               ORDER BY seq ASC""",
            (session_id, prompt["seq"], ceiling),
        ).fetchall()
        out: list[Event] = []
        for r in rows:
            out.append(Event(
                id=r["id"], session_id=r["session_id"], seq=r["seq"], ts=r["ts"],
                type=EventType(r["type"]),
                payload=json.loads(r["payload_json"]),
                parent_ids=tuple(json.loads(r["parent_ids_json"])),
                tokens_in=r["tokens_in"], tokens_out=r["tokens_out"],
                cached_tokens=r["cached_tokens"],
                cache_creation_tokens=r["cache_creation_tokens"],
                model=r["model"], latency_ms=r["latency_ms"],
            ))
        return out

    def _render_file_summary(self, file_events: list[Event]) -> str:
        if not file_events:
            return "(no files written)"
        lines: list[str] = []
        for ev in file_events[:12]:
            path = ev.payload.get("path") or "?"
            size = int(ev.payload.get("bytes") or 0)
            lines.append(f"- {path}  ({size:,} bytes)")
            # Try to include a small sample of the file's current content,
            # if it still exists on disk. Clamp to 600 chars so the judge
            # prompt stays small.
            sample = self._read_sample(path)
            if sample:
                indent = "    "
                lines.append(indent + "```")
                for s_line in sample.splitlines()[:12]:
                    lines.append(indent + s_line[:180])
                lines.append(indent + "```")
        if len(file_events) > 12:
            lines.append(f"... and {len(file_events) - 12} more files")
        return "\n".join(lines)

    def _read_sample(self, path: str, max_bytes: int = 600) -> str | None:
        try:
            p = Path(path)
            if not p.is_absolute() and self.file_root is not None:
                p = self.file_root / path
            if not p.exists() or not p.is_file():
                return None
            # Skip anything suspicious-looking.
            if p.stat().st_size > 5_000_000:
                return None
            return p.read_text(encoding="utf-8", errors="replace")[:max_bytes]
        except Exception:
            return None

    # ---- judging ----

    def judge_one(self, ctx: PromptContext) -> Judgment:
        """Single prompt → single Judgment. Does NOT persist; caller does."""
        user_msg = ctx.render_user_message()
        # Soft-cap the user message length. Local models vary; 6K char ≈ 1.5K tokens.
        if len(user_msg) > 16000:
            user_msg = user_msg[:16000] + "\n\n[... truncated for judge context ...]"
        resp = self.llm.chat(
            messages=[
                {"role": "system", "content": _judge_system_prompt(self.locale)},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
            response_format=JUDGMENT_RESPONSE_FORMAT,
        )
        parsed = _parse_judgment_json(resp.text)
        # Normalize the "no code was produced" case back to None so
        # downstream consumers can still distinguish "code was bad" from
        # "no code at all" without a second flag.
        code_was_produced = bool(parsed.get("code_was_produced", True))
        raw_code_score = parsed.get("code_quality_score")
        code_quality: float | None
        if not code_was_produced:
            code_quality = None
        else:
            code_quality = _bounded_or_none(raw_code_score)

        # Normalize wasteful_patterns: must be a list of strings, capped at
        # 10 items and 200 chars each so a runaway LLM can't dump essays.
        raw_patterns = parsed.get("wasteful_patterns") or []
        if isinstance(raw_patterns, str):
            raw_patterns = [raw_patterns]
        patterns: list[str] = []
        for p in raw_patterns[:10]:
            if isinstance(p, str):
                s = p.strip()
                if s:
                    patterns.append(s[:200])
        if not patterns:
            patterns = ["(no waste signals returned by judge)"]

        return Judgment(
            prompt_event_id=ctx.prompt_event_id,
            meaningful_value=_bounded(parsed.get("meaningful_value_score")),
            code_quality=code_quality,
            output_durability=_bounded(parsed.get("output_durability")),
            efficiency=_bounded(parsed.get("efficiency")),
            reasoning=(parsed.get("reasoning") or "")[:2000],
            wasteful_patterns=patterns,
            model=resp.model,
        )

    # ---- session summaries ----

    def sessions_needing_summary(self, *, force: bool = False) -> list[str]:
        """Return session ids that still need an LLM-generated name.

        Importer pre-populates ``session_summaries`` with a
        ``metadata-only`` placeholder row (empty name + empty summary +
        project_slug + employee_id) so downstream joins don't lose
        sessions that haven't been named yet. A naive
        ``LEFT JOIN … WHERE s.session_id IS NULL`` filter misses those
        placeholders and leaves the dashboard showing anonymous UUIDs
        forever. The ``name = ''`` / ``model = 'metadata-only'`` guards
        correctly flag placeholder rows as needing naming.

        Locale-aware: a session whose cached name was generated in a
        different locale than the one the namer is about to run under
        is re-queued so the dashboard can render cleanly in one language.
        """
        if force:
            return self.db.all_sessions()
        rows = self.db._conn.execute(
            """SELECT DISTINCT e.session_id
                 FROM events e
                 LEFT JOIN session_summaries s ON s.session_id = e.session_id
                WHERE s.session_id IS NULL
                   OR s.name IS NULL
                   OR s.name = ''
                   OR s.model = 'metadata-only'
                   OR s.locale != ?
                ORDER BY e.session_id""",
            (self.locale,),
        ).fetchall()
        return [r["session_id"] for r in rows]

    def summarize_session(self, session_id: str) -> SessionSummary:
        """Build context from a session's prompts + files and ask the LLM
        for a short name + one-line summary.

        Sessions with no user prompts (transient CLI sessions from
        `token-roi query` / `compress` / `index-memory`) skip the LLM call
        and get a deterministic name from their actual event content.
        """
        prompts = self.db._conn.execute(
            """SELECT id, payload_json, ts FROM events
                WHERE session_id = ? AND type = 'user_prompt'
                ORDER BY seq ASC""",
            (session_id,),
        ).fetchall()
        prompt_texts: list[str] = []
        for r in prompts:
            t = json.loads(r["payload_json"]).get("text", "") or ""
            if t.strip():
                prompt_texts.append(_trim(t, 800))

        if not prompt_texts:
            # Promptless: classify by event mix, rule-based. Faster than
            # spending an LLM call on a session with nothing to judge.
            return self._name_promptless_session(session_id)

        file_rows = self.db._conn.execute(
            """SELECT json_extract(payload_json, '$.path')  AS path,
                      COUNT(*)                               AS writes,
                      SUM(json_extract(payload_json, '$.bytes')) AS bytes
                 FROM events
                WHERE session_id = ? AND type = 'file_write'
                GROUP BY path
                ORDER BY bytes DESC
                LIMIT 15""",
            (session_id,),
        ).fetchall()
        file_lines = [
            f"- {r['path']}  ({int(r['bytes'] or 0):,} bytes, {r['writes']}x)"
            for r in file_rows if r["path"]
        ] or ["(no files written)"]

        tool_rows = self.db._conn.execute(
            """SELECT json_extract(payload_json, '$.tool_name') AS name,
                      COUNT(*) AS n
                 FROM events
                WHERE session_id = ? AND type = 'post_tool_use'
                GROUP BY name
                ORDER BY n DESC""",
            (session_id,),
        ).fetchall()
        tool_summary = ", ".join(f"{r['name']}={r['n']}" for r in tool_rows) or "(no tools)"

        first = prompt_texts[0] if prompt_texts else "(no prompts)"
        last = prompt_texts[-1] if len(prompt_texts) > 1 else ""
        middle_preview = [_trim(p, 200) for p in prompt_texts[1:-1][:5]]

        ctx_parts = [
            "# SESSION CONTEXT",
            f"session_id: {session_id}",
            f"prompt_count: {len(prompt_texts)}",
            "",
            "# FIRST USER PROMPT",
            first,
        ]
        if middle_preview:
            ctx_parts += ["", "# OTHER PROMPTS (first lines)"]
            ctx_parts += [f"- {p}" for p in middle_preview]
        if last:
            ctx_parts += ["", "# LAST USER PROMPT", last]
        ctx_parts += [
            "",
            "# TOOL USAGE",
            tool_summary,
            "",
            "# FILES PRODUCED (top 15 by bytes)",
            "\n".join(file_lines),
        ]
        ctx = "\n".join(ctx_parts)
        if len(ctx) > 14000:
            ctx = ctx[:14000] + "\n\n[... truncated ...]"

        resp = self.llm.chat(
            messages=[
                {"role": "system", "content": _session_summary_system_prompt(self.locale)},
                {"role": "user",   "content": ctx},
            ],
            temperature=0.2,
            max_tokens=500,
            response_format=SESSION_SUMMARY_RESPONSE_FORMAT,
        )
        parsed = _parse_judgment_json(resp.text)
        name = (parsed.get("name") or "").strip() or f"session {session_id[:8]}"
        summary = (parsed.get("summary") or "").strip()
        return SessionSummary(
            session_id=session_id,
            name=_safe_name(name),
            summary=summary[:300],
            model=resp.model,
        )

    def _name_promptless_session(self, session_id: str) -> SessionSummary:
        """Deterministic name for sessions that have no user prompts.

        These are transient sessions the CLI creates when it runs
        `query` / `compress` / `index-memory` without an explicit session id.
        Their actual content is reflected in specific event types — we use
        the dominant event kind as the name.
        """
        row = self.db._conn.execute(
            """
            SELECT
                COUNT(*)                                    AS events,
                SUM(type = 'retrieval_query')               AS queries,
                SUM(type = 'compression_run')               AS compressions,
                SUM(type = 'memory_write')                  AS mem_writes,
                SUM(type = 'memory_read')                   AS mem_reads,
                SUM(type = 'file_write')                    AS file_writes
              FROM events WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
        counts = {k: (row[k] or 0) for k in row.keys() if k != "events"}
        evt_count = row["events"] or 0

        # Priority: compression > retrieval > memory I/O > file ops > empty
        zh = self.locale == "zh"
        if counts.get("compressions", 0) > 0:
            n_mw = counts.get("mem_writes", 0)
            if zh:
                name = "记忆压缩"
                summary = f"执行记忆压缩，共生成 {n_mw} 个主题文件。"
            else:
                name = "Compression pass"
                summary = f"Ran memory compression producing {n_mw} topic file(s)."
        elif counts.get("queries", 0) > 0:
            query_row = self.db._conn.execute(
                """SELECT json_extract(payload_json, '$.query') AS q
                     FROM events
                    WHERE session_id = ? AND type = 'retrieval_query'
                    ORDER BY seq ASC LIMIT 1""",
                (session_id,),
            ).fetchone()
            q = (query_row["q"] or "").strip() if query_row else ""
            if q:
                trimmed = q if len(q) <= 38 else q[:35].rstrip() + "..."
                if zh:
                    name = f'检索：「{trimmed}」'
                    summary = f'对「{q[:120]}」执行混合检索'
                else:
                    name = f'Query: "{trimmed}"'
                    summary = f'Hybrid retrieval for "{q[:120]}"'
            else:
                if zh:
                    name = "检索会话"
                    summary = f"{counts['queries']} 次检索查询，无下游工作。"
                else:
                    name = "Retrieval query"
                    summary = f"{counts['queries']} retrieval query(s), no downstream work."
        elif counts.get("mem_writes", 0) > 0:
            if zh:
                name = "记忆写入"
                summary = f"{counts['mem_writes']} 次记忆写入，无用户提示。"
            else:
                name = "Memory write pass"
                summary = f"{counts['mem_writes']} memory write(s), no user prompt."
        elif counts.get("file_writes", 0) > 0:
            if zh:
                name = "文件写入"
                summary = f"{counts['file_writes']} 次文件写入，无用户提示。"
            else:
                name = "File write pass"
                summary = f"{counts['file_writes']} file write(s) with no user prompt."
        elif evt_count <= 2:
            if zh:
                name = "仅会话启动"
                summary = "会话已开启又关闭，无其他活动。"
            else:
                name = "Session start only"
                summary = "Session opened and closed with no other activity."
        else:
            if zh:
                name = "记账会话"
                summary = f"{evt_count} 个事件，无用户提示。"
            else:
                name = "Bookkeeping session"
                summary = f"{evt_count} events with no user prompt."
        return SessionSummary(
            session_id=session_id, name=name, summary=summary,
            model="rule-based",
        )

    def summarize_all(
        self, *, force: bool = False, progress: bool = True,
    ) -> Iterator[SessionSummary]:
        sessions = self.sessions_needing_summary(force=force)
        for i, sid in enumerate(sessions, 1):
            t0 = time.time()
            try:
                summary = self.summarize_session(sid)
            except Exception as e:  # noqa: BLE001
                log.error("summarize_session failed on %s: %s", sid, e)
                if progress:
                    print(f"[{i}/{len(sessions)}] {sid[:12]}  ERROR: {e}")
                continue
            self.db.upsert_session_summary(summary)
            if progress:
                print(f"[{i}/{len(sessions)}] {sid[:12]}  {summary.name!r}  "
                      f"({time.time() - t0:.1f}s)")
            yield summary

    # ---- project summaries ----

    def summarize_project(self, project_slug: str) -> "ProjectSummary":
        """Ask the LLM for a human-readable name + description of a project.

        A "project" groups every session that shares a Claude Code
        project slug (path-encoded cwd). We feed the LLM the slug, the
        session-name list, and the top files written across sessions —
        that's enough for it to name the project concretely (e.g.
        "Bossify tooling" or "CLRS lesson site").
        """
        session_rows = self.db._conn.execute(
            """SELECT session_id, name, summary
                 FROM session_summaries
                WHERE project_slug = ?""",
            (project_slug,),
        ).fetchall()
        sids = [r["session_id"] for r in session_rows]
        if not sids:
            return ProjectSummary(
                slug=project_slug,
                display_name=_slug_to_display(project_slug),
                description="",
                model="rule-based",
            )

        placeholders = ",".join("?" for _ in sids)
        file_rows = self.db._conn.execute(
            f"""SELECT json_extract(payload_json, '$.path') AS path,
                       SUM(json_extract(payload_json, '$.bytes')) AS bytes,
                       COUNT(*) AS writes
                  FROM events
                 WHERE session_id IN ({placeholders}) AND type = 'file_write'
                 GROUP BY path
                 ORDER BY bytes DESC
                 LIMIT 20""",
            sids,
        ).fetchall()

        session_lines = [
            f"- [{r['name'] or r['session_id'][:8]}] {(r['summary'] or '').strip()[:140]}"
            for r in session_rows[:25]
        ]
        file_lines = [
            f"- {r['path']}  ({int(r['bytes'] or 0):,} bytes, {r['writes']}x)"
            for r in file_rows if r["path"]
        ] or ["(no files written)"]

        ctx = "\n".join([
            "# PROJECT CONTEXT",
            f"slug (path-encoded cwd): {project_slug}",
            f"sessions: {len(sids)}",
            "",
            "# SESSION NAMES + SUMMARIES",
            *session_lines,
            "",
            "# TOP FILES WRITTEN (across all sessions)",
            *file_lines,
        ])
        if len(ctx) > 12000:
            ctx = ctx[:12000] + "\n\n[... truncated ...]"

        resp = self.llm.chat(
            messages=[
                {"role": "system", "content": _project_summary_system_prompt(self.locale)},
                {"role": "user",   "content": ctx},
            ],
            temperature=0.2,
            max_tokens=400,
            response_format=SESSION_SUMMARY_RESPONSE_FORMAT,
        )
        parsed = _parse_judgment_json(resp.text)
        name = (parsed.get("name") or "").strip() or _slug_to_display(project_slug)
        summary = (parsed.get("summary") or "").strip()
        return ProjectSummary(
            slug=project_slug,
            display_name=_safe_name(name),
            description=summary[:300],
            model=resp.model,
        )

    def summarize_projects(
        self, *, force: bool = False, progress: bool = True,
    ) -> Iterator["ProjectSummary"]:
        slugs = (
            self.db._conn.execute(
                """SELECT DISTINCT project_slug FROM session_summaries
                    WHERE project_slug IS NOT NULL AND project_slug != ''"""
            ).fetchall()
            if force else None
        )
        if force:
            pending = [r["project_slug"] for r in slugs]
        else:
            pending = self.db.projects_needing_name(locale=self.locale)
        for i, slug in enumerate(pending, 1):
            t0 = time.time()
            try:
                ps = self.summarize_project(slug)
            except Exception as e:  # noqa: BLE001
                log.error("summarize_project failed on %s: %s", slug, e)
                if progress:
                    print(f"[{i}/{len(pending)}] {slug[:32]}  ERROR: {e}")
                continue
            self.db.upsert_project(
                slug=ps.slug,
                display_name=ps.display_name,
                description=ps.description,
                model=ps.model,
                generated_at=time.time(),
            )
            if progress:
                print(f"[{i}/{len(pending)}] {slug[:32]:<32}  "
                      f"{ps.display_name!r}  ({time.time() - t0:.1f}s)")
            yield ps

    def judge_all(
        self,
        *,
        session_id: str | None = None,
        since_ts: float | None = None,
        limit: int | None = None,
        force: bool = False,
        progress: bool = True,
    ) -> Iterator[Judgment]:
        """Iterate over un-judged prompts, judging each one."""
        rows = self.prompts_needing_judgment(
            session_id=session_id, since_ts=since_ts, force=force,
        )
        if limit is not None:
            rows = rows[:limit]
        total = len(rows)
        for i, r in enumerate(rows, 1):
            ctx = self.build_context(r)
            t0 = time.time()
            try:
                judgment = self.judge_one(ctx)
            except Exception as e:  # noqa: BLE001
                log.error("judge failed on %s: %s", ctx.prompt_event_id, e)
                if progress:
                    print(f"[{i}/{total}] {ctx.prompt_event_id[:12]}  ERROR: {e}")
                continue
            elapsed = time.time() - t0
            self.db.upsert_llm_judgment(judgment)
            yield judgment
            if progress:
                print(
                    f"[{i}/{total}] {ctx.prompt_event_id[:12]}  "
                    f"meaning={judgment.meaningful_value:.2f} "
                    f"durability={judgment.output_durability:.2f} "
                    f"code={judgment.code_quality if judgment.code_quality is None else f'{judgment.code_quality:.2f}'} "
                    f"({elapsed:.1f}s)"
                )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _trim(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} chars truncated]"


def _safe_name(name: str) -> str:
    """Clamp LLM-produced names to something reasonable for UI columns.

    Permit ASCII letters/digits/basic punctuation AND the CJK ranges so
    Chinese session names work. Also permit full-width Chinese punctuation
    characters commonly used in titles (「」《》：·，。). Strip anything
    else (emoji, control chars, zero-width joiners) and clamp length for
    chart labels.
    """
    import re
    # Chinese block (main CJK), plus commonly-used full-width punctuation.
    pattern = (
        r"[^A-Za-z0-9 _.,:/\-&()+"
        r"\u4e00-\u9fff"                  # CJK Unified Ideographs
        r"\u3000-\u303f"                  # CJK symbols / punctuation
        r"\uff00-\uff5e"                  # Full-width Latin + punctuation
        r"\u2018\u2019\u201c\u201d"       # Curly quotes
        r"「」『』《》〈〉·]"
    )
    name = re.sub(pattern, "", name).strip()
    # CJK chars are visually ~2x wider than latin; clamp both cases.
    # Count CJK chars as 2 visual units.
    visual = sum(2 if 0x4e00 <= ord(ch) <= 0x9fff else 1 for ch in name)
    if visual > 45:
        out = []
        seen = 0
        for ch in name:
            add = 2 if 0x4e00 <= ord(ch) <= 0x9fff else 1
            if seen + add > 42:
                break
            out.append(ch)
            seen += add
        name = "".join(out).rstrip() + "..."
    return name or "unnamed session"


def _bounded(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


def _bounded_or_none(v) -> float | None:
    if v is None or v == "null":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


def _parse_judgment_json(text: str) -> dict:
    """Best-effort JSON extraction from an LLM response.

    Most local models comply with response_format=json_object, but some
    still prepend chatter. We strip code fences and look for the first
    balanced `{...}` block.
    """
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        # drop optional leading "json\n"
        if t.lower().startswith("json"):
            t = t[4:]
    # Fast path: whole string is JSON.
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Fallback: find the first balanced brace region.
    start = t.find("{")
    if start < 0:
        raise ValueError(f"no JSON object in LLM output: {text[:200]!r}")
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        raise ValueError(f"unterminated JSON in LLM output: {text[:200]!r}")
    return json.loads(t[start:end + 1])
