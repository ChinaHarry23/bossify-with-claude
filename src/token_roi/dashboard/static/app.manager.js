// Manager-view renderer + tab controller.
//
// Boots AFTER app.js so it can reuse: I18N + T(), THEME, fmt / fmtCompact,
// escapeHtml, mount / instances, ROI_COLORS, openSessionDetail.
//
// Responsibilities:
//   1. Tab switching (team / employees / advanced).
//   2. Team-overview charts (ROI donut, waste pattern list, waste leaderboard).
//   3. Employee cards grid.
//   4. Employee drill-down (reuses the existing session modal for each session).

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------

// Defer boot until app.js has loaded the i18n dictionary. The manager
// view's first render calls T() many times, so painting before the
// dictionary is present would produce a flash of unreplaced English.
(window.i18nReady || Promise.resolve()).then(mountTabs);

function mountTabs() {
  const tabs = document.querySelectorAll(".tabs .tab");
  const views = {
    team:      document.getElementById("view-team"),
    employees: document.getElementById("view-employees"),
    projects:  document.getElementById("view-projects"),
    advanced:  document.getElementById("view-advanced"),
  };

  function activate(name) {
    tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    Object.entries(views).forEach(([k, el]) => el && el.classList.toggle("active", k === name));
    // Hidden-during-init ECharts containers report zero size; once their
    // tab becomes visible we must trigger a resize so they repaint at
    // their real dimensions.
    setTimeout(() => {
      if (typeof instances === "object" && instances) {
        Object.values(instances).forEach((c) => c && c.resize && c.resize());
      }
    }, 0);
    // Remember the choice so a refresh doesn't reset the manager to the
    // technical tab mid-audit.
    try { localStorage.setItem("tokenRoi.activeTab", name); } catch (_) {}
    // Lazy-load tab content on first reveal.
    if (name === "team"      && !mountTabs._teamLoaded)      { loadTeamView();      mountTabs._teamLoaded = true; }
    if (name === "employees" && !mountTabs._employeesLoaded) { loadEmployeesView(); mountTabs._employeesLoaded = true; }
    if (name === "projects"  && !mountTabs._projectsLoaded)  { loadProjectsView();  mountTabs._projectsLoaded = true; }
  }

  tabs.forEach((t) => t.addEventListener("click", () => activate(t.dataset.tab)));
  // Restore last-active tab, defaulting to team for new visitors.
  let initial = "team";
  try { initial = localStorage.getItem("tokenRoi.activeTab") || "team"; } catch (_) {}
  activate(initial);
}

// ---------------------------------------------------------------------------
// Team view
// ---------------------------------------------------------------------------

async function loadTeamView() {
  try {
    const team = await json("/api/team");
    renderTeamKpis(team);
    renderTeamRoi(team);
    renderTeamWaste(team);
    renderTeamPlatformMix(team);
    await renderTeamLeaderboard();
  } catch (e) {
    console.error("team view load failed", e);
  }
}

// Short display label for a platform id. Keeps the raw id ("claude-code")
// as a tooltip on the chip; the shown text is a human-friendly name.
function platformLabel(id) {
  if (!id) return "—";
  const s = String(id).toLowerCase();
  const map = {
    "claude-code":  "Claude Code",
    "cursor":       "Cursor",
    "codex":        "Codex",
    "aider":        "Aider",
    "openai-jsonl": "OpenAI JSONL",
  };
  return map[s] || id;
}

// Row of platform chips: one pill per source (Claude Code, Cursor, …)
// showing USD cost, session count, and % of total. Same visual language
// as renderModelMix so the two rows stack cleanly on the same card.
function renderPlatformMix(platforms) {
  if (!platforms || !platforms.length) {
    return `<div class="muted tiny">${T("platform.empty")}</div>`;
  }
  const total = platforms.reduce((a, p) => a + (p.cost_usd || 0), 0);
  return `<div class="platform-mix">
    ${platforms.map((p) => {
      const pct = total > 0 ? (p.cost_usd / total) * 100 : 0;
      const sessLabel = T("projects.sessions");
      return `<span class="platform-chip" title="${escapeHtml(p.platform || "")}">
        <b>${escapeHtml(platformLabel(p.platform))}</b>
        <span class="muted"> ${escapeHtml(p.formatted_cost || "—")}</span>
        <span class="muted tiny"> · ${fmt(p.sessions || 0)} ${sessLabel}</span>
        <span class="muted tiny"> · ${pct.toFixed(0)}%</span>
      </span>`;
    }).join("")}
  </div>`;
}

