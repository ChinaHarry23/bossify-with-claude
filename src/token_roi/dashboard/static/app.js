// Token ROI dashboard frontend — ECharts-driven, no build step.
//
// Design:
//   - Each card owns one ECharts instance, resized on window.resize.
//   - Data fetches are parallel on load; each chart renders independently
//     so a slow endpoint doesn't block the rest.
//   - A shared THEME keeps colors in sync with CSS variables.
//   - Locale is bootstrapped once via /api/i18n; every user-facing string
//     flows through T(key, …), which formats Python-style {name} placeholders.

let I18N = {
  locale: "en",
  strings: {},
  // Server sets a URL-query-parameter ?locale=xx that survives across page
  // loads; this lets the locale-switch links in the header work without
  // any cookie or server session.
};

function T(key, params) {
  const raw = (I18N.strings && I18N.strings[key]) || key;
  if (!params) return raw;
  return raw.replace(/\{(\w+)\}/g, (_, k) =>
    params[k] != null ? params[k] : `{${k}}`);
}

async function loadI18n() {
  // Honor ?locale=xx in the query string so the two <a> links in the
  // header act as a locale switcher.
  const qs = new URLSearchParams(window.location.search);
  const localeParam = qs.get("locale");
  const url = "/api/i18n" + (localeParam ? "?locale=" + encodeURIComponent(localeParam) : "");
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error("i18n HTTP " + r.status);
    const body = await r.json();
    I18N = body;
  } catch (e) {
    console.warn("i18n load failed, using English defaults", e);
  }
  // Paint the active locale link, then apply translations to every
  // element carrying a data-i18n attribute.
  document.documentElement.lang = I18N.locale || "en";
  document.querySelectorAll(".locale-switch a").forEach((a) => {
    if (a.dataset.locale === I18N.locale) a.classList.add("active");
  });
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    if (key) el.textContent = T(key);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const key = el.getAttribute("data-i18n-placeholder");
    if (key) el.setAttribute("placeholder", T(key));
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const key = el.getAttribute("data-i18n-title");
    if (key) el.setAttribute("title", T(key));
  });
}

const THEME = {
  bg:        "#0b0e13",
  panel:     "#151b23",
  line:      "#242c38",
  text:      "#e6edf3",
  muted:     "#7d8794",
  accent:    "#58a6ff",
  high:      "#3fb950",
  transient: "#d29922",
  low:       "#db6d28",
  wasted:    "#f85149",
};

const ROI_COLORS = {
  HIGH_VALUE:      THEME.high,
  TRANSIENT_VALUE: THEME.transient,
  LOW_VALUE:       THEME.low,
  WASTED:          THEME.wasted,
  null:            THEME.muted,
};

const $  = (sel) => document.querySelector(sel);
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
const fmtCompact = (n) => {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
};

async function json(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

// Common ECharts option fragments so every chart has consistent styling.
const baseGrid = { left: 40, right: 20, top: 24, bottom: 30, containLabel: true };
const baseTextStyle = { color: THEME.text, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: 11 };
const baseTooltip = {
  backgroundColor: THEME.panel,
  borderColor: THEME.line,
  textStyle: { color: THEME.text, fontFamily: baseTextStyle.fontFamily, fontSize: 11 },
  padding: [8, 10],
};

// ECharts instance registry keeps references so we can .resize() on window resize.
const instances = {};

function mount(id, opts) {
  const el = document.getElementById(id);
  if (!el) return null;
  const chart = echarts.init(el, null, { renderer: "canvas" });
  chart.setOption(opts);
  instances[id] = chart;
  return chart;
}

window.addEventListener("resize", () => {
  Object.values(instances).forEach((c) => c.resize());
});

// ---- charts ----

async function loadKpis() {
  const k = await json("/api/kpis");
  $("#kpi-total").textContent     = fmtCompact(k.total_tokens);
  $("#kpi-total-sub").textContent = T("kpi.flow_subline", {
    tokens_in:  fmtCompact(k.tokens_in),
    tokens_out: fmtCompact(k.tokens_out),
    cache:      fmtCompact(k.cache_read + k.cache_create),
  });
  $("#kpi-sessions").textContent = fmt(k.sessions);
  $("#kpi-events-sub").textContent = T("kpi.events_subline", {
    events:     fmt(k.events),
    tools:      fmt(k.tool_calls),
    mem_writes: fmt(k.memory_writes),
  });
  $("#kpi-high").textContent      = fmt(k.high_value);
  $("#kpi-transient").textContent = fmt(k.transient);
  $("#kpi-low").textContent       = fmt(k.low_value);
  $("#kpi-wasted").textContent    = fmt(k.wasted);

  $("#last-updated").textContent =
    T("brand.last_refreshed") + " " + new Date().toLocaleTimeString();
  return k;
}

async function loadRoiChart() {
  const roi = await json("/api/roi/summary");
  // Force all four classes to appear even when count is 0 so the donut
  // shows the full class palette consistently. Labels go through T() so
  // Chinese users see 高价值 / 过渡价值 / 低价值 / 浪费.
  const keys = ["HIGH_VALUE", "TRANSIENT_VALUE", "LOW_VALUE", "WASTED"];
  const data = keys.map((k) => ({
    name: T("roi." + k), value: roi[k] || 0,
    itemStyle: { color: ROI_COLORS[k] },
  }));
  const total = data.reduce((a, b) => a + b.value, 0);
  mount("chart-roi", {
    tooltip: { ...baseTooltip, trigger: "item", formatter: (p) =>
      `${p.name}<br/>${fmt(p.value)} (${((p.value / (total || 1)) * 100).toFixed(1)}%)` },
    legend: { bottom: 4, textStyle: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10 } },
    series: [{
      type: "pie",
      radius: ["52%", "78%"],
      center: ["50%", "44%"],
      avoidLabelOverlap: false,
      label: { show: true, color: THEME.text, fontFamily: baseTextStyle.fontFamily,
               formatter: "{b}\n{c}" },
      labelLine: { lineStyle: { color: THEME.muted } },
      data,
    }],
  });
}

