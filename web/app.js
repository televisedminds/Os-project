/* Opportunity OS dashboard — vanilla JS, no build step. */

"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  plan: "pro",
  selectedId: null,
  filters: { q: "", status: "active", category: "", min_score: 0 },
  opsTab: "agents",
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

async function api(path, opts) {
  const r = await fetch(path, opts);
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
  const [b, s] = await Promise.all([api(`/api/briefing?plan=${state.plan}`), api("/api/stats")]);
  $("#tick-label").textContent = `tick ${s.tick} · 1 cycle ≈ 1 market day`;
  $("#briefing-headline").textContent = b.headline;
  $("#briefing-notes").textContent = b.notes.join("  ");
  const conf = s.avg_confidence || 0;
  const lastInv = b.counts.invalidated_last_cycle;
  $("#stat-tiles").innerHTML = `
    ${tile("Verified active", s.opportunities_active, "published & re-verified each cycle")}
    ${tile("New today", b.counts.new_today, `tick ${s.tick}`)}
    ${tile("Avg confidence", pct(conf), "reliability-weighted consensus")}
    ${tile("Est. profit pool", fmtUSD(s.profit_pool_usd, 0), "sum of active base-case nets")}
    ${tile("Invalidated", `${lastInv}<span style="font-size:13px;color:var(--muted)"> last cycle</span>`,
           `${s.invalidated} all-time — conditions changed`)}
    ${tile("Signals stored", s.signals_total.toLocaleString(), `${s.anomalies_total.toLocaleString()} anomalies flagged`)}
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

    <div class="d-section"><h3>Why the AI believes this — the investigation chain</h3>
      <div class="why">${o.why_chain.map((s) => `
        <div class="why-step"><div class="why-q">${esc(s.question)}</div>
        <div class="why-a">${esc(s.finding)}</div></div>`).join("")}
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

    <div class="d-section"><h3>Score breakdown (weights learned from outcomes)</h3>
      ${Object.entries(o.score.factors)
        .sort((a, b) => (o.score.weights[b[0]] ?? 0) - (o.score.weights[a[0]] ?? 0))
        .map(([k, v]) => `
        <div class="fbar-row" title="contributes ${o.score.contributions[k] ?? 0} pts at weight ${((o.score.weights[k] ?? 0) * 100).toFixed(0)}%">
          <div class="fbar-label">${esc(k.replace(/_/g, " "))}</div>
          <div class="fbar-track"><div class="fbar-fill" style="width:${v}%"></div></div>
          <div class="fbar-num">${Math.round(v)}</div>
        </div>`).join("")}
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

    <div class="d-section"><h3>Execution playbook</h3>${playbook(o)}</div>

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
  return `
    ${pb.steps.map((s) => `
      <div class="pb-step"><div class="pb-n">${s.order}</div>
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
  if (state.opsTab === "agents") {
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
}

init();