function renderTeamPlatformMix(team) {
  const box = document.getElementById("team-platform-mix");
  if (!box) return;
  box.innerHTML = renderPlatformMix(team.platform_breakdown || []);
}

function renderTeamKpis(team) {
  const $ = (sel) => document.querySelector(sel);
  $("#team-kpi-employees").textContent = fmt(team.active_employees);
  // Boss view: USD is the hero metric, not token counts. Fall back to a
  // compact token display only if the server hasn't pre-formatted a USD
  // string (older server versions, empty DB).
  $("#team-kpi-cost").textContent      =
    team.formatted_cost || ("$" + fmtCompact(team.total_cost_usd || 0));
  // New KPI: cost per KB of durable code shipped. Answers "what did we
  // get for the spend?" in the single sharpest boss-friendly ratio.
  const ppkEl = $("#team-kpi-ppk");
  if (ppkEl) {
    ppkEl.textContent = team.formatted_cost_per_kb || "—";
  }
  $("#team-kpi-eff").textContent       =
    team.avg_efficiency == null ? "—" : Number(team.avg_efficiency).toFixed(2);
  $("#team-kpi-high").textContent      = fmt(team.high_value_sessions || 0);
  $("#team-kpi-waste").textContent     = fmt(team.waste_alerts || 0);
}

// Current ROI-class filter on the team leaderboard. Set by clicking
// a donut segment; cleared by clicking the same segment again (or the
// "clear filter" pill).
let _teamRoiFilter = null;

function renderTeamRoi(team) {
  const keys = ["HIGH_VALUE", "TRANSIENT_VALUE", "LOW_VALUE", "WASTED"];
  const roi = team.roi_totals || {};
  const data = keys.map((k) => ({
    name:  T("roi." + k),
    value: roi[k] || 0,
    // Expose the canonical class id so the click handler doesn't have
    // to re-translate T("roi.XXX") back into the enum name.
    _class: k,
    itemStyle: { color: ROI_COLORS[k] },
  }));
  const total = data.reduce((a, b) => a + b.value, 0);
  const chart = mount("chart-team-roi", {
    tooltip: {
      backgroundColor: THEME.panel, borderColor: THEME.line,
      textStyle: { color: THEME.text, fontFamily: "ui-monospace, Menlo, monospace", fontSize: 11 },
      padding: [8, 10], trigger: "item",
      formatter: (p) =>
        `${p.name}<br/>${fmt(p.value)} (${((p.value / (total || 1)) * 100).toFixed(1)}%)`
        + `<br/><span style="color:${THEME.muted}">${T("team.roi_click_hint")}</span>`,
    },
    legend: {
      bottom: 4,
      textStyle: { color: THEME.muted, fontSize: 10 },
      itemHeight: 8, itemWidth: 10,
    },
    series: [{
      type: "pie",
      // Tighter radius so outward labels never exit the 280px card.
      // Previously used 52/78% with outward labels + leader lines,
      // which overflowed when any class had a long translated name
      // (e.g. "TRANSIENT_VALUE" or "过渡价值 19").
      radius: ["45%", "68%"],
      center: ["50%", "42%"],
      avoidLabelOverlap: true,
      // Labels live INSIDE the donut ring now — just the count, the
      // class name is already in the legend + tooltip. Keeps the
      // visual weight of the chart contained within the card.
      label: {
        show: true, position: "inside",
        color: THEME.text, fontSize: 11, fontWeight: 600,
        formatter: (p) => (p.value > 0 ? String(p.value) : ""),
      },
      labelLine: { show: false },
      emphasis: { scale: true, scaleSize: 6 },
      // Cursor + tooltip hint it's interactive.
      cursor: "pointer",
      data,
    }],
  });

  // Click to filter the leaderboard below by that ROI class. Click the
  // same segment again to clear. The filter lives in module state so
  // tab switching preserves it (the boss can flip to People/Projects
  // and come back without losing their filter).
  if (chart) {
    chart.off("click");
    chart.on("click", (p) => {
      if (p.seriesType !== "pie") return;
      const clicked = (p.data && p.data._class) || null;
      if (!clicked) return;
      _teamRoiFilter = (_teamRoiFilter === clicked) ? null : clicked;
      renderTeamLeaderboard();
    });
  }
}