// Prefer a human-readable name when available; fall back to the first 12
// chars of the UUID so even un-named sessions stay recognisable.
function sessionLabel(r) {
  return (r.name && r.name.trim())
    ? r.name
    : (r.session_id ? r.session_id.slice(0, 12) : "session");
}

async function loadCostChart() {
  const rows = await json("/api/cost-breakdown");
  const sessions = rows.map(sessionLabel);
  // Keep the raw rows around so the click handler can resolve session_id
  // from the y-axis dataIndex without re-fetching.
  loadCostChart._rows = rows;
  // Segment names follow the English token-accounting vocabulary even in
  // zh because these are Anthropic API billing buckets — managers benefit
  // from using the same words as the billing dashboard.
  const series = [
    { name: "input",       color: THEME.accent,    key: "tokens_in"    },
    { name: "output",      color: "#a371f7",       key: "tokens_out"   },
    { name: "cache_create",color: "#ec6547",       key: "cache_create" },
    { name: "cache_read",  color: "#2a7ecb",       key: "cache_read"   },
  ].map((s) => ({
    name: s.name,
    type: "bar",
    stack: "tokens",
    emphasis: { focus: "series" },
    itemStyle: { color: s.color },
    data: rows.map((r) => r[s.key] || 0),
  }));
  mount("chart-cost", {
    tooltip: { ...baseTooltip, trigger: "axis", axisPointer: { type: "shadow" },
               valueFormatter: fmt },
    legend: { textStyle: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10 }, top: 0 },
    grid: { ...baseGrid, top: 30 },
    xAxis: {
      type: "value",
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, formatter: fmtCompact },
      splitLine: { lineStyle: { color: THEME.line } },
    },
    yAxis: {
      type: "category",
      data: sessions,
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10 },
      axisLine: { lineStyle: { color: THEME.line } },
    },
    series,
  });
  wireSessionClick("chart-cost", (p) => {
    const row = (loadCostChart._rows || [])[p.dataIndex];
    return row && row.session_id;
  });
}

async function loadTopSpendersChart() {
  const rows = await json("/api/top-spenders");
  rows.reverse();  // so highest cost appears on top in horizontal bar
  loadTopSpendersChart._rows = rows;
  const categories = rows.map((r, i) => {
    const sn = r.session_name || (r.session_id ? r.session_id.slice(0, 8) : "?");
    const cls = r.class ? T("roi." + r.class) : "?";
    const txt = (r.text || "").slice(0, 40).replace(/\s+/g, " ");
    return `${cls} · ${sn} · ${txt}`;
  });
  const data = rows.map((r) => ({
    value: r.cost_tokens,
    itemStyle: { color: ROI_COLORS[r.class] || THEME.muted },
  }));
  mount("chart-top-spenders", {
    tooltip: {
      ...baseTooltip, trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (arr) => {
        const p = arr[0];
        const r = rows[p.dataIndex];
        const text = (r.text || "").replace(/</g, "&lt;").slice(0, 400);
        const sn = r.session_name
          ? `${r.session_name} <span style="color:${THEME.muted}">(${r.session_id.slice(0, 8)})</span>`
          : r.session_id.slice(0, 12);
        const clsLabel = r.class ? T("roi." + r.class) : "—";
        return `<div style="max-width:480px;white-space:normal">
                  <b>${clsLabel}</b>  ${T("chart.score_label")} ${r.score != null ? r.score.toFixed(3) : "—"}<br/>
                  ${T("llm.cost")}: ${fmt(r.cost_tokens)} ${T("unit.tokens")}<br/>
                  ${T("unit.session")}: ${sn}<br/>
                  <div style="color:${THEME.muted};margin-top:4px">${text}</div>
                </div>`;
      },
    },
    grid: { ...baseGrid, left: 520, right: 30 },
    xAxis: {
      type: "value",
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, formatter: fmtCompact },
      splitLine: { lineStyle: { color: THEME.line } },
    },
    yAxis: {
      type: "category",
      data: categories,
      axisLabel: {
        color: THEME.muted,
        fontFamily: baseTextStyle.fontFamily,
        fontSize: 10,
        width: 500,
        overflow: "truncate",
      },
      axisLine: { lineStyle: { color: THEME.line } },
    },
    series: [{ type: "bar", data, barWidth: "65%" }],
  });
  wirePromptClick("chart-top-spenders", (p) => {
    const row = (loadTopSpendersChart._rows || [])[p.dataIndex];
    return row && { session_id: row.session_id, prompt_id: row.prompt_id };
  });
}

