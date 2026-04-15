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
    await renderTeamLeaderboard();
  } catch (e) {
    console.error("team view load failed", e);
  }
}

function renderTeamKpis(team) {
  const $ = (sel) => document.querySelector(sel);
  $("#team-kpi-employees").textContent = fmt(team.active_employees);
  $("#team-kpi-cost").textContent      = fmtCompact(team.total_cost || 0);
  $("#team-kpi-eff").textContent       =
    team.avg_efficiency == null ? "—" : Number(team.avg_efficiency).toFixed(2);
  $("#team-kpi-high").textContent      = fmt(team.high_value_sessions || 0);
  $("#team-kpi-waste").textContent     = fmt(team.waste_alerts || 0);
}

function renderTeamRoi(team) {
  const keys = ["HIGH_VALUE", "TRANSIENT_VALUE", "LOW_VALUE", "WASTED"];
  const roi = team.roi_totals || {};
  const data = keys.map((k) => ({
    name: T("roi." + k),
    value: roi[k] || 0,
    itemStyle: { color: ROI_COLORS[k] },
  }));
  const total = data.reduce((a, b) => a + b.value, 0);
  mount("chart-team-roi", {
    tooltip: {
      backgroundColor: THEME.panel, borderColor: THEME.line,
      textStyle: { color: THEME.text, fontFamily: "ui-monospace, Menlo, monospace", fontSize: 11 },
      padding: [8, 10], trigger: "item",
      formatter: (p) =>
        `${p.name}<br/>${fmt(p.value)} (${((p.value / (total || 1)) * 100).toFixed(1)}%)`,
    },
    legend: { bottom: 4, textStyle: { color: THEME.muted, fontSize: 10 } },
    series: [{
      type: "pie",
      radius: ["52%", "78%"],
      center: ["50%", "44%"],
      avoidLabelOverlap: false,
      label: { show: true, color: THEME.text, formatter: "{b}\n{c}" },
      labelLine: { lineStyle: { color: THEME.muted } },
      data,
    }],
  });
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
  // The top-spenders endpoint already returns prompts ordered by cost and
  // tagged with their ROI class. Filter for the wasteful classes so this
  // strip reads as "why are we burning tokens?" not "where are the tokens?"
  const rows = await json("/api/top-spenders");
  const waste = rows.filter((r) => r.class === "LOW_VALUE" || r.class === "WASTED").slice(0, 10);
  // Also include the TRANSIENT_VALUE top spenders when we don't have
  // enough real-waste rows — bosses still benefit from knowing which
  // sessions have the widest cost-to-value gap, even if classed TRANSIENT.
  if (waste.length < 5) {
    rows.forEach((r) => { if (r.class === "TRANSIENT_VALUE" && waste.length < 10) waste.push(r); });
  }
  const box = document.getElementById("team-leaderboard");
  if (!waste.length) {
    box.innerHTML = `<div class="empty muted">${T("team.no_patterns_yet")}</div>`;
    return;
  }
  // Build fast session_id -> employee name map from /api/sessions so each
  // row labels who owns this prompt.
  const sessions = await json("/api/sessions").catch(() => []);
  const employees = await json("/api/employees").catch(() => []);
  const sidToEmpName = new Map();
  // sessions endpoint doesn't yet carry employee_id — look up via the
  // employees endpoint's session list instead.
  const empDetails = await Promise.all(
    employees.map((e) => json("/api/employees/" + encodeURIComponent(e.id)).catch(() => null))
  );
  for (const det of empDetails) {
    if (!det || !det.sessions) continue;
    for (const s of det.sessions) sidToEmpName.set(s.session_id, det.name);
  }

  box.innerHTML = waste.map((r) => {
    const empName = sidToEmpName.get(r.session_id) || "—";
    const cls = r.class ? T("roi." + r.class) : "—";
    return `<div class="row" data-session-id="${escapeHtml(r.session_id)}"
                             data-prompt-id="${escapeHtml(r.prompt_id)}">
      <div class="emp">${escapeHtml(empName)}</div>
      <div class="path">${escapeHtml(r.session_name || r.session_id.slice(0, 12))}
        <span class="muted">— ${escapeHtml((r.text || "").slice(0, 60))}</span>
      </div>
      <div class="muted">${fmtCompact(r.cost_tokens)}</div>
      <div class="cls cls-${r.class}">${escapeHtml(cls)}</div>
    </div>`;
  }).join("");
  box.querySelectorAll(".row").forEach((el) => {
    el.addEventListener("click", () =>
      openSessionDetail(el.dataset.sessionId, el.dataset.promptId));
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
          <div class="metric"><div class="label">LLM</div><div class="value">${llmAgg}</div></div>
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