function renderTeamWaste(team) {
  const box = document.getElementById("team-waste-list");
  const rows = team.top_waste_patterns || [];
  if (!rows.length) {
    box.innerHTML = `<div class="empty">${T("team.no_patterns_yet")}</div>`;
    return;
  }
  box.innerHTML = rows.map((p) => `
    <div class="row">
      <div class="count">×${p.count}</div>
      <div class="text">${escapeHtml(p.pattern)}</div>
    </div>`).join("");
}

async function renderTeamLeaderboard() {
  // Session-level leaderboard. Previously this fetched /api/top-spenders
  // (PROMPT-level) which made the counts mismatch the donut — the donut
  // counts 2 WASTED sessions but a session with 3 WASTED prompts would
  // contribute 3 prompt rows to the list. Switched to /api/sessions so
  // "N sessions in class X" in the donut == "N rows" in the list.
  //
  //   - Default ("why are we burning money?"): show LOW_VALUE + WASTED
  //     sessions, falling back to TRANSIENT_VALUE if those don't fill
  //     the strip. Ranked by USD cost (boss-view default).
  //   - Filtered (user clicked a donut segment): show only sessions in
  //     that class, up to 20 rows, with a "clear filter" pill on top.
  const rows = await json("/api/sessions");
  const box = document.getElementById("team-leaderboard");
  let waste;
  let filterPill = "";
  if (_teamRoiFilter) {
    waste = rows.filter((r) => r.roi_class === _teamRoiFilter).slice(0, 20);
    filterPill = `<div class="filter-pill" data-clear="1">
      ${T("team.leaderboard_filtered_by")}:
      <b class="cls-${_teamRoiFilter}">${T("roi." + _teamRoiFilter)}</b>
      <span class="muted tiny"> · ${waste.length}</span>
      <span class="clear-x" title="${T("team.leaderboard_clear_filter")}">×</span>
    </div>`;
  } else {
    waste = rows.filter((r) => r.roi_class === "LOW_VALUE" || r.roi_class === "WASTED").slice(0, 10);
    if (waste.length < 5) {
      rows.forEach((r) => {
        if (r.roi_class === "TRANSIENT_VALUE" && waste.length < 10) waste.push(r);
      });
    }
  }
  if (!waste.length) {
    box.innerHTML = `${filterPill}<div class="empty muted">${_teamRoiFilter
      ? T("team.leaderboard_no_sessions_in_class")
      : T("team.no_patterns_yet")}</div>`;
    _wireLeaderboardFilterPill(box);
    return;
  }
  // Build fast session_id -> employee name map from /api/employees.
  const employees = await json("/api/employees").catch(() => []);
  const sidToEmpName = new Map();
  const empDetails = await Promise.all(
    employees.map((e) => json("/api/employees/" + encodeURIComponent(e.id)).catch(() => null))
  );
  for (const det of empDetails) {
    if (!det || !det.sessions) continue;
    for (const s of det.sessions) sidToEmpName.set(s.session_id, det.name);
  }

  box.innerHTML = filterPill + waste.map((r) => {
    const empName = sidToEmpName.get(r.session_id) || "—";
    const cls = r.roi_class ? T("roi." + r.roi_class) : "—";
    // USD leads (boss-view headline). Token count drops to a muted
    // secondary. Session summary (LLM-generated one-liner) fills the
    // explanatory slot that used to carry the prompt text.
    const cost = r.formatted_cost || ("$" + fmtCompact(r.total_tokens || 0));
    const label = r.name || r.session_id.slice(0, 12);
    const sub = (r.summary || "").slice(0, 80);
    return `<div class="row" data-session-id="${escapeHtml(r.session_id)}">
      <div class="emp">${escapeHtml(empName)}</div>
      <div class="path"><b>${escapeHtml(label)}</b>
        ${sub ? `<span class="muted"> — ${escapeHtml(sub)}</span>` : ""}
      </div>
      <div><b>${escapeHtml(cost)}</b>
        <span class="muted tiny"> · ${fmtCompact(r.total_tokens || 0)}</span>
      </div>
      <div class="cls cls-${r.roi_class}">${escapeHtml(cls)}</div>
    </div>`;
  }).join("");
  box.querySelectorAll(".row").forEach((el) => {
    el.addEventListener("click", () =>
      openSessionDetail(el.dataset.sessionId));
  });
  _wireLeaderboardFilterPill(box);
}