async function loadTimelineChart() {
  const rows = await json("/api/timeline?bucket_minutes=30");
  if (!rows.length) {
    mount("chart-timeline", { title: { text: T("chart.no_events"), textStyle: { color: THEME.muted } } });
    return;
  }
  const xs = rows.map((r) => new Date(r.ts * 1000).toISOString().slice(0, 16).replace("T", " "));
  const series = [
    { name: "cache_read",   color: "#2a7ecb", key: "cache_read"   },
    { name: "cache_create", color: "#ec6547", key: "cache_create" },
    { name: "output",       color: "#a371f7", key: "tokens_out"   },
    { name: "input",        color: THEME.accent, key: "tokens_in"  },
  ].map((s) => ({
    name: s.name,
    type: "line",
    stack: "tokens",
    smooth: true,
    showSymbol: false,
    areaStyle: { opacity: 0.6 },
    lineStyle: { width: 0 },
    emphasis: { focus: "series" },
    itemStyle: { color: s.color },
    data: rows.map((r) => r[s.key] || 0),
  }));
  mount("chart-timeline", {
    tooltip: { ...baseTooltip, trigger: "axis", valueFormatter: fmt },
    legend: { textStyle: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10 }, top: 0 },
    grid: { ...baseGrid, top: 30 },
    xAxis: {
      type: "category",
      data: xs,
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: THEME.line } },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, formatter: fmtCompact },
      splitLine: { lineStyle: { color: THEME.line } },
    },
    series,
  });
}

async function loadMemoryScatterChart() {
  const rows = await json("/api/memory-scatter");
  if (!rows.length) {
    mount("chart-memory-scatter", { title: { text: T("chart.no_memory"), textStyle: { color: THEME.muted } } });
    return;
  }
  // Group by ROI class so each class becomes its own series (and own legend entry).
  const groups = {};
  for (const r of rows) {
    const cls = r.class || "UNSCORED";
    if (!groups[cls]) groups[cls] = [];
    // ECharts scatter expects [x, y, ...extras] with a name for tooltip.
    groups[cls].push({
      value: [Math.max(r.bytes, 1), r.hits],
      name: r.path || r.id,
      score: r.score,
    });
  }
  const series = Object.entries(groups).map(([cls, data]) => ({
    name: cls,
    type: "scatter",
    symbolSize: (v) => Math.max(8, Math.min(28, 8 + Math.sqrt(v[1] || 0) * 6)),
    itemStyle: { color: ROI_COLORS[cls] || THEME.muted, opacity: 0.85 },
    data,
  }));
  mount("chart-memory-scatter", {
    tooltip: {
      ...baseTooltip, trigger: "item",
      formatter: (p) => `<b>${p.seriesName}</b><br/>${p.name}<br/>${fmt(p.value[0])} bytes · ${fmt(p.value[1])} hits`,
    },
    legend: { textStyle: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10 }, top: 0 },
    grid: { ...baseGrid, top: 30 },
    xAxis: {
      type: "log", name: T("chart.bytes"), nameTextStyle: { color: THEME.muted, fontSize: 10 },
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, formatter: fmtCompact },
      splitLine: { lineStyle: { color: THEME.line } },
    },
    yAxis: {
      type: "value", name: T("chart.retrieval_hits"), nameTextStyle: { color: THEME.muted, fontSize: 10 },
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily },
      splitLine: { lineStyle: { color: THEME.line } },
    },
    series,
  });
}

async function loadToolsChart() {
  const rows = await json("/api/tool-usage");
  if (!rows.length) {
    mount("chart-tools", { title: { text: T("chart.no_tools"), textStyle: { color: THEME.muted } } });
    return;
  }
  const data = rows.map((r) => ({
    name: r.name,
    value: r.value,
    errors: r.errors,
    itemStyle: {
      color: r.errors > 0
        ? THEME.wasted
        : `hsl(${(r.name.charCodeAt(0) * 31) % 360}, 45%, 45%)`,
    },
  }));
  mount("chart-tools", {
    tooltip: {
      ...baseTooltip,
      formatter: (p) => `<b>${p.name}</b><br/>${fmt(p.value)} calls · ${fmt(p.data.errors)} errors`,
    },
    series: [{
      type: "treemap",
      width: "100%",
      height: "100%",
      roam: false,
      breadcrumb: { show: false },
      label: {
        show: true,
        color: THEME.text,
        fontFamily: baseTextStyle.fontFamily,
        fontSize: 11,
        formatter: "{b}\n{c}",
      },
      itemStyle: { borderColor: THEME.bg, borderWidth: 2, gapWidth: 2 },
      upperLabel: { show: false },
      data,
    }],
  });
}

