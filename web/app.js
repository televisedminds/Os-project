/* Opportunity OS dashboard — vanilla JS, no build step. */

"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  plan: "pro",
  selectedId: null,
  filters: { q: "", status: "active", category: "", min_score: 0 },
  opsTab: "activity",
};

/* Human labels for the genome factors. */
const FACTOR_LABELS = {
  profit_margin: "Profit potential",
  demand_trend: "Demand momentum",
  competition: "Competitive gap",
  difficulty: "Ease of execution",
  capital_required: "Capital efficiency",
  time_required: "Time to first sale",
  risk: "Risk resilience",
  automation: "Automation",
  market_size: "Market size",
  repeatability: "Repeatability",
};

/* Fixed identity colors: opportunity TYPE → categorical slot (never re-derived). */
const TYPE_COLOR = {
  product_arbitrage: "var(--accent)",
  digital_product: "var(--aqua)",
  local_service: "var(--violet)",
  b2b_service: "var(--orange)",
  info_product: "var(--magenta)",
};
const TYPE_LABEL = {
  product_arbitrage: "flip",
  digital_product: "digital build",
  local_service: "local service",
  b2b_service: "B2B",
  info_product: "info product",
};

const fmtUSD = (v, dp) => {
  if (v == null) return "—";
  const d = dp ?? (Math.abs(v) >= 1000 ? 0 : 2);
  return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US",
    { minimumFractionDigits: d, maximumFractionDigits: d });
};
const fmtTHB = (v) => v == null ? "—" : "฿" + Math.round(v).toLocaleString("en-US");
const pct = (v, dp = 0) => v == null ? "—" : (v * 100).toFixed(dp) + "%";

/* Admin token: attached to every call (harmless on public reads, required on
   settings + cycle). Stored locally, never in the page source. On a 401/403 we
   prompt once and retry — so the dashboard stays usable once a token is set on
   the server, without shipping the secret to the browser. */
function adminToken() { return localStorage.getItem("oos_admin_token") || ""; }