// Clicking the filter pill (or its "×") clears the ROI filter and
// re-renders the leaderboard. Kept separate from renderTeamLeaderboard
// so both the "has results" and "no results" paths can reuse it.
function _wireLeaderboardFilterPill(box) {
  const pill = box.querySelector(".filter-pill[data-clear]");
  if (!pill) return;
  pill.addEventListener("click", () => {
    _teamRoiFilter = null;
    renderTeamLeaderboard();
  });
}

// ---------------------------------------------------------------------------
// Employees view
// ---------------------------------------------------------------------------

async function loadEmployeesView() {
  const grid = document.getElementById("employees-grid");
  try {
    const rows = await json("/api/employees");
    if (!rows.length) {
      grid.innerHTML = `<div class="loading">${T("employees.empty")}</div>`;
      return;
    }
    grid.innerHTML = rows.map(renderEmployeeCard).join("");
    // Wire card clicks to open an employee's drill-down. We reuse the
    // existing session modal for individual sessions — deep chain
    // openEmployeeDetail → openSessionDetail.
    grid.querySelectorAll(".emp-card").forEach((el) => {
      el.addEventListener("click", () => openEmployeeDetail(el.dataset.id));
    });
  } catch (e) {
    console.error("employees view failed", e);
    grid.innerHTML = `<div class="loading">${T("modal.load_failed")}</div>`;
  }
}

function classifyRating(e) {
  // Match the server-side HIGH_VALUE gate for consistency, then fall back
  // to needs-attention if LOW+WASTED dominate.
  const roi = e.roi_counts || {};
  const total = Object.values(roi).reduce((a, b) => a + b, 0);
  const hiRatio = total ? (roi.HIGH_VALUE || 0) / total : 0;
  const badRatio = total ? ((roi.LOW_VALUE || 0) + (roi.WASTED || 0)) / total : 0;
  const eff = e.avg_efficiency;
  if (eff == null && total === 0) return "unjudged";
  if (eff != null && eff >= 0.6 && hiRatio > 0.3) return "top";
  if ((eff != null && eff < 0.4) || badRatio > 0.3) return "warn";
  return "normal";
}

function ratingLabel(r) {
  return (
    r === "top"    ? T("rating.top") :
    r === "warn"   ? T("rating.needs_attention") :
    r === "unjudged" ? T("rating.unjudged") :
    T("rating.normal")
  );
}