async function loadBlackHolesChart() {
  const rows = await json("/api/black-holes");
  if (!rows.length) {
    mount("chart-black-holes", {
      title: {
        text:    T("chart.no_black_holes"),
        subtext: T("chart.no_black_holes_sub"),
        textStyle:    { color: THEME.muted, fontSize: 11 },
        subtextStyle: { color: THEME.muted, fontSize: 10 },
        left: "center", top: "center",
      },
    });
    return;
  }
  loadBlackHolesChart._rows = rows;
  const cats = rows.map(sessionLabel);
  mount("chart-black-holes", {
    tooltip: {
      ...baseTooltip, trigger: "axis", axisPointer: { type: "shadow" },
      formatter: (arr) => {
        const r = rows[arr[0].dataIndex];
        const head = r.name ? `<b>${escapeHtml(r.name)}</b><br/>
                               <span style="color:${THEME.muted}">${r.session_id.slice(0, 12)}</span>`
                            : `<b>${r.session_id.slice(0, 12)}</b>`;
        const cls = r.roi_class || "UNSCORED";
        const clsLabel = r.roi_class ? T("roi." + r.roi_class) : T("modal.unscored");
        const score = r.roi_score != null ? ` · ${T("chart.score_label")} ${Number(r.roi_score).toFixed(3)}` : "";
        return `${head}<br/>
                ${T("chart.class_label")}: <b class="cls-${cls}">${clsLabel}</b>${score}<br/>
                ${T("llm.cost")}: ${fmt(r.total_cost)}<br/>
                ${T("modal.prompt.file_writes")}: ${fmt(r.total_file_writes || 0)} ${T("unit.bytes")}<br/>
                ${T("modal.metric.memory_writes")}: ${fmt(r.total_durable)}<br/>
                ${T("chart.retrieval_hits")}: ${fmt(r.total_reuse)}`;
      },
    },
    grid: { ...baseGrid, left: 100 },
    xAxis: {
      type: "value",
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, formatter: fmtCompact },
      splitLine: { lineStyle: { color: THEME.line } },
    },
    yAxis: {
      type: "category",
      data: cats,
      axisLabel: { color: THEME.muted, fontFamily: baseTextStyle.fontFamily, fontSize: 10 },
      axisLine: { lineStyle: { color: THEME.line } },
    },
    series: [{
      type: "bar",
      // Color each bar by the session's actual ROI class so WASTED
      // sessions read as red and LOW_VALUE as orange — same palette the
      // KPI cards use.
      data: rows.map((r) => ({
        value: r.total_cost,
        itemStyle: {
          color: r.roi_class === "WASTED"    ? THEME.wasted
               : r.roi_class === "LOW_VALUE" ? THEME.low
               : THEME.muted,
          opacity: 0.9,
        },
      })),
      barWidth: "60%",
    }],
  });
  wireSessionClick("chart-black-holes", (p) => {
    const row = (loadBlackHolesChart._rows || [])[p.dataIndex];
    return row && row.session_id;
  });
}

// ---- LLM verdicts ----