async function api(path, opts = {}) {
  const withTok = (tok) => Object.assign({}, opts, {
    headers: Object.assign({}, opts.headers || {}, tok ? { "X-OOS-Token": tok } : {}),
  });
  let r = await fetch(path, withTok(adminToken()));
  if (r.status === 401 || r.status === 403) {
    const entered = (window.prompt(
      "Admin token required to view/manage keys or run cycles.\n" +
      "Set OOS_DASHBOARD_TOKEN on the server (see SECURITY.md), then paste it here:") || "").trim();
    if (entered) {
      localStorage.setItem("oos_admin_token", entered);
      r = await fetch(path, withTok(entered));
      if (r.status === 401 || r.status === 403) localStorage.removeItem("oos_admin_token");
    }
  }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

let toastTimer = null;
function toast(msg, ms = 5000) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

/* ------------------------------------------------------------- briefing */

async function loadBriefing() {
  const [b, s, h, g, fu] = await Promise.all([
    api(`/api/briefing?plan=${state.plan}`), api("/api/stats"), api("/api/health"),
    api("/api/goal"), api("/api/funnel")]);
  const ver = $("#brand-version");
  if (ver && h.version) ver.textContent = "v" + h.version;
  const badge = document.querySelector(".badge-demo");
  if (badge && h.mode === "live") {
    badge.textContent = "LIVE FEED";
    badge.classList.add("badge-live");
    const degraded = (h.adapters || []).filter((a) => !a.ok);
    badge.title = degraded.length
      ? "Live connectors with issues: " + degraded.map((a) => `${a.id} (${a.note})`).join("; ")
      : "All live connectors healthy.";
  }
  $("#tick-label").textContent = h.mode === "live"
    ? `pass ${s.tick} · observing every cycle`
    : `tick ${s.tick} · 1 cycle ≈ 1 market day`;
  $("#hero-money").textContent = fmtUSD(s.profit_pool_usd, 0);
  $("#briefing-headline").textContent = b.headline;
  // Key-gap notes (🔑) are blockers, not commentary — render them loud.
  const gaps = b.notes.filter((n) => n.startsWith("🔑"));
  const rest = b.notes.filter((n) => !n.startsWith("🔑"));
  $("#briefing-notes").innerHTML = esc(rest.join("  ")) + gaps.map((n) => `
    <div class="key-gap-note">${esc(n)}
      <button class="btn key-gap-cta" type="button">Open ⚙ Keys</button></div>`).join("");
  document.querySelectorAll(".key-gap-cta").forEach((btn) =>
    btn.addEventListener("click", () => {
      document.querySelector('.ops-tab[data-tab="keys"]')?.click();
      document.querySelector(".ops")?.scrollIntoView({ behavior: "smooth" });
    }));

  const mc = $("#mission-chip");
  if (g.mission) {
    mc.innerHTML = `<button class="mission-cta" data-id="${esc(g.mission.id)}">🎯 Today's mission:
      <b>${esc(g.mission.title)}</b> — expected <b class="pos">+${fmtUSD(g.mission.expected_usd)}</b>
      on ${fmtUSD(g.mission.capital_usd)} (${g.mission.roi_pct}% ROI) →</button>`;
    mc.querySelector(".mission-cta").addEventListener("click", () =>
      selectOpportunity(g.mission.id).catch(() => {}));
  } else {
    mc.innerHTML = `<div class="mission-cta mission-hold">🛡 ${esc(g.recommendation)}</div>`;
  }

  const gp = $("#goal-panel");
  if (g.enabled && g.goal_usd) {
    gp.hidden = false;
    const pctDone = Math.min(100, g.progress_pct || 0);
    gp.innerHTML = `
      <div class="tile-label">YOUR GOAL</div>
      <div class="goal-nums"><b>${fmtUSD(g.wallet_usd, 0)}</b><span class="of">/ ${fmtUSD(g.goal_usd, 0)}</span></div>
      <div class="meter"><div class="meter-track"><div class="meter-fill" style="width:${pctDone}%"></div></div>
        <span class="meter-num">${pctDone.toFixed(0)}%</span></div>
      <div class="tile-sub">cash ${fmtUSD(g.cash_usd, 0)} · deployed ${fmtUSD(g.deployed_usd, 0)} ·
        realized ${fmtUSD(g.realized_usd, 0)}</div>
      ${g.eta_months ? `<div class="tile-sub">≈ ${g.eta_months} months at the current verified pace</div>` : ""}`;
  } else {
    gp.hidden = false;
    gp.innerHTML = `<div class="tile-label">YOUR GOAL</div>
      <div class="tile-sub" style="margin-top:6px">Set <code>"capital_usd"</code> and <code>"goal_usd"</code>
      in the operator section of watchlist.json and the whole product starts working toward your number.</div>`;
  }

  const watching = s.watching && s.watching.total
    ? `<span class="fn-step"><b>${s.watching.total.toLocaleString()}</b> markets watched${
        s.watching.discovered ? ` <span class="fn-sub">(${s.watching.discovered} auto-found)</span>` : ""}</span><span class="fn-a">→</span>`
    : "";
  $("#funnel-strip").innerHTML = `
    <span class="fn-label">RESEARCH FUNNEL · last 24h</span>
    ${watching}
    <span class="fn-step"><b>${fu.observations.toLocaleString()}</b> observations</span><span class="fn-a">→</span>
    <span class="fn-step"><b>${fu.anomalies.toLocaleString()}</b> anomalies</span><span class="fn-a">→</span>
    <span class="fn-step"><b>${fu.investigations.toLocaleString()}</b> investigated</span><span class="fn-a">→</span>
    <span class="fn-step"><b>${fu.rejected.toLocaleString()}</b> rejected</span><span class="fn-a">→</span>
    <span class="fn-step"><b>${fu.verified.toLocaleString()}</b> newly verified</span><span class="fn-a">→</span>
    <span class="fn-step"><b>${(fu.rechecked || 0).toLocaleString()}</b> re-checks held</span><span class="fn-a">→</span>
    <span class="fn-step fn-final"><b>${fu.recommended_now}</b> live for you now</span>`;

  const conf = s.avg_confidence || 0;
  const lastInv = b.counts.invalidated_last_cycle;
  $("#stat-tiles").innerHTML = `
    ${tile("Verified active", s.opportunities_active, "published & re-verified each cycle")}
    ${tile("Avg confidence", pct(conf), "reliability-weighted consensus")}
    ${tile("Capital needed", fmtUSD(s.capital_needed_usd, 0), "to take every active deal")}
    ${tile("Best ROI", (s.best_roi_pct || 0) + "%", "highest verified return live now")}
    ${tile("Closing soon", b.counts.closing_soon ?? 0, "window ≤ 3 days — act first")}
    ${tile("Invalidated", `${lastInv}<span style="font-size:13px;color:var(--muted)"> last cycle</span>`,
           `${s.invalidated} all-time — conditions changed`)}
  `;
}
const tile = (label, value, sub) =>
  `<div class="tile"><div class="tile-label">${esc(label)}</div>
   <div class="tile-value">${value}</div><div class="tile-sub">${esc(sub)}</div></div>`;

/* ----------------------------------------------------------------- feed */

async function loadFeed() {
  const f = state.filters;
  const qs = new URLSearchParams({ plan: state.plan, min_score: f.min_score });
  if (f.status) qs.set("status", f.status);
  if (f.category) qs.set("category", f.category);
  if (f.q) qs.set("q", f.q);
  const r = await api(`/api/opportunities?${qs}`);

  const banner = $("#locked-banner");
  if (r.locked > 0) {
    banner.hidden = false;
    banner.innerHTML = `${r.locked} more verified opportunit${r.locked === 1 ? "y" : "ies"} this week are on
      the <b>Pro</b> plan — the free tier shows 3. (Switch the plan picker to see the gate work.)`;
  } else banner.hidden = true;

  const body = $("#feed-body");
  if (!r.opportunities.length) {
    body.innerHTML = `<tr><td colspan="6" style="color:var(--muted);padding:24px;text-align:center">
      No opportunities match. Run a research cycle or loosen the filters.</td></tr>`;
    return;
  }
  body.innerHTML = r.opportunities.map((o) => {
    const dot = TYPE_COLOR[o.type] || "var(--accent)";
    const statusPill = o.status !== "active"
      ? `<span class="status-pill status-${esc(o.status)}">${esc(o.status)}</span>` : "";
    return `<tr class="feed-row ${o.id === state.selectedId ? "selected" : ""}" data-id="${esc(o.id)}">
      <td class="t-left">
        <div class="op-title">${esc(o.title)} ${statusPill}</div>
        <div class="op-sub">
          <span class="chip"><span class="dot" style="background:${dot}"></span>${esc(TYPE_LABEL[o.type] || o.type)}</span>
          <span class="chip">${esc(o.category.replace(/_/g, " "))}</span>
          <span class="chip">${esc(o.route)}</span>
          ${o.personal ? `<span class="chip personal-chip ${o.personal.boost > 0 ? "" : "personal-down"}"
            title="${esc(o.personal.note)}">${o.personal.boost > 0 ? "★ for you" : "▼ downranked"}</span>` : ""}
        </div>
      </td>
      <td class="t-right"><span class="${o.net_usd >= 0 ? "pos" : "neg"}">${fmtUSD(o.net_usd)}</span>
        <div class="op-sub">${o.econ_kind === "venture" ? "/mo · " : `${o.qty}× · `}${fmtTHB(o.net_thb)}</div></td>
      <td class="t-right">${o.margin_pct.toFixed(0)}%</td>
      <td>${meter(o.score)}</td>
      <td class="t-right">${pct(o.confidence)}</td>
      <td class="t-right">${o.window_days.toFixed(0)}d</td>
    </tr>`;
  }).join("");

  body.querySelectorAll(".feed-row").forEach((tr) =>
    tr.addEventListener("click", () => selectOpportunity(tr.dataset.id)));
}

const meter = (score) => `
  <div class="meter" title="overall score ${score}/100">
    <div class="meter-track"><div class="meter-fill" style="width:${Math.max(2, score)}%"></div></div>
    <span class="meter-num">${Math.round(score)}</span>
  </div>`;

/* --------------------------------------------------------------- detail */

async function selectOpportunity(id) {
  state.selectedId = id;
  document.querySelectorAll(".feed-row").forEach((tr) =>
    tr.classList.toggle("selected", tr.dataset.id === id));
  const o = await api(`/api/opportunities/${id}?plan=${state.plan}`);
  $("#detail-empty").hidden = true;
  const el = $("#detail-content");
  el.hidden = false;
  el.innerHTML = renderDetail(o);
  wireDetail(el, o);
  el.scrollTop = 0;
}

function renderDetail(o) {
  const e = o.economics;
  const isFlip = e.kind === "flip";
  const per = isFlip ? "/unit" : "/mo";
  const dot = TYPE_COLOR[o.type] || "var(--accent)";
  const invalid = o.status === "invalidated" && o.invalidation_reason
    ? `<div class="locked-note" style="margin:10px 0;border-color:color-mix(in srgb,var(--critical) 50%,transparent);color:var(--critical)">
         Invalidated on re-verification — ${esc(o.invalidation_reason)}</div>` : "";

  return `
    <div class="d-head-row">
      <div>
        <div class="d-title">${esc(o.title)}</div>
        <div class="d-sub">${esc(o.subtitle)}</div>
        <div style="margin-top:7px">
          <span class="chip"><span class="dot" style="background:${dot}"></span>${esc(TYPE_LABEL[o.type] || o.type)}</span>
          <span class="chip">${esc(o.category.replace(/_/g, " "))}</span>
          ${verificationChip(o)}
          ${o.status !== "active" ? `<span class="status-pill status-${esc(o.status)}">${esc(o.status)}</span>` : ""}
        </div>
      </div>
      <div style="text-align:right">
        <div class="score-overall" style="margin:0"><span class="big">${Math.round(o.score.overall)}</span><span class="of">/100</span></div>
        <div class="kpi-s">confidence ${pct(o.confidence)}</div>
      </div>
    </div>
    ${invalid}
    <div class="d-kpis">
      ${kpi(`Net ${per} (base)`, fmtUSD(e.base.net_usd), `${fmtUSD(e.pessimistic.net_usd)} pessimistic`)}
      ${kpi("Total (base)", fmtUSD(e.total_net_usd), esc(fmtTHB(e.thb.total_net_thb ?? e.thb.net_per_month_thb)))}
      ${kpi("Margin", `${e.base.margin_pct.toFixed(0)}%`, `${e.pessimistic.margin_pct.toFixed(0)}% pessimistic`)}
      ${kpi("Capital needed", fmtUSD(e.capital_usd), `${esc(fmtTHB(e.thb.capital_thb))}${isFlip ? ` · ${e.qty} units` : " startup"}`)}
      ${kpi("Window", `${o.window_days.toFixed(0)} days`, `updated tick ${o.tick_updated}`)}
    </div>

    ${actionCard(o)}
    ${sellingKit(o)}
    ${discoveryReport(o)}

    <div class="d-section"><h3>Step-by-step instructions</h3>${missionBar(o)}${playbook(o)}</div>

    <div class="d-section"><h3>AI investigation timeline</h3>
      <div class="why-meta">Ran automatically on pass ${o.tick_updated}${o.updated_ts ?
        " · " + new Date(o.updated_ts * 1000).toLocaleString() : ""} — each step queried live data:</div>
      <div class="why">${o.why_chain.map((s, i) => `
        <div class="why-step"><div class="why-n">${i + 1}</div><div>
        <div class="why-q">${esc(s.question)}</div>
        <div class="why-a">${esc(s.finding)}</div></div></div>`).join("")}
      </div>
    </div>

    ${o.history && o.history.points && o.history.points.length > 2 ? `
    <div class="d-section"><h3>${esc(o.history.label)} — last ${o.history.points.length} days</h3>
      <div class="spark-wrap" id="spark-wrap"></div>
    </div>` : ""}

    <div class="d-section"><h3>Cost waterfall — every baht accounted for</h3>
      <div class="wf-toggle">
        <button class="active" data-scn="base">base</button>
        <button data-scn="pessimistic">pessimistic</button>
      </div>
      <div id="wf-body">${waterfall(e.base, per)}</div>
      <p class="wf-note" style="margin:8px 0 0">${esc(e.route_note)}${isFlip ? ` Breakeven sale: ${fmtUSD(e.breakeven_revenue_usd)}.` : ""}</p>
    </div>

    <div class="d-section"><h3>Opportunity genome — ${Math.round(o.score.overall)}/100 (weights learned from outcomes)</h3>
      ${Object.entries(o.score.factors)
        .sort((a, b) => (o.score.weights[b[0]] ?? 0) - (o.score.weights[a[0]] ?? 0))
        .map(([k, v]) => `
        <div class="fbar-row" title="contributes ${o.score.contributions[k] ?? 0} pts at weight ${((o.score.weights[k] ?? 0) * 100).toFixed(0)}%">
          <div class="fbar-label">${esc(FACTOR_LABELS[k] || k.replace(/_/g, " "))}</div>
          <div class="fbar-track"><div class="fbar-fill" style="width:${v}%"></div></div>
          <div class="fbar-num">${Math.round(v)}</div>
        </div>`).join("")}
      <div class="fbar-row" title="derived from the current decay window (~${o.window_days.toFixed(0)} days)">
        <div class="fbar-label">Market lifetime</div>
        <div class="fbar-track"><div class="fbar-fill" style="width:${Math.min(100, o.window_days / 14 * 100)}%"></div></div>
        <div class="fbar-num">${o.window_days.toFixed(0)}d</div>
      </div>
    </div>

    <div class="d-section"><h3>Verification council — ${o.verification.checks.length} independent checks</h3>
      ${o.verification.checks.map((c) => `
        <div class="check">
          <div class="check-ic ${c.passed ? "ok" : "fail"}">${c.passed ? "✓" : "✗"}</div>
          <div><div class="check-name">${esc(c.name)}${c.critical ? ' <span style="color:var(--muted);font-size:10px">CRITICAL</span>' : ""}</div>
          <div class="check-ev">${esc(c.evidence)}</div></div>
          <div class="check-conf">${pct(c.confidence)}</div>
        </div>`).join("")}
      <div class="consensus-line">Consensus <b style="color:var(--ink)">${pct(o.verification.consensus)}</b>
        × learning calibration → published at <b style="color:var(--ink)">${pct(o.confidence)}</b> confidence.</div>
    </div>

    <div class="d-section"><h3>Thailand lens 🇹🇭</h3>
      <div class="th-flags">
        <span class="th-flag ${o.feasibility.can_buy ? "yes" : "no"}">${o.feasibility.can_buy ? "✓" : "✗"} buy from TH</span>
        <span class="th-flag ${o.feasibility.can_sell ? "yes" : "no"}">${o.feasibility.can_sell ? "✓" : "✗"} sell from TH</span>
        ${o.feasibility.requires_proxy ? '<span class="th-flag">via proxy service</span>' : ""}
      </div>
      <ul class="th-notes">
        ${o.feasibility.buy_notes.concat(o.feasibility.sell_notes, o.feasibility.customs_notes)
          .filter(Boolean).map((n) => `<li>${esc(n)}</li>`).join("")}
      </ul>
      <div class="wf-note">Payments: ${esc((o.feasibility.payment_rails || []).join(" · "))}</div>
    </div>

    ${o.automation ? `
    <div class="d-section"><h3>Automation plan — ${Math.round(o.automation.coverage_pct)}% machine-runnable</h3>
      <div class="meter" style="max-width:280px;margin-bottom:10px">
        <div class="meter-track"><div class="meter-fill" style="width:${o.automation.coverage_pct}%"></div></div>
        <span class="meter-num">${Math.round(o.automation.coverage_pct)}%</span>
      </div>
      ${o.automation.tasks.map((t) => `
        <div class="check"><div class="check-ic ${t.automatable ? "ok" : ""}" style="${t.automatable ? "" : "color:var(--muted)"}">${t.automatable ? "⚙" : "👤"}</div>
        <div><div class="check-name">${esc(t.task)}</div><div class="check-ev">${esc(t.tool || "")}</div></div><div></div></div>`).join("")}
      <div class="wf-note" style="margin-top:8px">Human checkpoints: ${o.automation.human_checkpoints.map(esc).join(" · ")}</div>
    </div>` : ""}

    <div class="d-section" id="chat-section"><h3>💬 Execution chat — this deal's own assistant</h3>
      <div class="chat-meta" id="chat-meta">Loading workspace…</div>
      <div class="chat-log" id="chat-log"></div>
      <div class="chat-taps" id="chat-taps">
        ${["Which exact product do I buy?", "Send me the buying link", "Is this still profitable?",
           "How many units should I buy?", "Where do I sell it from Thailand?",
           "Generate the selling listing", "What do I do next?"].map((q) =>
          `<button class="chip chat-tap" type="button">${esc(q)}</button>`).join("")}
      </div>
      <div class="chat-inbar">
        <input id="chat-input" type="text" placeholder="Ask about this deal, paste a price/listing, or 'I bought 2 for $80'…">
        <button class="btn btn-primary" id="chat-send">Send</button>
      </div>
      <div class="wf-note" style="margin-top:6px">Evidence tags: LIVE fetched now · SAVED stored ·
        CALCULATED computed · ASSUMPTION · UNKNOWN — the chat records and researches, it never buys,
        pays or publishes anything without you.</div>
    </div>

    <div class="d-section"><h3>Close the loop</h3>
      <p class="wf-note" style="margin:0 0 8px">Acted on this? Record what actually happened — outcomes retune
      scoring weights, source reliability and confidence calibration.</p>
      <div class="outcome-form">
        <select id="oc-result"><option value="success">success</option><option value="failure">failure</option></select>
        <input id="oc-profit" type="number" step="0.01" placeholder="profit USD">
        <select id="oc-reason" hidden>
          <option value="">failure reason…</option>
          <option value="competition">competition</option><option value="shipping">shipping</option>
          <option value="demand">demand</option><option value="fees">fees</option>
          <option value="customs">customs</option><option value="price_moved">price moved</option>
        </select>
        <button class="btn" id="oc-submit">Record outcome</button>
      </div>
      <div class="wf-note" style="margin-top:8px">Sources: ${o.sources.map(esc).join(", ")} · id ${esc(o.id)}</div>
    </div>
  `;
}

const kpi = (l, v, s) => `<div class="kpi"><div class="kpi-l">${esc(l)}</div>
  <div class="kpi-v">${v}</div><div class="kpi-s">${s}</div></div>`;

/* Verification level (Phase 7): single-source work is labelled, never dressed
   up as fully verified. */
const LEVEL_LABEL = {
  execution_ready: ["EXECUTION READY", "var(--good)"],
  multi_source_verified: ["MULTI-SOURCE VERIFIED", "var(--good)"],
  partially_verified: ["SINGLE-SOURCE · PARTIAL", "var(--warning)"],
  discovered: ["DISCOVERED", "var(--muted)"],
  invalidated: ["INVALIDATED", "var(--critical)"],
};
function verificationChip(o) {
  const lv = o.verification_level || "discovered";
  const [label, color] = LEVEL_LABEL[lv] || [lv, "var(--muted)"];
  const single = o.single_source && lv !== "partially_verified" ? " · single-source" : "";
  return `<span class="chip" title="Graded from the evidence ledger: independent sources, sold comps, Thailand executability, freshness."
    style="color:${color};border-color:color-mix(in srgb,${color} 45%,transparent)">${label}${esc(single)}</span>`;
}

/* ------------------------------------------------ execution chat (Phase 13) */

function chatBubble(m) {
  const ev = (m.evidence || []).map((e) =>
    `<span class="ev-tag ev-${esc((e.label || "").toLowerCase())}" title="${esc(e.text || "")}">${esc(e.label)}</span>`).join("");
  return `<div class="chat-msg chat-${m.role === "user" ? "user" : "ai"}">
    <div class="chat-body">${esc(m.content)}</div>${ev ? `<div class="chat-evs">${ev}</div>` : ""}</div>`;
}

async function loadChat(o) {
  try {
    const c = await api(`/api/opportunities/${o.id}/chat`);
    const st = c.state || {};
    const na = (st.next_action || {});
    const pnl = ((c.context || {}).realized_pnl || {});
    $("#chat-meta").innerHTML =
      `<span class="chip">state: <b>${esc((st.state || "not_started").replace(/_/g, " "))}</b></span>
       <span class="chip">realised: <b class="${pnl.net_usd >= 0 ? "pos" : "neg"}">${fmtUSD(pnl.net_usd || 0)}</b></span>
       <span class="chip" title="${esc(na.why || "")}">next: ${esc(na.title || "—")}</span>`;
    const log = $("#chat-log");
    log.innerHTML = (c.history || []).map(chatBubble).join("") ||
      `<div class="wf-note">No messages yet — this chat already knows the whole deal. Ask it anything below.</div>`;
    log.scrollTop = log.scrollHeight;
  } catch (e) {
    $("#chat-meta").textContent = "Chat unavailable: " + e.message;
  }
}

function wireChat(el, o) {
  const input = el.querySelector("#chat-input");
  const send = async (text) => {
    if (!text.trim()) return;
    const log = $("#chat-log");
    log.insertAdjacentHTML("beforeend", chatBubble({ role: "user", content: text }));
    log.scrollTop = log.scrollHeight;
    input.value = "";
    try {
      const r = await api(`/api/opportunities/${o.id}/chat`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text }) });
      log.insertAdjacentHTML("beforeend", chatBubble({ role: "assistant", content: r.reply, evidence: r.evidence }));
      log.scrollTop = log.scrollHeight;
      loadChat(o);                       // refresh state/P&L strip
    } catch (e) {
      toast("Chat failed: " + e.message, 6000);
    }
  };
  el.querySelector("#chat-send")?.addEventListener("click", () => send(input.value));
  input?.addEventListener("keydown", (ev) => { if (ev.key === "Enter") send(input.value); });
  el.querySelectorAll(".chat-tap").forEach((b) =>
    b.addEventListener("click", () => send(b.textContent)));
  loadChat(o);
}

