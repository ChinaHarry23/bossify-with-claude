"""Internationalization module for bossify-with-claude (新时代老板).

Currently supports two locales:
    en — English (the original / developer-facing language)
    zh — Simplified Chinese, framed for management / productivity audit use

Locale resolution order (first hit wins):
    1. explicit argument passed to `get_locale()` / `t()`
    2. TOKEN_ROI_LOCALE environment variable
    3. default: "en"

Usage
-----

    from token_roi.i18n import t, get_locale, set_locale

    set_locale("zh")          # process-wide switch
    print(t("ui.total_flow"))  # "总Token流量"

    # Ad-hoc per-call:
    print(t("ui.total_flow", locale="zh"))

All user-facing strings in the dashboard, CLI, and LLM prompts flow through
this module. Strings that are *not* user-facing (code comments, log lines,
raw event payloads) stay in English because they are developer artifacts.
"""
from __future__ import annotations

import os
from typing import Any


SUPPORTED_LOCALES = ("en", "zh")
DEFAULT_LOCALE = "en"


# The translation catalog is kept inline here rather than in separate JSON
# files so the package has zero IO cost to boot and so diffing a locale
# change is a single file review. At ~200 strings per locale this is easy
# to maintain by hand; if it grows past ~1000 we should split.
_CATALOG: dict[str, dict[str, str]] = {
    "en": {
        # ---- brand / top bar ----
        "brand.name":             "Bossify with Claude",
        "brand.sub":              "新时代老板 · local-first flight recorder + ROI analyzer",
        "brand.data_dir_label":   "data dir:",
        "brand.last_refreshed":   "last refreshed",

        # ---- KPI row ----
        "kpi.total_flow":         "total AI usage",
        "kpi.total_flow_sub":     "tokens across all events",
        "kpi.sessions":           "coding sessions",
        "kpi.events_subline":     "{events} events · {tools} tools · {mem_writes} mem writes",
        "kpi.flow_subline":       "in {tokens_in} · out {tokens_out} · cache {cache}",
        "kpi.high_value":         "worth the spend",
        "kpi.high_value_sub":     "durable + reused",
        "kpi.transient":          "one-time wins",
        "kpi.transient_sub":      "produced output, never reused",
        "kpi.low_value":          "poor return",
        "kpi.low_value_sub":      "cost > value",
        "kpi.wasted":             "money burnt",
        "kpi.wasted_sub":         "nothing durable produced",

        # ---- card headers ----
        "card.roi_distribution":      "ROI distribution",
        "card.cost_breakdown":        "Per-session token breakdown",
        "card.top_spenders":          "Top token spenders (colored by ROI class)",
        "card.timeline":              "Token flow over time (30-min buckets)",
        "card.memory_scatter":        "Memory effectiveness (bytes × retrieval hits)",
        "card.tools":                 "Tool usage (treemap)",
        "card.black_holes":           "Token black holes (low value-per-token sessions)",
        "card.llm_verdicts":          "Local-LLM verdicts",
        "card.query":                 "Hybrid query",
        "card.hint_click_bar":        "click a bar to inspect",
        "card.hint_click_bar_session":"click a bar to inspect its session",
        "card.hint_click":            "click to inspect",

        # ---- ROI class labels ----
        "roi.HIGH_VALUE":      "HIGH_VALUE",
        "roi.TRANSIENT_VALUE": "TRANSIENT_VALUE",
        "roi.LOW_VALUE":       "LOW_VALUE",
        "roi.WASTED":          "WASTED",

        # ---- tooltips + chart labels ----
        "chart.no_events":          "no events",
        "chart.no_memory":          "no memory writes",
        "chart.no_tools":           "no tool calls",
        "chart.no_black_holes":     "no LOW_VALUE or WASTED sessions over cost threshold",
        "chart.no_black_holes_sub": "your sessions are classified HIGH_VALUE or TRANSIENT",
        "chart.bytes":              "bytes",
        "chart.retrieval_hits":     "retrieval hits",
        "chart.class_label":        "class",
        "chart.score_label":        "score",

        # ---- query form ----
        "form.query_placeholder":   "auth middleware rewrite",
        "form.query_button":        "query",
        "form.querying":            "querying",
        "form.no_matches":          "no matches",
        "form.query_failed":        "query failed",

        # ---- LLM verdicts list ----
        "llm.no_judgments":     ("no LLM judgments yet. Run `token-roi judge` to evaluate "
                                 "prompts with a local LLM (LM Studio / Ollama)."),
        "llm.not_yet_run":      "not yet run",
        "llm.judgments_meta":   "{n} judgments · model{s}: {models} · avg meaningful {meaningful} · avg durability {durability}",
        "llm.meaningful":       "meaningful",
        "llm.durability":       "durability",
        "llm.efficiency":       "efficiency",
        "llm.code":             "code",
        "llm.cost":             "cost",
        "llm.na":               "n/a",
        "llm.session_prefix":   "session",
        "llm.no_material_waste": "no material waste found",

        # ---- modal (drill-down) ----
        "modal.loading":          "Loading",
        "modal.close_hint":       "Press Esc or click outside to close.",
        "modal.unscored":         "UNSCORED",
        "modal.stale_banner":     "The dashboard process is running old code.",
        "modal.stale_bullet_1":   "The client JS expects fields this server version doesn't return.",
        "modal.stale_bullet_2":   "Stop the dashboard (Ctrl-C) and restart: `token-roi dashboard`.",
        "modal.stale_bullet_3":   "Then hard-refresh this page (Cmd-Shift-R).",
        "modal.load_failed":      "Failed to load session detail.",
        "modal.restart_hint":     "If the dashboard was running from a previous version, restart it (`token-roi dashboard`), then hard-refresh (Cmd-Shift-R).",
        "modal.legacy_subtitle":  "legacy server response",
        "modal.stale_badge":      "STALE",
        "modal.no_prompts":       "This session has no user prompts. See the rule-based name + event summary above.",
        "modal.why_prefix":       "Why this is",
        "modal.waste_flagged":    "Waste the judge flagged:",
        "modal.no_signals":       "No strong positive or negative signals — the session is middling by every axis.",
        "modal.section.totals":   "Session totals",
        "modal.section.prompts":  "Prompts ({n})",
        "modal.section.files":    "Top files produced ({n})",
        "modal.section.tools":    "Tool usage",
        "sort.by_cost":           "by cost",
        "sort.chronological":     "in order asked",
        "modal.metric.tokens_out":    "tokens out",
        "modal.metric.cache_read":    "cache read",
        "modal.metric.cache_create":  "cache create",
        "modal.metric.prompts":       "prompts",
        "modal.metric.tool_calls":    "tool calls",
        "modal.metric.memory_writes": "memory writes",
        "modal.prompt.file_writes":   "file writes",
        "modal.prompt.tool_calls":    "tool calls",
        "modal.prompt.retrieval_hits":"retrieval hits",
        "modal.prompt.no_llm":        "no LLM judgment yet — run `token-roi judge`",
        "modal.tool_calls_label":     "calls",
        "modal.errors_label":         "errors",
        "modal.ok_label":             "ok",
        "modal.prompt.expand_hint":   "click to view full prompt + token breakdown",
        "modal.prompt.collapse_hint": "click to collapse",
        "modal.prompt.full_prompt":   "Full prompt",
        "modal.prompt.token_breakdown":"Token breakdown",
        "modal.prompt.tokens_in":     "input",
        "modal.prompt.tokens_out":    "output",
        "modal.prompt.cache_read":    "cache read",
        "modal.prompt.cache_create":  "cache create",
        "modal.prompt.tokens_total":  "total",
        "modal.prompt.model":         "model",
        "modal.prompt.timestamp":     "timestamp",
        "modal.prompt.text_truncated":"prompt was truncated to 50KB for display",
        "modal.cost.click_hint":      "click to see how this dollar figure was computed",
        "modal.cost.collapse_hint":   "click to collapse breakdown",
        "modal.cost.title":           "Cost breakdown",
        "modal.cost.subtitle":        "tokens × per-model rate, summed across {n} model{s}",
        "modal.cost.col_category":    "category",
        "modal.cost.col_tokens":      "tokens",
        "modal.cost.col_rate":        "rate / 1M",
        "modal.cost.col_subtotal":    "subtotal",
        "modal.cost.cat_input":       "input",
        "modal.cost.cat_output":      "output",
        "modal.cost.cat_cache_read":  "cache read",
        "modal.cost.cat_cache_create":"cache create",
        "modal.cost.model_total":     "model total",
        "modal.cost.session_total":   "Session total",
        "modal.cost.no_billed":       "no billed events for this session",

        # Inline English tokens previously baked into chart tooltips and the
        # ROI math row. Each is its own key so translators can swap exact
        # wording without touching code.
        "chart.tooltip.bytes":        "bytes",
        "chart.tooltip.hits":         "hits",
        "chart.tooltip.calls":        "calls",
        "chart.tooltip.errors":       "errors",
        "llm.aggregate_label":        "LLM aggregate",
        "modal.prompt.ok_badge":      "ok {n}",
        "employee.llm_metric":        "LLM",
        "aria.close":                 "close",
        "form.query_placeholder_default": "auth middleware rewrite",

        # ROI-formula terms shown in the math row at the bottom of the
        # "Why this is …" panel. We translate them because the audit view
        # is user-facing, not a debug dump.
        "derivation.numerator":       "numerator",
        "derivation.denominator":     "denominator",
        "derivation.w_durable":       "w_durable",
        "derivation.w_reuse":         "w_reuse",
        "derivation.w_outcome":       "w_outcome",
        "derivation.w_llm":           "w_llm",
        "derivation.cost_unit":       "cost_unit",
        "derivation.penalty":         "penalty",
        "derivation.score":           "score",

        # ---- why-panel bullet phrases ----
        "why.llm_aggregate":      "The local LLM rated the aggregated work at {score} across {n} prompt(s) — the strongest non-reuse signal available.",
        "why.llm_efficiency":     "Average efficiency {score} — the LLM thought the tokens were well spent relative to the output.",
        "why.file_writes":        "Produced {bytes} bytes of file writes — real durable output.",
        "why.durable_memory":     "Wrote {bytes} bytes to memory/ — retrievable across future sessions.",
        "why.meaningful_flag":    "The local LLM rated the work meaningful ({score}) but flagged efficiency as the drag.",
        "why.efficiency_drag":    "Average efficiency {score} — the output was good but the token spend was bloated.",
        "why.cost_excess":        "Effective cost {tokens} tokens — a \"reasonable agentic turn\" is ~150K, so this spent {ratio}× that budget.",
        "why.low_meaningful":     "The local LLM rated the meaningful content at only {score} — it didn't see substantive work.",
        "why.zero_durable":       "Zero durable output: no memory writes, no file writes.",
        "why.zero_reuse":         "No cross-session retrieval hit this session's artifacts — whatever was produced is currently orphan.",
        "why.low_return":         "{tokens} effective tokens spent for low-return output.",

        # ---- tabs ----
        "tab.team":           "Overview",
        "tab.employees":      "People",
        "tab.projects":       "Projects",
        "tab.advanced":       "Technical detail",

        # ---- team view ----
        "team.heading":                "Team Overview",
        "team.active_employees":       "people using AI",
        "team.total_spend":            "total spent",
        "team.total_spend_sub":        "estimated USD cost (per-model priced)",
        "team.cost_per_kb":            "cost per KB of code shipped",
        "team.cost_per_kb_sub":        "total USD ÷ durable bytes produced",
        "team.avg_efficiency":         "efficiency score",
        "team.avg_efficiency_sub":     "0 = wasteful, 1 = lean (LLM-judged)",
        "team.high_value_sessions":    "great sessions",
        "team.high_value_sessions_sub":"durable output, strong LLM verdict",
        "team.waste_alerts":           "problem sessions",
        "team.waste_alerts_sub":       "burnt money, little to show",
        "team.top_waste_patterns":     "biggest causes of waste (AI reviewer)",
        "team.no_patterns_yet":        "no waste patterns yet — run `token-roi judge` to populate.",
        "team.roi_distribution":       "where did the money go?",
        "team.roi_click_hint":         "click to filter the list below",
        "team.leaderboard":            "biggest money burners — sessions that spent the most for the least",
        "team.leaderboard_filtered_by":   "filtering",
        "team.leaderboard_clear_filter":  "clear filter",
        "team.leaderboard_no_sessions_in_class": "no sessions in this category",

        # ---- projects view ----
        "projects.heading":         "Projects",
        "projects.empty":           "no projects yet. Run `token-roi import claude-code` then `token-roi name-projects` to group and name them.",
        "projects.unnamed_hint":    "run `token-roi name-projects` to label this",
        "projects.sessions":        "sessions",
        "projects.durable_bytes":   "code shipped",
        "projects.cost_per_kb":     "cost per KB",
        "projects.roi_mix":         "outcome mix",
        "projects.model_mix":       "model mix",
        "projects.description":     "what this project is",

        # ---- employees view ----
        "employees.heading":           "Employees",
        "employees.empty":             "no employees yet. Run `token-roi import claude-code` to ingest sessions.",
        "employee.session_count":      "sessions",
        "employee.total_cost":         "total cost",
        "employee.avg_efficiency":     "avg efficiency",
        "employee.role":               "role",
        "employee.team":               "team",
        "employee.view_details":       "view details",
        "employee.no_sessions":        "no sessions yet",
        "employee.main_waste":         "main waste patterns",
        "employee.roi_mix":            "ROI mix",
        "employee.last_active":        "last active",

        # ---- rating badges ----
        "rating.top":             "Top performer",
        "rating.normal":          "Normal",
        "rating.needs_attention": "Needs attention",
        "rating.unjudged":        "Not yet judged",

        # ---- generic / shared ----
        "unit.tokens":       "tokens",
        "unit.bytes":        "bytes",
        "unit.session":      "session",
        "unit.sessions":     "sessions",
        "common.loading":    "Loading",
        "common.empty":      "(empty)",
    },

    # ------------------------------------------------------------------
    # 中文 — Simplified Chinese translation.
    # Framed for Chinese management auditing AI-assisted developer output:
    # term choices favor "Token 投入产出 / 会话 / 产出 / 效率" rather than
    # looser colloquial terms, because the audience is managers who want
    # actionable productivity numbers, not developers.
    # ------------------------------------------------------------------
    "zh": {
        # ---- brand / top bar ----
        "brand.name":             "新时代老板",
        "brand.sub":              "本地优先 · 会话完整审计 · 投入产出评估",
        "brand.data_dir_label":   "数据目录：",
        "brand.last_refreshed":   "最近刷新",

        # ---- KPI row ----
        "kpi.total_flow":         "AI 总用量",
        "kpi.total_flow_sub":     "涵盖所有事件",
        "kpi.sessions":           "编码会话",
        "kpi.events_subline":     "{events} 个事件 · {tools} 次工具调用 · {mem_writes} 次记忆写入",
        "kpi.flow_subline":       "输入 {tokens_in} · 输出 {tokens_out} · 缓存 {cache}",
        "kpi.high_value":         "花得值",
        "kpi.high_value_sub":     "产出持久且被复用",
        "kpi.transient":          "一次性成果",
        "kpi.transient_sub":      "有产出但未复用",
        "kpi.low_value":          "回报偏低",
        "kpi.low_value_sub":      "成本大于价值",
        "kpi.wasted":             "钱打水漂",
        "kpi.wasted_sub":         "没留下任何持久成果",

        # ---- card headers ----
        "card.roi_distribution":      "ROI 分布",
        "card.cost_breakdown":        "各会话 Token 用量构成",
        "card.top_spenders":          "Token 消耗榜首（按 ROI 分类着色）",
        "card.timeline":              "Token 流量时间趋势（30 分钟分桶）",
        "card.memory_scatter":        "记忆有效性（字节 × 检索命中）",
        "card.tools":                 "工具使用分布（矩形树图）",
        "card.black_holes":           "Token 黑洞（低效会话）",
        "card.llm_verdicts":          "本地 LLM 评审结果",
        "card.query":                 "混合检索",
        "card.hint_click_bar":        "点击柱状图查看详情",
        "card.hint_click_bar_session":"点击查看所属会话",
        "card.hint_click":            "点击查看详情",

        # ---- ROI class labels ----
        "roi.HIGH_VALUE":      "高价值",
        "roi.TRANSIENT_VALUE": "过渡价值",
        "roi.LOW_VALUE":       "低价值",
        "roi.WASTED":          "浪费",

        # ---- tooltips + chart labels ----
        "chart.no_events":          "暂无事件",
        "chart.no_memory":          "暂无记忆写入",
        "chart.no_tools":           "暂无工具调用",
        "chart.no_black_holes":     "暂无达到成本阈值的低价值会话",
        "chart.no_black_holes_sub": "您的会话均被评为高价值或过渡价值",
        "chart.bytes":              "字节数",
        "chart.retrieval_hits":     "检索命中",
        "chart.class_label":        "分类",
        "chart.score_label":        "得分",

        # ---- query form ----
        "form.query_placeholder":   "权限中间件重构",
        "form.query_button":        "检索",
        "form.querying":            "检索中",
        "form.no_matches":          "未找到匹配结果",
        "form.query_failed":        "检索失败",

        # ---- LLM verdicts list ----
        "llm.no_judgments":     ("尚无本地 LLM 评审结果。运行 `token-roi judge` "
                                 "让本地 LLM（LM Studio / Ollama）评估每个提示的价值。"),
        "llm.not_yet_run":      "未运行",
        "llm.judgments_meta":   "共 {n} 条评审 · 模型：{models} · 平均实质性 {meaningful} · 平均持久性 {durability}",
        "llm.meaningful":       "实质性",
        "llm.durability":       "持久性",
        "llm.efficiency":       "效率",
        "llm.code":             "代码质量",
        "llm.cost":             "成本",
        "llm.na":               "无",
        "llm.session_prefix":   "会话",
        "llm.no_material_waste": "未发现重大浪费",

        # ---- modal (drill-down) ----
        "modal.loading":          "加载中",
        "modal.close_hint":       "按 Esc 或点击外部关闭。",
        "modal.unscored":         "未评分",
        "modal.stale_banner":     "仪表板进程运行的是旧版代码。",
        "modal.stale_bullet_1":   "前端 JS 期望的字段，当前服务端版本未返回。",
        "modal.stale_bullet_2":   "停止仪表板（Ctrl-C）并重启：`token-roi dashboard`。",
        "modal.stale_bullet_3":   "然后强制刷新本页（Cmd-Shift-R）。",
        "modal.load_failed":      "加载会话详情失败。",
        "modal.restart_hint":     "如果仪表板运行的是旧版本，请重启（`token-roi dashboard`），然后强制刷新（Cmd-Shift-R）。",
        "modal.legacy_subtitle":  "旧版服务端响应",
        "modal.stale_badge":      "过期",
        "modal.no_prompts":       "该会话没有用户提示。上方的规则化命名和事件摘要即为全部内容。",
        "modal.why_prefix":       "评定为",
        "modal.waste_flagged":    "评审员标记的浪费：",
        "modal.no_signals":       "无明显正负信号——该会话在各维度均属中等水平。",
        "modal.section.totals":   "会话合计",
        "modal.section.prompts":  "提示列表（共 {n} 条）",
        "modal.section.files":    "生成文件榜首（共 {n} 个）",
        "modal.section.tools":    "工具使用情况",
        "sort.by_cost":           "按成本",
        "sort.chronological":     "按顺序",
        "modal.metric.tokens_out":    "输出 Token",
        "modal.metric.cache_read":    "缓存读取",
        "modal.metric.cache_create":  "缓存创建",
        "modal.metric.prompts":       "提示数",
        "modal.metric.tool_calls":    "工具调用",
        "modal.metric.memory_writes": "记忆写入",
        "modal.prompt.file_writes":   "文件写入",
        "modal.prompt.tool_calls":    "工具调用",
        "modal.prompt.retrieval_hits":"检索命中",
        "modal.prompt.no_llm":        "尚未评审——运行 `token-roi judge`",
        "modal.tool_calls_label":     "次调用",
        "modal.errors_label":         "次错误",
        "modal.ok_label":             "无错误",
        "modal.prompt.expand_hint":   "点击查看完整提示与 Token 明细",
        "modal.prompt.collapse_hint": "点击收起",
        "modal.prompt.full_prompt":   "完整提示内容",
        "modal.prompt.token_breakdown":"Token 明细",
        "modal.prompt.tokens_in":     "输入",
        "modal.prompt.tokens_out":    "输出",
        "modal.prompt.cache_read":    "缓存读取",
        "modal.prompt.cache_create":  "缓存创建",
        "modal.prompt.tokens_total":  "合计",
        "modal.prompt.model":         "模型",
        "modal.prompt.timestamp":     "时间戳",
        "modal.prompt.text_truncated":"提示内容已截断至 50KB 以便显示",
        "modal.cost.click_hint":      "点击查看美元成本是如何计算的",
        "modal.cost.collapse_hint":   "点击收起明细",
        "modal.cost.title":           "成本明细",
        "modal.cost.subtitle":        "Token × 每模型单价，跨 {n} 个模型求和",
        "modal.cost.col_category":    "类别",
        "modal.cost.col_tokens":      "Token 数",
        "modal.cost.col_rate":        "单价 / 1M",
        "modal.cost.col_subtotal":    "小计",
        "modal.cost.cat_input":       "输入",
        "modal.cost.cat_output":      "输出",
        "modal.cost.cat_cache_read":  "缓存读取",
        "modal.cost.cat_cache_create":"缓存创建",
        "modal.cost.model_total":     "模型小计",
        "modal.cost.session_total":   "会话合计",
        "modal.cost.no_billed":       "本会话无可计费事件",

        "chart.tooltip.bytes":        "字节",
        "chart.tooltip.hits":         "次命中",
        "chart.tooltip.calls":        "次调用",
        "chart.tooltip.errors":       "次错误",
        "llm.aggregate_label":        "LLM 综合",
        "modal.prompt.ok_badge":      "成功 {n}",
        "employee.llm_metric":        "LLM 评分",
        "aria.close":                 "关闭",
        "form.query_placeholder_default": "认证中间件重构",

        "derivation.numerator":       "分子",
        "derivation.denominator":     "分母",
        "derivation.w_durable":       "持久权重",
        "derivation.w_reuse":         "复用权重",
        "derivation.w_outcome":       "结果权重",
        "derivation.w_llm":           "LLM 权重",
        "derivation.cost_unit":       "成本单位",
        "derivation.penalty":         "惩罚项",
        "derivation.score":           "评分",

        # ---- why-panel bullet phrases ----
        "why.llm_aggregate":      "本地 LLM 对 {n} 条提示的综合工作评分为 {score}——现有的最强内容感知信号。",
        "why.llm_efficiency":     "平均效率 {score}——LLM 认为相对于产出，Token 花得合理。",
        "why.file_writes":        "产出文件写入 {bytes} 字节——真实的持久产出。",
        "why.durable_memory":     "写入 {bytes} 字节到 memory/——未来会话可检索。",
        "why.meaningful_flag":    "本地 LLM 认为工作有实质性（{score}），但将效率标记为拖累。",
        "why.efficiency_drag":    "平均效率 {score}——产出质量尚可，但 Token 花费明显膨胀。",
        "why.cost_excess":        "有效成本 {tokens} Tokens——"
                                  "一个「合理的智能体回合」约为 15 万，本会话用了预算的 {ratio} 倍。",
        "why.low_meaningful":     "本地 LLM 给实质性内容的评分仅为 {score}——未见到实质性工作。",
        "why.zero_durable":       "零持久产出：无记忆写入，无文件写入。",
        "why.zero_reuse":         "本会话的产物尚未被任何跨会话检索命中——目前处于孤立状态。",
        "why.low_return":         "花费 {tokens} 有效 Tokens 换取低回报产出。",

        # ---- tabs ----
        "tab.team":           "总览",
        "tab.employees":      "成员",
        "tab.projects":       "项目",
        "tab.advanced":       "技术细节",

        # ---- team view ----
        "team.heading":                "团队总览",
        "team.active_employees":       "使用 AI 的成员",
        "team.total_spend":            "总花费",
        "team.total_spend_sub":        "按模型分别计价的估算美元成本",
        "team.cost_per_kb":            "每 KB 代码的花费",
        "team.cost_per_kb_sub":        "总美元 ÷ 产出的持久代码字节数",
        "team.avg_efficiency":         "效率评分",
        "team.avg_efficiency_sub":     "0 = 浪费，1 = 精干（由 LLM 评审）",
        "team.high_value_sessions":    "优秀会话",
        "team.high_value_sessions_sub":"产出持久、LLM 评分高",
        "team.waste_alerts":           "问题会话",
        "team.waste_alerts_sub":       "钱花了但没什么成果",
        "team.top_waste_patterns":     "最常见浪费原因（由 AI 评审识别）",
        "team.no_patterns_yet":        "暂无浪费模式 — 运行 `token-roi judge` 后生成。",
        "team.roi_distribution":       "钱花去哪儿了？",
        "team.roi_click_hint":         "点击按此类别筛选下方列表",
        "team.leaderboard":            "最烧钱的会话 — 按「花得最多、产出最少」排序",
        "team.leaderboard_filtered_by":   "已筛选",
        "team.leaderboard_clear_filter":  "清除筛选",
        "team.leaderboard_no_sessions_in_class": "该分类下暂无会话",

        # ---- projects view ----
        "projects.heading":         "项目",
        "projects.empty":           "暂无项目。先运行 `token-roi import claude-code`，再运行 `token-roi name-projects` 来归组并命名。",
        "projects.unnamed_hint":    "运行 `token-roi name-projects` 以生成名称",
        "projects.sessions":        "会话",
        "projects.durable_bytes":   "产出代码",
        "projects.cost_per_kb":     "每 KB 花费",
        "projects.roi_mix":         "产出分布",
        "projects.model_mix":       "模型构成",
        "projects.description":     "项目简介",

        # ---- employees view ----
        "employees.heading":           "员工",
        "employees.empty":             "暂无员工数据。运行 `token-roi import claude-code` 导入会话。",
        "employee.session_count":      "会话数",
        "employee.total_cost":         "Token 消耗",
        "employee.avg_efficiency":     "平均效率",
        "employee.role":               "角色",
        "employee.team":               "团队",
        "employee.view_details":       "查看详情",
        "employee.no_sessions":        "暂无会话",
        "employee.main_waste":         "主要浪费模式",
        "employee.roi_mix":            "ROI 分布",
        "employee.last_active":        "最近活跃",

        # ---- rating badges ----
        "rating.top":             "表现优异",
        "rating.normal":          "正常",
        "rating.needs_attention": "需关注",
        "rating.unjudged":        "尚未评审",

        # ---- generic / shared ----
        "unit.tokens":       "Tokens",
        "unit.bytes":        "字节",
        "unit.session":      "会话",
        "unit.sessions":     "会话",
        "common.loading":    "加载中",
        "common.empty":      "（空）",
    },
}