async function loadLlmJudgments() {
  const [rows, summary] = await Promise.all([
    json("/api/llm-judgments"),
    json("/api/llm-summary"),
  ]);
  const list = $("#llm-list");
  if (!rows.length) {
    list.innerHTML = `<div class="query-results"><div class="empty">
      ${T("llm.no_judgments")}
    </div></div>`;
    $("#llm-model").textContent = T("llm.not_yet_run");
    return;
  }
  const models = [...new Set(rows.map((r) => r.model))];
  $("#llm-model").textContent = T("llm.judgments_meta", {
    n:          rows.length,
    s:          models.length > 1 ? "s" : "",
    models:     models.join(", "),
    meaningful: (summary.avg_meaningful || 0).toFixed(2),
    durability: (summary.avg_durability || 0).toFixed(2),
  });

  const rowHtml = rows.map((r) => {
    const agg = r.aggregate ?? 0;
    const badgeCls =
      r.roi_class === "HIGH_VALUE"      ? "high" :
      r.roi_class === "TRANSIENT_VALUE" ? "transient" :
      r.roi_class === "LOW_VALUE"       ? "low" :
      r.roi_class === "WASTED"          ? "wasted" : "";
    const code = r.code_quality == null ? T("llm.na") : r.code_quality.toFixed(2);
    const shortCls = r.roi_class ? T("roi." + r.roi_class) : "—";
    return `
      <div class="llm-row clickable"
           data-session-id="${escapeHtml(r.session_id)}"
           data-prompt-id="${escapeHtml(r.prompt_id)}">
        <div class="score-badge ${badgeCls}">
          <div class="agg">${agg.toFixed(2)}</div>
          <div class="label">${escapeHtml(shortCls)}</div>
        </div>
        <div class="body">
          <div class="prompt-text">${escapeHtml(r.text)}</div>
          <div class="meta">
            <span>${T("llm.cost")}: <b>${fmtCompact(r.cost_tokens)}</b></span>
            <span>${T("llm.meaningful")}: <b class="cls-${r.meaningful_value >= 0.7 ? 'HIGH_VALUE' : r.meaningful_value >= 0.4 ? 'TRANSIENT_VALUE' : 'WASTED'}">${r.meaningful_value.toFixed(2)}</b></span>
            <span>${T("llm.durability")}: <b>${r.output_durability.toFixed(2)}</b></span>
            <span>${T("llm.efficiency")}: <b class="cls-${r.efficiency >= 0.7 ? 'HIGH_VALUE' : r.efficiency >= 0.4 ? 'TRANSIENT_VALUE' : 'WASTED'}">${r.efficiency.toFixed(2)}</b></span>
            <span>${T("llm.code")}: <b>${code}</b></span>
            <span style="color:var(--muted)">${T("llm.session_prefix")} ${escapeHtml(r.session_name || r.session_id.slice(0, 8))}</span>
          </div>
          <div class="reasoning">${escapeHtml(r.reasoning)}</div>
          ${renderWasteList(r.wasteful_patterns)}
        </div>
      </div>`;
  }).join("");
  list.innerHTML = rowHtml;
  list.querySelectorAll(".llm-row.clickable").forEach((el) => {
    el.addEventListener("click", () => {
      openSessionDetail(el.dataset.sessionId, el.dataset.promptId);
    });
  });
}

// ---- session drill-down modal ----

const modal = {
  el:       () => $("#session-modal"),
  body:     () => $("#modal-body"),
  title:    () => $("#modal-title"),
  subtitle: () => $("#modal-subtitle"),
  clsBadge: () => $("#modal-class"),
  open() { this.el().classList.add("open"); this.el().setAttribute("aria-hidden", "false"); },
  close() { this.el().classList.remove("open"); this.el().setAttribute("aria-hidden", "true"); },
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") modal.close();
});
document.querySelectorAll("#session-modal [data-close]").forEach((el) =>
  el.addEventListener("click", () => modal.close())
);

const clsSuffix = (c) =>
  c === "HIGH_VALUE"      ? "high" :
  c === "TRANSIENT_VALUE" ? "transient" :
  c === "LOW_VALUE"       ? "low" :
  c === "WASTED"          ? "wasted" : "";