/* AI selling kit — the execution layer: one tap turns a verified opportunity
   into ready-to-paste listings (flips) or a launch kit (ventures). */
const KIT_FIELDS = {
  flip: [
    ["listing_title_en", "eBay title (EN)"],
    ["listing_title_th", "Shopee / TikTok Shop title (TH)"],
    ["bullets_en", "Selling points (EN)"],
    ["description_en", "Listing description (EN)"],
    ["description_th", "Listing description (TH)"],
    ["seller_message_th", "Message to send the source seller (TH)"],
    ["hashtags", "Hashtags"],
    ["pricing_strategy", "Pricing strategy"],
  ],
  venture: [
    ["product_name", "Product name"],
    ["one_liner_en", "One-liner (EN)"],
    ["one_liner_th", "One-liner (TH)"],
    ["outline", "Smallest sellable version — outline"],
    ["landing_headline_en", "Landing headline (EN)"],
    ["landing_headline_th", "Landing headline (TH)"],
    ["landing_copy_en", "Landing copy (EN)"],
    ["landing_copy_th", "Landing copy (TH)"],
    ["first_posts", "First 3 launch posts"],
    ["pricing_advice", "Pricing"],
    ["first_week_plan", "Your first week, day by day"],
  ],
};

function kitValue(key, v) {
  if (v == null) return "";
  if (key === "hashtags" && Array.isArray(v)) return v.map((h) => "#" + String(h).replace(/^#/, "")).join(" ");
  if (key === "first_posts" && Array.isArray(v))
    return v.map((p) => `[${p.platform}]\n${p.text}`).join("\n\n");
  if (Array.isArray(v)) return v.map((x) => "• " + x).join("\n");
  return String(v);
}

function sellingKit(o) {
  if (o.locked) return "";
  let inner;
  if (o.kit && o.kit.kit) {
    const k = o.kit.kit;
    const fields = KIT_FIELDS[k._kind === "venture" ? "venture" : "flip"];
    inner = fields.filter(([key]) => k[key] != null && String(k[key]).length).map(([key, label]) => `
      <div class="kit-field">
        <div class="kit-label">${esc(label)}<button class="copy-btn" type="button">copy</button></div>
        <div class="kit-text">${esc(kitValue(key, k[key]))}</div>
      </div>`).join("") + `
      <div class="wf-note" style="margin-top:8px">Written ${new Date(o.kit.generated_at * 1000).toLocaleString()}
        by ${esc(o.kit.model)} from this opportunity's verified data — read before posting; you are the final check.
        <button class="btn kit-gen" data-force="1" type="button" style="margin-left:8px">↻ Regenerate</button></div>`;
  } else if (o.kit_available) {
    inner = `<p class="wf-note" style="margin:0 0 8px">One tap writes everything you need to act:
      ${o.type === "product_arbitrage"
        ? "a ready-to-paste eBay listing (English), a Shopee/TikTok Shop listing (Thai), and the message to send the source seller."
        : "the smallest sellable version, bilingual landing copy, launch posts, and a first-week plan."}</p>
      <button class="btn btn-primary kit-gen" data-force="0" type="button">✨ Generate selling kit</button>`;
  } else {
    inner = `<p class="wf-note" style="margin:0">Add <code>ANTHROPIC_API_KEY</code> to your .env
      (console.anthropic.com — a few cents per kit) to unlock one-tap selling kits:
      ready-to-paste bilingual listings and launch plans, written from this opportunity's verified data.</p>`;
  }
  return `<div class="d-section" id="kit-section"><h3>✨ AI selling kit — from verified deal to posted listing</h3>${inner}</div>`;
}

function copyText(text, btn) {
  const done = () => {
    const old = btn.textContent;
    btn.textContent = "✓ copied";
    setTimeout(() => { btn.textContent = old; }, 1400);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);            // http:// droplet — no Clipboard API
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch { toast("Copy failed — select the text manually."); }
  document.body.removeChild(ta);
}

/* The do-this-deal card: where to buy, where to sell, at which prices,
   what to invest and what you keep. */
function actionCard(o) {
  const a = o.action;
  if (!a) return "";
  const stepsHtml = a.first_steps && a.first_steps.length
    ? `<div class="ac-steps">Start with: ${a.first_steps.map((s, i) =>
        `<span class="ac-step">${i + 1}. ${esc(s)}</span>`).join(" ")}</div>`
    : `<div class="ac-steps">🔒 Full instructions are on the Pro plan — switch the plan picker.</div>`;

  const linkBtn = (url, label) => url
    ? `<a class="ac-link" href="${esc(url)}" target="_blank" rel="noopener">${esc(label)} ↗</a>` : "";
  // Prefer the exact listing when we have one, and still offer the full search.
  const sideLinks = (side, exactLabel, searchLabel) => {
    if (!side || !side.url) return "";
    if (side.exact) {
      const seeAll = (side.search_url && side.search_url !== side.url)
        ? linkBtn(side.search_url, searchLabel) : "";
      return linkBtn(side.url, "🎯 " + exactLabel) + seeAll;
    }
    return linkBtn(side.url, searchLabel);
  };
  const moneyTimeline = (tl) => !tl ? "" : `
    <div class="mt-strip">${tl.map((ev) => `
      <span class="mt-ev"><b>Day ${ev.day}</b> ${esc(ev.label)}${ev.amount_usd == null ? "" :
        ` <span class="${ev.amount_usd >= 0 ? "pos" : "neg"}">${fmtUSD(ev.amount_usd)}</span>`}</span>`)
      .join('<span class="mt-arrow">→</span>')}</div>`;
  const forecastRow = (f) => !f ? "" : `
    <div class="ac-forecast" title="${esc(f.note)}">
      If you wait, chance the edge survives:
      ${Object.entries(f.probabilities).map(([d, p]) =>
        `<span class="fc-chip ${p < 40 ? "fc-low" : ""}">${d}d ${p}%</span>`).join(" ")}
      — <b>${esc(f.recommendation)}</b>
    </div>`;

  if (a.type === "flip") {
    return `
    <div class="action-card">
      <div class="ac-title">✅ How to execute this deal</div>
      <div class="ac-flow">
        <div class="ac-box">
          <div class="ac-l">BUY ${esc(String(a.buy.qty))}× on</div>
          <div class="ac-v">${esc(a.buy.venue)}</div>
          <div class="ac-p">${fmtTHB(a.buy.price_thb)} <span class="ac-sub">(${fmtUSD(a.buy.price_usd)})</span>/unit</div>
          <div class="ac-note">Never pay above ${fmtUSD(a.buy.max_price_usd)}. ${esc(a.buy.how)}</div>
          ${sideLinks(a.buy, "Open the exact listing", "Open live listings")}
        </div>
        <div class="ac-arrow">→</div>
        <div class="ac-box">
          <div class="ac-l">SELL on</div>
          <div class="ac-v">${esc(a.sell.venue)}${a.sell.registered ? ' <span class="pos">✓</span>' : ""}</div>
          <div class="ac-p">${fmtTHB(a.sell.price_thb)} <span class="ac-sub">(${fmtUSD(a.sell.price_usd)})</span>/unit</div>
          <div class="ac-note">${esc(a.sell.how)}</div>
          ${sideLinks(a.sell, "Open the exact listing", "See competing listings")}
          ${!a.sell.registered ? linkBtn(a.sell.signup_url, "Create seller account") : ""}
        </div>
      </div>
      <div class="ac-money">
        <span>You invest <b>${fmtTHB(a.invest_thb)}</b> (${fmtUSD(a.invest_usd)})</span>
        <span>You keep ≈ <b class="pos">${fmtTHB(a.profit_thb)}</b> (${fmtUSD(a.profit_usd)}, ${a.margin_pct.toFixed(0)}% margin)</span>
        <span>Worst case still ≈ ${fmtUSD(a.pessimistic_unit_usd)}/unit</span>
        <span>~${Math.round(a.timeline_days)} days start → paid</span>
      </div>
      ${moneyTimeline(a.money_timeline)}
      ${forecastRow(a.forecast)}
      ${stepsHtml}
    </div>`;
  }
  return `
    <div class="action-card">
      <div class="ac-title">✅ How to execute this opportunity</div>
      <div class="ac-flow"><div class="ac-box" style="flex:1">
        <div class="ac-l">BUILD / LAUNCH (${esc(a.geo)})</div>
        <div class="ac-v" style="font-size:14px">${esc(a.what)}</div>
      </div></div>
      <div class="ac-money">
        <span>Startup cost <b>${fmtTHB(a.invest_thb)}</b> (${fmtUSD(a.invest_usd)})</span>
        <span>Expected ≈ <b class="pos">${fmtTHB(a.monthly_thb)}</b>/month (${fmtUSD(a.monthly_usd)})</span>
        <span>Worst case ≈ ${fmtUSD(a.pessimistic_monthly_usd)}/mo</span>
        <span>Pays itself back in ~${a.payback_months} months</span>
      </div>
      ${moneyTimeline(a.money_timeline)}
      ${forecastRow(a.forecast)}
      ${stepsHtml}
    </div>`;
}

/* Discovery report: the deltas that made the fleet look twice. */
function discoveryReport(o) {
  const d = o.discovery;
  if (!d || !d.rows || !d.rows.length) return "";
  return `<div class="d-section"><h3>Discovery report — what the fleet saw</h3>
    <div class="disc-grid">${d.rows.map((r) => {
      const cls = !r.delta ? "" : r.delta.startsWith("+") ? "disc-up" : r.delta.startsWith("-") ? "disc-down" : "";
      return `<div class="disc-row"><span class="disc-l">${esc(r.label)}</span>
        <span class="disc-v">${esc(r.value)}</span>
        <span class="disc-d ${cls}">${esc(r.delta || "")}</span></div>`;
    }).join("")}</div></div>`;
}

/* Mission header: quest-style progress over the playbook. */
function missionBar(o) {
  if (!o.playbook || !o.playbook.steps.length) return "";
  const p = o.progress || { done_steps: [], pct: 0 };
  const diff = (o.score.factors.difficulty ?? 50);
  const diffLabel = diff >= 68 ? "EASY" : diff >= 45 ? "MEDIUM" : "HARD";
  const e = o.economics;
  return `<div class="mission-bar" id="mission-bar">
    <span class="m-id">MISSION ${esc(o.id.slice(-4).toUpperCase())}</span>
    <span class="chip">difficulty ${diffLabel}</span>
    <span class="chip">~${Math.round(o.playbook.timeline_days || o.window_days)} days</span>
    <span class="chip">capital ${fmtUSD(e.capital_usd)}</span>
    <span class="chip pos">potential +${fmtUSD(e.total_net_usd)}${e.kind === "venture" ? "/mo" : ""}</span>
    <div class="meter" style="flex:1;min-width:120px">
      <div class="meter-track"><div class="meter-fill" id="mission-fill" style="width:${p.pct}%"></div></div>
      <span class="meter-num" id="mission-pct">${p.pct}%</span>
    </div>
  </div>`;
}

function waterfall(scn, per) {
  return `<table class="wf-table">
    <tr class="wf-revenue"><td>Sale price ${esc(per)}</td><td></td>
      <td class="wf-amount">${fmtUSD(scn.revenue_usd)}</td></tr>
    ${scn.lines.map((l) => `<tr><td>${esc(l.label)}</td><td class="wf-note">${esc(l.note || "")}</td>
      <td class="wf-amount">−${fmtUSD(l.amount_usd).replace("$", "$")}</td></tr>`).join("")}
    <tr class="wf-total"><td>Net ${esc(per)} (${esc(scn.name)})</td><td></td>
      <td class="wf-amount ${scn.net_usd >= 0 ? "pos" : "neg"}">${fmtUSD(scn.net_usd)}
      <span class="wf-note">(${scn.margin_pct.toFixed(1)}%)</span></td></tr>
  </table>`;
}

function playbook(o) {
  if (!o.playbook) {
    return `<div class="locked-note">🔒 ${esc(o.locked?.playbooks || "Playbooks are a Pro feature.")}
      ${esc(o.locked?.upgrade || "")}</div>`;
  }
  const pb = o.playbook;
  const done = new Set((o.progress && o.progress.done_steps) || []);
  return `
    ${pb.steps.map((s) => `
      <div class="pb-step ${done.has(s.order) ? "pb-done" : ""}">
        <input type="checkbox" class="pb-check" data-step="${s.order}" ${done.has(s.order) ? "checked" : ""}
               title="mark this step done">
        <div class="pb-n">${s.order}</div>
        <div><div class="pb-t">${esc(s.title)}</div><div class="pb-d">${esc(s.detail)}</div>
        <div class="pb-meta">${s.eta ? esc(s.eta) + " · " : ""}${s.automatable
          ? `<span class="tag-auto">⚙ automatable${s.tool ? " — " + esc(s.tool) : ""}</span>` : "👤 human step"}</div>
        </div></div>`).join("")}
    ${pb.listing ? `
      <div class="listing-card">
        <div class="wf-note">PRE-GENERATED LISTING — ${esc(pb.listing.platform)}</div>
        <div class="l-title">${esc(pb.listing.title)}</div>
        <div class="l-line"><b style="color:var(--ink)">${fmtUSD(pb.listing.price_usd)}</b>
          <span class="wf-note"> — ${esc(pb.listing.pricing_rule)}</span></div>
        <div class="l-line">${esc(pb.listing.description)}</div>
        <div class="l-line wf-note">📷 ${esc(pb.listing.photos)}</div>
      </div>` : ""}`;
}

function wireDetail(el, o) {
  wireChat(el, o);
  el.querySelectorAll(".kit-gen").forEach((b) =>
    b.addEventListener("click", async () => {
      b.disabled = true;
      const old = b.textContent;
      b.textContent = "✍️ Writing your kit…";
      try {
        await api(`/api/opportunities/${o.id}/kit?force=${b.dataset.force}&plan=${state.plan}`,
          { method: "POST" });
        await selectOpportunity(o.id);
        toast("✨ Selling kit ready — copy, check, and post.");
      } catch (err) {
        b.disabled = false;
        b.textContent = old;
        toast("Kit failed: " + err.message, 8000);
      }
    }));
  el.querySelectorAll(".copy-btn").forEach((b) =>
    b.addEventListener("click", () => {
      const txt = b.closest(".kit-field")?.querySelector(".kit-text")?.textContent || "";
      copyText(txt, b);
    }));

  el.querySelectorAll(".pb-check").forEach((cb) =>
    cb.addEventListener("change", async () => {
      try {
        const r = await api(`/api/opportunities/${o.id}/progress`,
          { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ step: parseInt(cb.dataset.step, 10), done: cb.checked }) });
        cb.closest(".pb-step").classList.toggle("pb-done", cb.checked);
        const n = o.playbook.steps.length;
        const pctDone = Math.round(100 * r.done_steps.length / n);
        const fill = $("#mission-fill", el), num = $("#mission-pct", el);
        if (fill) fill.style.width = pctDone + "%";
        if (num) num.textContent = pctDone + "%";
        if (pctDone === 100) toast("🏁 Mission complete — record the outcome below so the AI learns!");
      } catch (err) { toast("Could not save progress: " + err.message); cb.checked = !cb.checked; }
    }));

  el.querySelectorAll(".wf-toggle button").forEach((b) =>
    b.addEventListener("click", () => {
      el.querySelectorAll(".wf-toggle button").forEach((x) => x.classList.toggle("active", x === b));
      $("#wf-body", el).innerHTML = waterfall(o.economics[b.dataset.scn],
        o.economics.kind === "flip" ? "/unit" : "/mo");
    }));

  const resSel = $("#oc-result", el), reasonSel = $("#oc-reason", el);
  resSel.addEventListener("change", () => { reasonSel.hidden = resSel.value !== "failure"; });
  $("#oc-submit", el).addEventListener("click", async () => {
    try {
      const body = {
        result: resSel.value,
        realized_profit_usd: parseFloat($("#oc-profit", el).value) || null,
        failure_reason: resSel.value === "failure" ? (reasonSel.value || null) : null,
      };
      const r = await api(`/api/opportunities/${o.id}/outcome`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast(`🧠 ${r.learning.note}`);
      await Promise.all([loadFeed(), loadBriefing(), loadOps()]);
    } catch (err) { toast("Could not record outcome: " + err.message); }
  });

  if (o.history && o.history.points && o.history.points.length > 2) {
    sparkline($("#spark-wrap", el), o.history);
  }
}

/* Sparkline: 2px line, 10% area wash, ≥8px end marker with surface ring,
   crosshair + tooltip on hover. Single series → no legend (title names it). */
function sparkline(wrap, hist) {
  const pts = hist.points, W = 560, H = 84, P = 8;
  const min = Math.min(...pts), max = Math.max(...pts), span = (max - min) || 1;
  const x = (i) => P + (i * (W - 2 * P)) / (pts.length - 1);
  const y = (v) => H - P - ((v - min) * (H - 2 * P)) / span;
  const path = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${path} L${x(pts.length - 1).toFixed(1)},${H - 2} L${x(0).toFixed(1)},${H - 2} Z`;
  wrap.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;display:block" role="img" aria-label="${esc(hist.label)}">
      <line x1="${P}" y1="${H - 2}" x2="${W - P}" y2="${H - 2}" stroke="var(--baseline)" stroke-width="1"/>
      <path d="${area}" fill="var(--accent)" opacity="0.1"/>
      <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="${x(pts.length - 1)}" cy="${y(pts[pts.length - 1])}" r="4" fill="var(--accent)"
              stroke="var(--surface)" stroke-width="2"/>
      <line id="xhair" y1="${P}" y2="${H - 2}" stroke="var(--grid)" stroke-width="1" visibility="hidden"/>
      <circle id="hovdot" r="4" fill="var(--accent)" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
    </svg>
    <div class="spark-tip" hidden></div>`;
  const svg = wrap.querySelector("svg"), tip = wrap.querySelector(".spark-tip");
  const xhair = wrap.querySelector("#xhair"), hovdot = wrap.querySelector("#hovdot");
  svg.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(pts.length - 1,
      Math.round(((ev.clientX - r.left) / r.width * W - P) / ((W - 2 * P) / (pts.length - 1)))));
    xhair.setAttribute("x1", x(i)); xhair.setAttribute("x2", x(i)); xhair.setAttribute("visibility", "visible");
    hovdot.setAttribute("cx", x(i)); hovdot.setAttribute("cy", y(pts[i])); hovdot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = (x(i) / W * 100) + "%";
    tip.style.top = (y(pts[i]) / H * 100) + "%";
    tip.textContent = `day ${i - pts.length + 1}: ${hist.unit === "$" ? fmtUSD(pts[i]) : Math.round(pts[i]).toLocaleString()}`;
  });
  svg.addEventListener("mouseleave", () => {
    tip.hidden = true; xhair.setAttribute("visibility", "hidden"); hovdot.setAttribute("visibility", "hidden");
  });
}