function renderRoiBar(roi) {
  const total = (roi.HIGH_VALUE || 0) + (roi.TRANSIENT_VALUE || 0)
              + (roi.LOW_VALUE || 0) + (roi.WASTED || 0);
  if (!total) {
    return `<div class="roi-bar"></div>
            <div class="roi-bar-legend muted">${T("employee.no_sessions")}</div>`;
  }
  const pct = (n) => (n / total) * 100;
  return `<div class="roi-bar">
      ${roi.HIGH_VALUE       ? `<div class="seg high"      style="width:${pct(roi.HIGH_VALUE)}%"></div>`       : ""}
      ${roi.TRANSIENT_VALUE  ? `<div class="seg transient" style="width:${pct(roi.TRANSIENT_VALUE)}%"></div>`  : ""}
      ${roi.LOW_VALUE        ? `<div class="seg low"       style="width:${pct(roi.LOW_VALUE)}%"></div>`        : ""}
      ${roi.WASTED           ? `<div class="seg wasted"    style="width:${pct(roi.WASTED)}%"></div>`           : ""}
    </div>
    <div class="roi-bar-legend">
      <span><span class="dot high"></span>${T("roi.HIGH_VALUE")} ${roi.HIGH_VALUE || 0}</span>
      <span><span class="dot transient"></span>${T("roi.TRANSIENT_VALUE")} ${roi.TRANSIENT_VALUE || 0}</span>
      <span><span class="dot low"></span>${T("roi.LOW_VALUE")} ${roi.LOW_VALUE || 0}</span>
      <span><span class="dot wasted"></span>${T("roi.WASTED")} ${roi.WASTED || 0}</span>
    </div>`;
}