function buildWhyHtml(detail) {
  // Explain the session-level classification in plain language, sourced
  // directly from the derivation block and per-prompt LLM verdicts.
  const d = detail.roi_derivation || {};
  const num = d.numerator || {};
  const den = d.denominator || {};
  const inp = d.inputs || {};

  const prompts = Array.isArray(detail.prompts) ? detail.prompts : [];
  const judgedPrompts = prompts.filter((p) => p && p.llm);
  const judged = judgedPrompts.length;
  const avgMeaningful = judged
    ? judgedPrompts.reduce((a, p) => a + (p.llm.meaningful_value || 0), 0) / judged
    : null;
  const avgEfficiency = judged
    ? judgedPrompts.reduce((a, p) => a + (p.llm.efficiency || 0), 0) / judged
    : null;

  const bullets = [];
  if (detail.roi_class === "HIGH_VALUE") {
    bullets.push(`<span class="tag pos">+</span> ${T("why.llm_aggregate", {
      score: `<b>${(num.v_llm || 0).toFixed(2)}</b>`, n: judged,
    })}`);
    if (avgEfficiency != null && avgEfficiency >= 0.7)
      bullets.push(`<span class="tag pos">+</span> ${T("why.llm_efficiency", {
        score: `<b>${avgEfficiency.toFixed(2)}</b>`,
      })}`);
    if (inp.file_write_bytes > 0)
      bullets.push(`<span class="tag pos">+</span> ${T("why.file_writes", {
        bytes: `<b>${fmt(inp.file_write_bytes)}</b>`,
      })}`);
    if (inp.durable_bytes > 0)
      bullets.push(`<span class="tag pos">+</span> ${T("why.durable_memory", {
        bytes: `<b>${fmt(inp.durable_bytes)}</b>`,
      })}`);
  } else if (detail.roi_class === "TRANSIENT_VALUE") {
    bullets.push(`<span class="tag neu">·</span> ${T("why.meaningful_flag", {
      score: `<b>${(num.v_llm || 0).toFixed(2)}</b>`,
    })}`);
    if (avgEfficiency != null && avgEfficiency < 0.7)
      bullets.push(`<span class="tag neg">−</span> ${T("why.efficiency_drag", {
        score: `<b>${avgEfficiency.toFixed(2)}</b>`,
      })}`);
    if ((inp.cost_tokens || 0) > 1_500_000)
      bullets.push(`<span class="tag neg">−</span> ${T("why.cost_excess", {
        tokens: `<b>${fmt(inp.cost_tokens)}</b>`,
        ratio:  ((inp.cost_tokens || 0) / 150000).toFixed(1),
      })}`);
  } else if (detail.roi_class === "LOW_VALUE" || detail.roi_class === "WASTED") {
    if (avgMeaningful != null && avgMeaningful < 0.5)
      bullets.push(`<span class="tag neg">−</span> ${T("why.low_meaningful", {
        score: `<b>${avgMeaningful.toFixed(2)}</b>`,
      })}`);
    if (!inp.file_write_bytes && !inp.durable_bytes)
      bullets.push(`<span class="tag neg">−</span> ${T("why.zero_durable")}`);
    if ((inp.retrieval_count || 0) === 0)
      bullets.push(`<span class="tag neg">−</span> ${T("why.zero_reuse")}`);
    if (inp.cost_tokens > 500_000)
      bullets.push(`<span class="tag neg">−</span> ${T("why.low_return", {
        tokens: `<b>${fmt(inp.cost_tokens)}</b>`,
      })}`);
  }

  // Always surface the raw math at the bottom so everything remains auditable.
  const mathRow = `<div style="margin-top:10px;font-family:var(--mono);font-size:11px;color:var(--muted)">
      numerator = ${(num.v_durable || 0).toFixed(2)}·w_durable
               + ${(num.v_reuse || 0).toFixed(2)}·w_reuse
               + ${(num.v_outcome || 0).toFixed(2)}·w_outcome
               + ${(num.v_llm || 0).toFixed(2)}·w_llm
      &nbsp;/&nbsp; denominator = ${(den.cost_unit || 0).toFixed(2)} cost_unit + ${(den.v_negative || 0).toFixed(2)} penalty
      &nbsp;=&nbsp; <b style="color:var(--text)">score ${(detail.roi_score || 0).toFixed(3)}</b>
    </div>`;

  const bulletHtml = bullets.length
    ? `<ul>${bullets.map((b) => `<li>${b}</li>`).join("")}</ul>`
    : `<div class="muted">${T("modal.no_signals")}</div>`;

  // Roll up every prompt's wasteful_patterns into one deduplicated list so
  // the user sees the session-level waste summary without expanding each
  // prompt card. Neutral ("no material waste") placeholders are filtered
  // out of the aggregate view.
  const allWaste = [];
  for (const p of prompts) {
    if (!p.llm || !Array.isArray(p.llm.wasteful_patterns)) continue;
    for (const w of p.llm.wasteful_patterns) {
      if (/^no material waste|no waste signals/i.test(w)) continue;
      if (!allWaste.includes(w)) allWaste.push(w);
    }
  }
  const wasteHtml = allWaste.length
    ? `<div style="margin-top:10px"><b>${T("modal.waste_flagged")}</b>
         ${renderWasteList(allWaste.slice(0, 12))}
       </div>`
    : "";

  const clsLabel = detail.roi_class ? T("roi." + detail.roi_class) : T("modal.unscored");
  return `<div class="why cls-${detail.roi_class}">
    <b>${T("modal.why_prefix")} <span class="cls-${detail.roi_class}">${clsLabel}</span>:</b>
    ${bulletHtml}
    ${wasteHtml}
    ${mathRow}
  </div>`;
}

function buildMetricsHtml(detail) {
  const t = detail.totals || {};
  const cells = [
    [T("modal.metric.tokens_out"),    fmtCompact(t.tokens_out)],
    [T("modal.metric.cache_read"),    fmtCompact(t.cache_read)],
    [T("modal.metric.cache_create"),  fmtCompact(t.cache_create)],
    [T("modal.metric.prompts"),       fmt(t.prompts)],
    [T("modal.metric.tool_calls"),    fmt(t.tools)],
    [T("modal.metric.memory_writes"), fmt(t.memory_writes)],
  ];
  return `<div><div class="section-h">${T("modal.section.totals")}</div>
    <div class="metrics">${cells.map(([l, v]) => `
      <div class="metric"><div class="label">${l}</div><div class="value">${v}</div></div>
    `).join("")}</div>
  </div>`;
}