/* ------------------------------------------------------------------ ops */

async function loadOps() {
  const body = $("#ops-body");
  if (state.opsTab === "activity") {
    const r = await api("/api/activity");
    if (!r.items.length) {
      body.innerHTML = '<p class="wf-note">No activity yet — run a research cycle.</p>';
      return;
    }
    body.innerHTML = `<div class="act-feed">${r.items.map((it) => {
      const t = new Date(it.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      return `<div class="act-row act-${esc(it.kind)}">
        <span class="act-time">${t}</span>
        <span class="act-actor">${esc(it.actor)}</span>
        <span class="act-text">${esc(it.text)}</span></div>`;
    }).join("")}</div>`;
  } else if (state.opsTab === "discovery") {
    const d = await api("/api/discovery");
    if (!d.enabled) {
      body.innerHTML = `<p class="wf-note">The discovery engine runs in <b>live mode</b>: it sweeps
        Google Trends, Reddit commerce communities and eBay categories on a schedule, auto-promotes
        the best finds into the watched fleet, and (with an Anthropic key) has Claude judge every
        candidate. Demo mode uses a fixed simulated catalog, so there is nothing to discover here —
        run <code>python run.py serve --live</code> to turn it on.</p>`;
      return;
    }
    const srcRows = (d.sources || []).map((s) => `
      <div class="access-row"><span>${esc(s.name)}</span>
        <span class="yn ${s.ok ? "y" : "n"}">${s.ok ? "on" : "off"}</span></div>
      ${s.ok ? "" : `<div class="wf-note" style="margin:2px 0 8px">${esc(s.note)}</div>`}`).join("");
    const items = (d.found || []).map((c) => `
      <div class="pipe-item ${c.status === "active" ? "pub" : ""}">
        <b style="color:var(--ink)">${esc(c.name)}</b>
        <span class="r"> ${esc(c.kind)} · score ${Number(c.score).toFixed(2)} · via ${esc(c.source)}</span>
        <div class="r">${esc(c.reason || "")}</div>
        ${c.kind === "product"
          ? '<div class="r">➜ watching its sell side; add your buy quote (Buyee/Shopee/AliExpress price) in watchlist.json to price the flip</div>'
          : ""}
      </div>`).join("");
    const lr = d.last_run || {};
    body.innerHTML = `<div class="learn-cols">
      <div><h4>Discovery sources</h4>${srcRows}
        <div class="kv" style="margin-top:10px"><span>auto-found, being watched</span><b>${d.counts.active}</b></div>
        <div class="kv"><span>found all-time</span><b>${d.counts.total}</b></div>
        ${lr.found != null ? `<div class="kv"><span>last sweep</span><b>${lr.found} found · ${lr.promoted} promoted</b></div>` : ""}
        ${lr.ai ? `<div class="kv"><span>AI brain</span><b>${esc(lr.ai)}</b></div>` : ""}
      </div>
      <div style="grid-column: span 2"><h4>What the fleet found on its own</h4>
        ${items || '<div class="wf-note">Nothing yet — sweeps run every few cycles; candidates appear here, then must verify like everything else before reaching your feed.</div>'}
      </div>
    </div>`;
  } else if (state.opsTab === "agents") {
    const r = await api("/api/agents");
    body.innerHTML = `<div class="agents-grid">${r.agents.map((a) => `
      <div class="agent-card">
        <div class="agent-name"><span class="agent-live"></span>${esc(a.name)}</div>
        <div class="agent-desc">${esc(a.description)}</div>
        <div class="agent-stats">tick ${a.last_tick} · ${a.signals_last} signals last cycle ·
          ${a.signals_total.toLocaleString()} total · reliability ${pct(a.reliability)}</div>
      </div>`).join("")}</div>`;
  } else if (state.opsTab === "pipeline") {
    const s = await api("/api/stats");
    const rep = s.last_report;
    if (!rep) { body.innerHTML = '<p class="wf-note">No cycles yet.</p>'; return; }
    body.innerHTML = `
      <p class="wf-note" style="margin:0 0 10px">Cycle at tick ${rep.tick}: ${rep.signals} signals from
      ${rep.agents} agents → ${rep.anomalies} anomalies → ${rep.candidates} investigations →
      ${rep.published.length} published · ${rep.rejected.length} rejected · ${rep.reverified} re-verified ·
      ${rep.invalidated.length} invalidated.</p>
      <div class="pipe-cols">
        <div class="pipe-col"><h4>Published / refreshed</h4>${rep.published.map((p) => `
          <div class="pipe-item pub">${esc(p.title)} <span class="r">score ${p.score} · ${pct(p.confidence)}</span></div>`).join("") || '<div class="wf-note">none</div>'}</div>
        <div class="pipe-col"><h4>Rejected before reaching you</h4>${rep.rejected.map((p) => `
          <div class="pipe-item rej">${esc(p.title)} <div class="r">${esc(p.reason)}</div></div>`).join("") || '<div class="wf-note">none</div>'}</div>
        <div class="pipe-col"><h4>Invalidated (conditions changed)</h4>${rep.invalidated.map((p) => `
          <div class="pipe-item inv">${esc(p.title)} <div class="r">${esc(p.reason)}</div></div>`).join("") || '<div class="wf-note">none</div>'}</div>
      </div>`;
  } else if (state.opsTab === "learning") {
    const l = await api("/api/learning");
    const weights = Object.entries(l.weights).sort((a, b) => b[1] - a[1]);
    body.innerHTML = `<div class="learn-cols">
      <div><h4>Scoring weights (self-tuning)</h4>${weights.map(([k, v]) => `
        <div class="kv"><span>${esc(k.replace(/_/g, " "))}</span><b>${(v * 100).toFixed(1)}%</b></div>`).join("")}</div>
      <div><h4>Calibration & outcomes</h4>
        <div class="kv"><span>confidence calibration</span><b>×${l.calibration.toFixed(2)}</b></div>
        <div class="kv"><span>successes recorded</span><b>${l.outcomes.success}</b></div>
        <div class="kv"><span>failures recorded</span><b>${l.outcomes.failure}</b></div>
        <h4 style="margin-top:14px">Source reliability</h4>
        ${Object.entries(l.source_reliability).length
          ? Object.entries(l.source_reliability).map(([k, v]) => `
            <div class="kv"><span>${esc(k)}</span><b>${pct(v)}</b></div>`).join("")
          : '<div class="wf-note">all sources at the 80% prior — record outcomes to differentiate them</div>'}</div>
      <div><h4>Recent model adjustments</h4>
        ${l.adjustments.length ? l.adjustments.slice(-8).reverse().map((a) => `
          <div class="pipe-item ${a.result === "success" ? "pub" : "rej"}">${esc(a.note)}</div>`).join("")
          : '<div class="wf-note">none yet — the loop closes when you record outcomes</div>'}</div>
    </div>`;
  } else if (state.opsTab === "radar") {
    const r = await api("/api/radar");
    body.innerHTML = !r.items.length
      ? `<p class="wf-note">No catalysts on the radar. Add known future events to the
         <code>radar</code> section of watchlist.json — set releases, movie premieres, rule changes —
         and the AI computes when your prep window opens (buy lead time + sell-in).</p>`
      : `<div class="pipe-cols">${r.items.map((c) => `
          <div class="pipe-item ${c.status.includes("OPEN") ? "pub" : ""}">
            <b style="color:var(--ink)">${esc(c.label)}</b>
            <div class="r">${esc(c.date)} · in ${c.days_until} days · ${esc(c.status)}</div>
            ${c.note ? `<div class="r">${esc(c.note)}</div>` : ""}
            ${c.related ? `<div class="r">related: ${esc(c.related)}</div>` : ""}
          </div>`).join("")}</div>`;
  } else if (state.opsTab === "keys") {
    const s = await api("/api/settings");
    const insecure = !window.isSecureContext &&
      !["localhost", "127.0.0.1"].includes(location.hostname);
    const groups = {};
    s.keys.forEach((k) => (groups[k.group] = groups[k.group] || []).push(k));
    body.innerHTML = `
      ${insecure ? `<div class="locked-note" style="margin:0 0 12px;border-color:color-mix(in srgb,var(--critical) 55%,transparent);color:var(--critical)">
        ⚠ This dashboard is on plain HTTP — anyone on the network path can read what you type,
        and anyone who finds this page can change your keys. Before pasting real keys, protect it:
        run <code>bash deploy/setup_https_dashboard.sh</code> on your server (adds HTTPS + a password).</div>` : ""}
      <p class="wf-note" style="margin:0 0 12px">${esc(s.note)} Paste a value and press Save —
        leave a field empty to keep what's already there. Full step-by-step guide for every key:
        <b>docs/KEYS.md</b> in the repository.</p>
      ${Object.entries(groups).map(([g, keys]) => `
        <h4 style="margin:14px 0 6px">${esc(g)}</h4>
        ${keys.map((k) => `
          <div class="key-row" data-name="${esc(k.name)}">
            <div class="key-info">
              <div class="key-label">${esc(k.label)}
                <a class="key-link" href="${esc(k.url)}" target="_blank" rel="noopener">get it ↗</a></div>
              <div class="wf-note">${esc(k.help)}</div>
            </div>
            <div class="key-state">
              ${k.set ? `<span class="yn y">set</span> <span class="key-masked">${esc(k.masked)}</span>
                         <span class="key-src">${k.source === "app" ? "saved in app" : "from .env"}</span>
                         ${k.source === "app" ? '<button class="copy-btn key-clear" type="button">clear</button>' : ""}`
                      : '<span class="yn n">not set</span>'}
            </div>
            <input class="key-input" type="${k.secret ? "password" : "text"}"
                   placeholder="${k.set ? "paste new value to replace…" : "paste value…"}"
                   autocomplete="off" spellcheck="false">
          </div>`).join("")}`).join("")}
      <div style="margin-top:14px;display:flex;gap:10px;flex-wrap:wrap">
        <button id="keys-save" class="btn btn-primary" type="button">💾 Save keys</button>
        <button id="keys-test" class="btn" type="button">🧪 Test connections</button>
      </div>
      <div id="keys-test-out" style="margin-top:10px"></div>`;

    $("#keys-save").addEventListener("click", async () => {
      const updates = {};
      body.querySelectorAll(".key-row").forEach((row) => {
        const v = row.querySelector(".key-input").value.trim();
        if (v) updates[row.dataset.name] = v;
      });
      if (!Object.keys(updates).length) { toast("Nothing to save — paste a key first."); return; }
      try {
        await api("/api/settings", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify(updates) });
        toast("✅ Keys saved and applied — no restart needed.");
        loadOps().catch(() => {});
      } catch (err) { toast("Save failed: " + err.message, 8000); }
    });
    $("#keys-test").addEventListener("click", async () => {
      const out = $("#keys-test-out");
      out.innerHTML = '<p class="wf-note">Testing every configured service…</p>';
      try {
        const r = await api("/api/settings/test", { method: "POST" });
        out.innerHTML = r.results.length
          ? r.results.map((t) => `<div class="pipe-item ${t.ok ? "pub" : "rej"}">
              ${t.ok ? "✓" : "✗"} <b>${esc(t.label)}</b> <div class="r">${esc(t.note)}</div></div>`).join("")
          : '<p class="wf-note">No keys configured yet — save at least one, then test.</p>';
      } catch (err) { out.innerHTML = `<p class="wf-note">Test failed: ${esc(err.message)}</p>`; }
    });
    body.querySelectorAll(".key-clear").forEach((b) =>
      b.addEventListener("click", async () => {
        const name = b.closest(".key-row").dataset.name;
        try {
          await api("/api/settings", { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify({ [name]: "" }) });
          toast(`Cleared ${name} — falling back to .env if set there.`);
          loadOps().catch(() => {});
        } catch (err) { toast("Clear failed: " + err.message); }
      }));
  } else if (state.opsTab === "thailand") {
    const t = await api("/api/thailand");
    body.innerHTML = `<div class="th-grid">
      <div><h4 style="margin:0 0 8px;color:var(--ink);font-size:12.5px">Marketplace access from Thailand</h4>
        ${Object.entries(t.venue_access).map(([vid, a]) => `
          <div class="access-row"><span>${esc(t.venue_names[vid] || vid)}${a.proxy ? ' <span class="wf-note">(proxy)</span>' : ""}</span>
            <span class="yn ${a.buy ? "y" : "n"}">buy</span><span class="yn ${a.sell ? "y" : "n"}">sell</span></div>`).join("")}
      </div>
      <div><h4 style="margin:0 0 8px;color:var(--ink);font-size:12.5px">Import & duty rules</h4>
        <div class="kv"><span>VAT on imports</span><b>${pct(t.vat_rate)}</b></div>
        <div class="kv"><span>duty de-minimis</span><b>฿${t.de_minimis_thb.toLocaleString()}</b></div>
        ${Object.entries(t.duty_by_category).slice(0, 9).map(([k, v]) => `
          <div class="kv"><span>duty — ${esc(k.replace(/_/g, " "))}</span><b>${pct(v)}</b></div>`).join("")}
      </div>
      <div><h4 style="margin:0 0 8px;color:var(--ink);font-size:12.5px">Money & rules of thumb</h4>
        <ul class="th-notes">${t.payment_rails.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>
        <ul class="th-notes" style="margin-top:10px">${t.notes.map((n) => `<li class="wf-note">${esc(n)}</li>`).join("")}</ul>
      </div>
    </div>`;
  }
}