function renderEmployeeCard(e) {
  const rating = classifyRating(e);
  const ratingLabelText = ratingLabel(rating);
  const eff = e.avg_efficiency == null ? "—" : Number(e.avg_efficiency).toFixed(2);
  const llmAgg = e.avg_llm == null ? null : Number(e.avg_llm).toFixed(2);
  const roleLine = [e.role, e.team].filter(Boolean).join(" · ") || "";
  const wasteItems = (e.top_waste || []).slice(0, 3);
  const wasteHtml = wasteItems.length
    ? wasteItems.map((w) =>
        `<div class="item"><span class="count">×${w.count}</span>${escapeHtml(w.pattern)}</div>`
      ).join("")
    : `<div class="none">${T("team.no_patterns_yet")}</div>`;
  return `<div class="emp-card rating-${rating}" data-id="${escapeHtml(e.id)}">
    <div class="emp-head">
      <div>
        <div class="emp-name">${escapeHtml(e.name)}</div>
        <div class="emp-sub">${escapeHtml(roleLine)}</div>
      </div>
      <div class="emp-rating rating-${rating}">${escapeHtml(ratingLabelText)}</div>
    </div>

    <div class="emp-metrics">
      <div class="emp-metric">
        <div class="label">${T("employee.session_count")}</div>
        <div class="value">${fmt(e.session_count)}</div>
      </div>
      <div class="emp-metric">
        <div class="label">${T("employee.total_cost")}</div>
        <div class="value">${fmtCompact(e.total_cost)}</div>
      </div>
      <div class="emp-metric">
        <div class="label">${T("employee.avg_efficiency")}</div>
        <div class="value">${eff}</div>
      </div>
    </div>

    <div>
      <div class="emp-waste-head">${T("employee.roi_mix")}</div>
      ${renderRoiBar(e.roi_counts || {})}
    </div>

    <div>
      <div class="emp-waste-head">${T("employee.main_waste")}</div>
      <div class="emp-waste-list">${wasteHtml}</div>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Projects view — one card per Claude Code workspace, ranked by USD spend.
// "Project" = every session sharing the same project_slug (path-encoded
// cwd). A local-LLM pass produces a plain-language name + description via
// `token-roi name-projects`, so "-Users-alice-…-algo" shows up as e.g.
// "Algorithms learning site" instead of a path echo.
// ---------------------------------------------------------------------------

async function loadProjectsView() {
  const grid = document.getElementById("projects-grid");
  try {
    const rows = await json("/api/projects");
    if (!rows.length) {
      grid.innerHTML = `<div class="loading">${T("projects.empty")}</div>`;
      return;
    }
    grid.innerHTML = rows.map(renderProjectCard).join("");
    grid.querySelectorAll(".proj-card").forEach((el) => {
      el.addEventListener("click", () => openProjectDetail(el.dataset.slug));
    });
  } catch (e) {
    console.error("projects view failed", e);
    grid.innerHTML = `<div class="loading">${T("modal.load_failed")}</div>`;
  }
}

// Short display badge for an Anthropic model id. Keeps the actual
// generation (4.6 vs 4.7) visible because the prices differ — Opus 4.5+
// is $5/MTok, Opus 4.0/4.1 is $15/MTok (3× the price for similar work).
// The full id is preserved as a tooltip via title="" on the chip.
//
// Parser accepts both modern ("claude-opus-4-7-20260401") and legacy
// ("claude-3-5-sonnet-20241022") id formats.
function modelFamily(id) {
  if (!id) return "unknown";
  const s = String(id).toLowerCase();
  // Legacy format first: "claude-3-5-sonnet-{date}" or "claude-3.5-sonnet".
  // Must precede the modern match, otherwise the modern regex matches
  // "sonnet-20241022" inside the legacy id and reports "Sonnet 20241022".
  let m = s.match(/\b(\d+)[-.](\d+)-(opus|sonnet|haiku)\b/);
  if (m) {
    const family = m[3][0].toUpperCase() + m[3].slice(1);
    return `${family} ${m[1]}.${m[2]}`;
  }
  // Modern format: "claude-{family}-{major}-{minor}-{date?}". Version
  // groups are clamped to 1-2 digits so a trailing 8-digit date stamp
  // can't be mis-parsed as a version number.
  m = s.match(/\b(opus|sonnet|haiku)-(\d{1,2})(?:-(\d{1,2}))?\b/);
  if (m) {
    const family = m[1][0].toUpperCase() + m[1].slice(1);
    return m[3] ? `${family} ${m[2]}.${m[3]}` : `${family} ${m[2]}`;
  }
  // Fall back to Capitalized family name.
  for (const f of ["opus", "sonnet", "haiku"]) {
    if (s.includes(f)) return f[0].toUpperCase() + f.slice(1);
  }
  return id;
}

// One-liner "Opus $200 · Sonnet $30" pill row. The boss can see at a
// glance whether expensive models did the expensive work or not.
function renderModelMix(models) {
  if (!models || !models.length) return "";
  const total = models.reduce((a, m) => a + (m.cost_usd || 0), 0);
  return `<div class="model-mix">
    ${models.map((m) => {
      const pct = total > 0 ? (m.cost_usd / total) * 100 : 0;
      return `<span class="model-chip" title="${escapeHtml(m.model)}">
        <b>${escapeHtml(modelFamily(m.model))}</b>
        <span class="muted"> ${escapeHtml(m.formatted_cost || "—")}</span>
        <span class="muted tiny"> · ${pct.toFixed(0)}%</span>
      </span>`;
    }).join("")}
  </div>`;
}

function renderProjectCard(p) {
  const roi = p.roi_counts || {};
  const nameUnlabeled = (!p.display_name) || p.display_name === p.slug;
  return `<div class="proj-card" data-slug="${escapeHtml(p.slug)}">
    <div class="proj-head">
      <div>
        <div class="proj-name">${escapeHtml(p.display_name || p.slug)}</div>
        <div class="proj-sub">
          ${escapeHtml((p.description || "").slice(0, 160))}
          ${nameUnlabeled ? `<div class="muted tiny">${T("projects.unnamed_hint")}</div>` : ""}
        </div>
      </div>
      <div class="proj-cost">${escapeHtml(p.formatted_cost || "—")}</div>
    </div>
    <div class="emp-metrics">
      <div class="emp-metric">
        <div class="label">${T("projects.sessions")}</div>
        <div class="value">${fmt(p.session_count)}</div>
      </div>
      <div class="emp-metric">
        <div class="label">${T("projects.durable_bytes")}</div>
        <div class="value">${fmtCompact(p.file_bytes || 0)}</div>
      </div>
      <div class="emp-metric">
        <div class="label">${T("projects.cost_per_kb")}</div>
        <div class="value">${escapeHtml(p.formatted_cost_per_kb || "—")}</div>
      </div>
    </div>
    <div>
      <div class="emp-waste-head">${T("projects.platform_mix")}</div>
      ${renderPlatformMix(p.platform_breakdown)}
    </div>
    <div>
      <div class="emp-waste-head">${T("projects.model_mix")}</div>
      ${renderModelMix(p.model_breakdown)}
    </div>
    <div>
      <div class="emp-waste-head">${T("projects.roi_mix")}</div>
      ${renderRoiBar(roi)}
    </div>
  </div>`;
}

async function openProjectDetail(slug) {
  try {
    const det = await json("/api/projects/" + encodeURIComponent(slug));
    modal.title().textContent = det.display_name || slug;
    modal.subtitle().textContent =
      `${det.session_count} ${T("unit.sessions")} · ${det.formatted_cost || "—"}`;
    modal.clsBadge().textContent = "";
    modal.clsBadge().className = "modal-class";
    modal.open();
    const sessionRows = (det.sessions || []).map((s) => {
      const cls = s.roi_class ? T("roi." + s.roi_class) : T("modal.unscored");
      const platBadge = s.platform
        ? `<span class="platform-badge" title="${escapeHtml(s.platform)}">${escapeHtml(platformLabel(s.platform))}</span>`
        : "";
      return `<div class="row emp-session-row"
                   data-session-id="${escapeHtml(s.session_id)}">
        <div class="path"><b>${escapeHtml(s.name || s.session_id.slice(0, 12))}</b>
          ${platBadge}
          <span class="muted"> · ${escapeHtml((s.summary || "").slice(0, 140))}</span></div>
        <div><b>${escapeHtml(s.formatted_cost || "—")}</b>
          <span class="muted tiny"> · ${fmtCompact(s.total_tokens || 0)}</span>
        </div>
        <div class="cls cls-${s.roi_class || "UNSCORED"}">${escapeHtml(cls)}</div>
      </div>`;
    }).join("");
    modal.body().innerHTML = `
      <div>
        <div class="section-h">${T("projects.description")}</div>
        <div class="muted">${escapeHtml(det.description || "—")}</div>
      </div>
      <div>
        <div class="section-h">${T("modal.section.totals")}</div>
        <div class="metrics">
          <div class="metric"><div class="label">${T("team.total_spend")}</div><div class="value">${escapeHtml(det.formatted_cost || "—")}</div></div>
          <div class="metric"><div class="label">${T("projects.cost_per_kb")}</div><div class="value">${escapeHtml(det.formatted_cost_per_kb || "—")}</div></div>
          <div class="metric"><div class="label">${T("projects.sessions")}</div><div class="value">${fmt(det.session_count)}</div></div>
          <div class="metric"><div class="label">${T("projects.durable_bytes")}</div><div class="value">${fmtCompact(det.file_bytes || 0)}</div></div>
        </div>
      </div>
      <div>
        <div class="section-h">${T("projects.platform_mix")}</div>
        ${renderPlatformMix(det.platform_breakdown)}
      </div>
      <div>
        <div class="section-h">${T("projects.model_mix")}</div>
        ${renderModelMix(det.model_breakdown)}
      </div>
      <div>
        <div class="section-h">${T("projects.roi_mix")}</div>
        ${renderRoiBar(det.roi_counts || {})}
      </div>
      <div>
        <div class="section-h">${T("unit.sessions")} (${det.sessions.length})</div>
        <div class="strip">${sessionRows || `<div class="row muted">${T("employee.no_sessions")}</div>`}</div>
      </div>
    `;
    modal.body().querySelectorAll(".emp-session-row").forEach((el) => {
      el.addEventListener("click", () => openSessionDetail(el.dataset.sessionId));
    });
  } catch (e) {
    console.error("project detail failed", e);
    modal.body().innerHTML = `<div class="why cls-WASTED">
      <b>${T("modal.load_failed")}</b>
      <div style="margin-top:6px">${escapeHtml(e.message)}</div>
    </div>`;
  }
}

// ---------------------------------------------------------------------------
// Employee drill-down modal
// ---------------------------------------------------------------------------

async function openEmployeeDetail(employeeId) {
  try {
    const det = await json("/api/employees/" + encodeURIComponent(employeeId));
    const rating = classifyRating(det);
    modal.title().textContent = det.name;
    const subBits = [];
    if (det.role) subBits.push(det.role);
    if (det.team) subBits.push(det.team);
    subBits.push(`${det.session_count} ${T("unit.sessions")}`);
    modal.subtitle().textContent = subBits.join(" · ");
    modal.clsBadge().textContent = ratingLabel(rating);
    modal.clsBadge().className = "modal-class";
    modal.open();

    const eff = det.avg_efficiency == null ? "—" : Number(det.avg_efficiency).toFixed(2);
    const llmAgg = det.avg_llm == null ? "—" : Number(det.avg_llm).toFixed(2);
    const wasteChips = (det.top_waste || []).slice(0, 8)
      .map((w) => `<div class="item"><span class="count">×${w.count}</span>${escapeHtml(w.pattern)}</div>`)
      .join("");
    const sessionRows = (det.sessions || []).map((s) => {
      const cls = s.roi_class ? T("roi." + s.roi_class) : T("modal.unscored");
      return `<div class="row emp-session-row"
                   data-session-id="${escapeHtml(s.session_id)}">
        <div class="path"><b>${escapeHtml(s.name || s.session_id.slice(0, 12))}</b>
          <span class="muted"> · ${escapeHtml((s.summary || "").slice(0, 140))}</span></div>
        <div class="muted">${fmtCompact(s.cost)}</div>
        <div class="cls cls-${s.roi_class || "UNSCORED"}">${escapeHtml(cls)}</div>
      </div>`;
    }).join("");

    modal.body().innerHTML = `
      <div>
        <div class="section-h">${T("employee.roi_mix")}</div>
        ${renderRoiBar(det.roi_counts || {})}
      </div>
      <div>
        <div class="section-h">${T("modal.section.totals")}</div>
        <div class="metrics">
          <div class="metric"><div class="label">${T("employee.session_count")}</div><div class="value">${fmt(det.session_count)}</div></div>
          <div class="metric"><div class="label">${T("employee.total_cost")}</div><div class="value">${fmtCompact(det.total_cost)}</div></div>
          <div class="metric"><div class="label">${T("employee.avg_efficiency")}</div><div class="value">${eff}</div></div>
          <div class="metric"><div class="label">${T("employee.llm_metric")}</div><div class="value">${llmAgg}</div></div>
          <div class="metric"><div class="label">${T("modal.prompt.file_writes")}</div><div class="value">${fmtCompact(det.file_write_bytes)}</div></div>
          <div class="metric"><div class="label">${T("modal.prompt.tool_calls")}</div><div class="value">${fmt(det.tool_calls)}</div></div>
        </div>
      </div>
      <div>
        <div class="section-h">${T("employee.main_waste")}</div>
        <div class="emp-waste-list">${wasteChips || `<div class="none">${T("team.no_patterns_yet")}</div>`}</div>
      </div>
      <div>
        <div class="section-h">${T("unit.sessions")} (${det.sessions.length})</div>
        <div class="strip">${sessionRows || `<div class="row muted">${T("employee.no_sessions")}</div>`}</div>
      </div>
    `;

    // Chain clicks: clicking a session row in the employee drill-down
    // swaps the modal content to the full session detail view (same
    // modal element, different content). This keeps navigation inside
    // the same overlay and matches the boss's mental model: team →
    // employee → session.
    modal.body().querySelectorAll(".emp-session-row").forEach((el) => {
      el.addEventListener("click", () => openSessionDetail(el.dataset.sessionId));
    });
  } catch (e) {
    console.error("employee detail failed", e);
    modal.body().innerHTML = `<div class="why cls-WASTED">
      <b>${T("modal.load_failed")}</b>
      <div style="margin-top:6px">${escapeHtml(e.message)}</div>
    </div>`;
  }
}