function buildPromptHtml(p, idx) {
  const clsKey = p.class || "UNSCORED";
  const llm = p.llm;
  const textRaw = p.text || "";
  const fullLen = p.text_full_length != null ? p.text_full_length : textRaw.length;
  const long = fullLen > 480;
  const text = textRaw.slice(0, 480);
  const llmMeta = llm
    ? `<div class="metrics-inline">
         <span>${T("llm.session_prefix")==="会话"?"LLM 综合":"LLM aggregate"}: <b>${llm.aggregate.toFixed(2)}</b></span>
         <span>${T("llm.meaningful")}: <b>${llm.meaningful_value.toFixed(2)}</b></span>
         <span>${T("llm.durability")}: <b>${llm.output_durability.toFixed(2)}</b></span>
         <span>${T("llm.efficiency")}: <b>${llm.efficiency.toFixed(2)}</b></span>
         ${llm.code_quality != null ? `<span>${T("llm.code")}: <b>${llm.code_quality.toFixed(2)}</b></span>` : ""}
       </div>`
    : `<div class="metrics-inline"><span class="muted">${T("modal.prompt.no_llm")}</span></div>`;
  const reason = llm && llm.reasoning
    ? `<div class="llm-reason">${escapeHtml(llm.reasoning)}</div>`
    : "";
  const waste = llm ? renderWasteList(llm.wasteful_patterns) : "";
  const clsLabel = p.class ? T("roi." + p.class) : "—";

  return `<div class="prompt-item cls-${clsKey}">
    <div class="prompt-head">
      <div class="prompt-cls cls-${clsKey}">${escapeHtml(clsLabel)}</div>
      <div class="prompt-cost">#${idx + 1} · ${T("llm.cost")} ${fmtCompact(p.cost_tokens)} · ${T("chart.score_label")} ${(p.score ?? 0).toFixed(3)}</div>
      <div></div>
    </div>
    <div class="prompt-text ${long ? "long" : ""}">${escapeHtml(text)}</div>
    <div class="metrics-inline">
      <span>${T("modal.prompt.file_writes")}: <b>${fmtCompact(p.file_write_bytes)}</b></span>
      <span>${T("modal.prompt.tool_calls")}: <b>${p.tool_calls}</b> (ok ${p.tool_successes})</span>
      <span>${T("modal.prompt.retrieval_hits")}: <b>${p.retrieval_count}</b></span>
    </div>
    ${llmMeta}
    ${reason}
    ${waste}
  </div>`;
}

function renderWasteList(patterns) {
  if (!Array.isArray(patterns) || !patterns.length) return "";
  // A single "no material waste found" string stays informational (neutral
  // chip). Anything else is a real waste callout (red chip).
  const neutral = patterns.length === 1 &&
    /^no material waste|no waste signals/i.test(patterns[0]);
  const klass = neutral ? "neutral" : "";
  const chips = patterns
    .map((p) => `<span class="chip ${klass}">${escapeHtml(p)}</span>`)
    .join("");
  return `<div class="waste-list">${chips}</div>`;
}

function buildFilesHtml(detail) {
  const files = Array.isArray(detail.top_files) ? detail.top_files : [];
  if (!files.length) return "";
  return `<div><div class="section-h">${T("modal.section.files", { n: files.length })}</div>
    <div class="strip">${files.map((f) => `
      <div class="row">
        <div class="path">${escapeHtml(f.path)}</div>
        <div class="muted">${fmtCompact(f.bytes)}</div>
        <div class="muted">${f.writes}×</div>
      </div>
    `).join("")}</div></div>`;
}

function buildToolsHtml(detail) {
  const tools = Array.isArray(detail.tools) ? detail.tools : [];
  if (!tools.length) return "";
  return `<div><div class="section-h">${T("modal.section.tools")}</div>
    <div class="strip">${tools.map((tt) => `
      <div class="row">
        <div class="path">${escapeHtml(tt.name)}</div>
        <div class="muted">${fmt(tt.count)} ${T("modal.tool_calls_label")}</div>
        <div class="${tt.errors ? 'err' : 'muted'}">${tt.errors ? tt.errors + ' ' + T("modal.errors_label") : T("modal.ok_label")}</div>
      </div>
    `).join("")}</div></div>`;
}