# Process-wide default locale. set_locale() mutates this; individual calls
# can still override via the `locale=` kwarg.
_current_locale = os.environ.get("TOKEN_ROI_LOCALE", DEFAULT_LOCALE)
if _current_locale not in SUPPORTED_LOCALES:
    _current_locale = DEFAULT_LOCALE


def get_locale() -> str:
    """Return the current effective locale."""
    return _current_locale


def set_locale(locale: str) -> None:
    """Change the process-wide default locale. Unknown values fall back to en."""
    global _current_locale
    if locale in SUPPORTED_LOCALES:
        _current_locale = locale
    else:
        _current_locale = DEFAULT_LOCALE


def t(key: str, *, locale: str | None = None, **fmt: Any) -> str:
    """Translate a key, optionally formatting with {name} placeholders.

    If a key is missing in the target locale it falls back to English so
    we never render an empty string to the user. If it's missing in both,
    the key itself is returned (so missing entries are easy to spot).
    """
    target = locale or _current_locale
    catalog = _CATALOG.get(target) or _CATALOG["en"]
    value = catalog.get(key) or _CATALOG["en"].get(key) or key
    if fmt:
        try:
            return value.format(**fmt)
        except (KeyError, IndexError, ValueError):
            # If formatting fails (missing kwarg, stray brace), fall back to
            # the unformatted string rather than crashing the dashboard.
            return value
    return value


def all_strings(locale: str | None = None) -> dict[str, str]:
    """Return a full copy of the translation dict for a locale.

    Used by the /api/i18n endpoint so the frontend can do client-side
    lookup without hitting the server per string.
    """
    target = locale or _current_locale
    catalog = _CATALOG.get(target) or _CATALOG["en"]
    # Merge onto EN base so missing keys in zh are filled with EN values
    # (the same fallback rule as `t()`).
    merged = dict(_CATALOG["en"])
    merged.update(catalog)
    return merged