/* ---------------------------------------------------------------- wiring */

async function loadCategories() {
  const s = await api("/api/stats");
  const sel = $("#f-category");
  const cur = sel.value;
  sel.innerHTML = '<option value="">all categories</option>' +
    s.categories.map((c) => `<option value="${esc(c)}">${esc(c.replace(/_/g, " "))}</option>`).join("");
  sel.value = cur;
}

async function refreshAll() {
  await Promise.all([loadBriefing(), loadFeed(), loadOps(), loadCategories()]);
}

function init() {
  $("#plan-select").addEventListener("change", async (e) => {
    state.plan = e.target.value;
    await refreshAll();
    if (state.selectedId) selectOpportunity(state.selectedId).catch(() => {});
  });

  $("#cycle-btn").addEventListener("click", async () => {
    const btn = $("#cycle-btn");
    btn.disabled = true; btn.textContent = "⏳ agents researching…";
    try {
      const r = await api("/api/cycle", { method: "POST" });
      toast(`Cycle ${r.tick}: ${r.signals} signals → ${r.anomalies} anomalies → ${r.candidates} investigated → `
        + `${r.published.length} published, ${r.rejected.length} rejected, ${r.invalidated.length} invalidated.`);
      await refreshAll();
      if (state.selectedId) selectOpportunity(state.selectedId).catch(() => {});
    } catch (err) { toast("Cycle failed: " + err.message); }
    btn.disabled = false; btn.textContent = "▶ Run research cycle";
  });

  const f = state.filters;
  $("#f-q").addEventListener("input", (e) => { f.q = e.target.value.trim(); loadFeed(); });
  $("#f-status").addEventListener("change", (e) => { f.status = e.target.value; loadFeed(); });
  $("#f-category").addEventListener("change", (e) => { f.category = e.target.value; loadFeed(); });
  $("#f-minscore").addEventListener("change", (e) => { f.min_score = e.target.value; loadFeed(); });

  document.querySelectorAll(".ops-tab").forEach((b) =>
    b.addEventListener("click", () => {
      document.querySelectorAll(".ops-tab").forEach((x) => x.classList.toggle("active", x === b));
      state.opsTab = b.dataset.tab;
      loadOps();
    }));

  refreshAll().then(async () => {
    // preselect the top-scoring active opportunity so the evidence panel isn't empty
    const r = await api(`/api/opportunities?plan=${state.plan}&status=active`);
    if (r.opportunities.length) selectOpportunity(r.opportunities[0].id);
  }).catch((e) => toast("Failed to load: " + e.message));

  setInterval(() => { loadBriefing().catch(() => {}); loadFeed().catch(() => {}); }, 60_000);
  setInterval(() => { if (state.opsTab === "activity") loadOps().catch(() => {}); }, 12_000);
}

init();