async function openSessionDetail(sessionId, highlightPromptId) {
  modal.body().innerHTML = `<div class="muted">${T("modal.loading")} ${escapeHtml(sessionId)}…</div>`;
  modal.title().textContent = sessionId;
  modal.subtitle().textContent = "";
  modal.clsBadge().textContent = "—";
  modal.clsBadge().className = "modal-class";
  modal.open();

  try {
    const detail = await json("/api/sessions/" + encodeURIComponent(sessionId));

    // Detect pre-enrichment server: if the response lacks `name` and
    // `top_files`, the running dashboard process is older than the client
    // JS. Surface a clear message instead of a cryptic TypeError.
    const isLegacy = detail && detail.name === undefined && detail.top_files === undefined;
    if (isLegacy) {
      modal.title().textContent = sessionId;
      modal.subtitle().textContent = T("modal.legacy_subtitle");
      modal.clsBadge().textContent = T("modal.stale_badge");
      modal.clsBadge().className = "modal-class";
      modal.body().innerHTML = `<div class="why cls-WASTED">
        <b>${T("modal.stale_banner")}</b>
        <ul>
          <li>${T("modal.stale_bullet_1")}</li>
          <li>${T("modal.stale_bullet_2")}</li>
          <li>${T("modal.stale_bullet_3")}</li>
        </ul>
      </div>`;
      return;
    }

    modal.title().textContent = detail.name || sessionId;
    modal.subtitle().textContent = detail.summary
      ? `${detail.summary}  ·  ${sessionId}`
      : sessionId;
    modal.clsBadge().textContent = detail.roi_class ? T("roi." + detail.roi_class) : T("modal.unscored");
    modal.clsBadge().className = "modal-class " + clsSuffix(detail.roi_class);

    const prompts = Array.isArray(detail.prompts) ? detail.prompts : [];
    const promptsHtml = prompts.length
      ? `<div><div class="section-h">${T("modal.section.prompts", { n: prompts.length })}</div>
          <div class="prompts-list">${prompts.map(buildPromptHtml).join("")}</div>
        </div>`
      : `<div class="muted">${T("modal.no_prompts")}</div>`;

    modal.body().innerHTML = [
      buildWhyHtml(detail),
      buildMetricsHtml(detail),
      promptsHtml,
      buildFilesHtml(detail),
      buildToolsHtml(detail),
    ].join("");

    if (highlightPromptId) {
      // Find the matching prompt-item and scroll into view with a flash.
      const nodes = modal.body().querySelectorAll(".prompt-item");
      prompts.forEach((p, i) => {
        if (p.id === highlightPromptId && nodes[i]) {
          nodes[i].scrollIntoView({ behavior: "smooth", block: "center" });
          nodes[i].style.outline = "2px solid var(--accent)";
          setTimeout(() => { nodes[i].style.outline = ""; }, 1800);
        }
      });
    }
  } catch (e) {
    console.error("session detail failed", e);
    modal.body().innerHTML = `<div class="why cls-WASTED">
      <b>${T("modal.load_failed")}</b>
      <div style="margin-top:6px">${escapeHtml(e.message)}</div>
      <div style="margin-top:6px;font-family:var(--mono);font-size:11px;color:var(--muted)">
        ${T("modal.restart_hint")}
      </div>
    </div>`;
  }
}

// Attach click handlers on the three session-level charts. Called once the
// chart is mounted — the instance is pulled from the registry.
function wireSessionClick(chartId, resolveSessionId) {
  const chart = instances[chartId];
  if (!chart) return;
  chart.off("click");
  chart.on("click", (params) => {
    const sid = resolveSessionId(params);
    if (sid) openSessionDetail(sid);
  });
}

function wirePromptClick(chartId, resolve) {
  const chart = instances[chartId];
  if (!chart) return;
  chart.off("click");
  chart.on("click", (params) => {
    const r = resolve(params);
    if (r && r.session_id) openSessionDetail(r.session_id, r.prompt_id);
  });
}

// ---- query form ----

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

$("#query-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = new FormData(e.target).get("q");
  if (!q) return;
  const box = $("#query-results");
  box.innerHTML = `<div class="empty">${T("form.querying")} "${escapeHtml(q)}"...</div>`;
  try {
    const rows = await json("/api/query?q=" + encodeURIComponent(q) + "&top_k=10");
    if (!rows.length) {
      box.innerHTML = `<div class="empty">${T("form.no_matches")}</div>`;
      return;
    }
    box.innerHTML = rows.map((r) => `
      <div class="row">
        <div class="score">${(r.score ?? 0).toFixed(3)}</div>
        <div class="kind">${escapeHtml(r.kind)}</div>
        <div class="body">${escapeHtml(r.title)} — ${escapeHtml(r.snippet)}</div>
      </div>`).join("");
  } catch (err) {
    box.innerHTML = `<div class="empty">${T("form.query_failed")}: ${escapeHtml(err.message)}</div>`;
  }
});

// ---- boot ----

// Expose the i18n-ready promise so app.manager.js (loaded immediately
// after this file) can defer any T()-using rendering until translations
// are actually in memory. Without this gate, the manager view paints
// English strings briefly before the fetch resolves.
window.i18nReady = loadI18n();

(async () => {
  // Load translations FIRST so every chart/tooltip/label uses the right
  // locale. Otherwise the first render paints English before the i18n
  // promise resolves, causing a visible flash.
  await window.i18nReady;
  const jobs = [
    loadKpis().catch((e) => console.error("kpis", e)),
    loadRoiChart().catch((e) => console.error("roi", e)),
    loadCostChart().catch((e) => console.error("cost", e)),
    loadTopSpendersChart().catch((e) => console.error("top", e)),
    loadTimelineChart().catch((e) => console.error("timeline", e)),
    loadMemoryScatterChart().catch((e) => console.error("memory", e)),
    loadToolsChart().catch((e) => console.error("tools", e)),
    loadBlackHolesChart().catch((e) => console.error("black_holes", e)),
    loadLlmJudgments().catch((e) => console.error("llm", e)),
  ];
  await Promise.all(jobs);
})();
