/* Atlas phone app. No build step, no dependencies.
 * Every value from the server is escaped with esc() before it touches the page:
 * headlines and report text are written by other people. */
"use strict";
(() => {

const $app = document.getElementById("app");
const $nav = document.getElementById("nav");
const TOKEN_KEY = "atlas.passcode";
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: fine */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};
const state = { session: null, home: null, chat: { sid: null, msgs: [], busy: false }, laterAlerts: new Set() };
const SHORT_LABEL = { ca_equity: "CA STOCKS", us_equity: "US STOCKS", intl_equity: "INTL STOCKS", fixed_income: "BONDS", cash: "CASH", other: "OTHER" };
const COLORS = { ca_equity: "#FF8A3D", us_equity: "#FFB020", intl_equity: "#2F7BFF", fixed_income: "#3FE0FF", cash: "#4E6B78", other: "#9A7BFF" };

// ---------- helpers ----------
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const num = (v) => (v === null || v === undefined || v === "" ? null : Number(v));
function money(v, cents = false) {
  const n = num(v);
  if (n === null || Number.isNaN(n)) return "n/a";
  const s = Math.abs(n).toLocaleString("en-CA", { minimumFractionDigits: cents ? 2 : 0, maximumFractionDigits: cents ? 2 : 0 });
  return (n < 0 ? "−$" : "$") + s;
}
const signed = (v) => (num(v) >= 0 ? "+" : "−") + money(Math.abs(num(v)));
const cap1 = (t) => t.charAt(0).toUpperCase() + t.slice(1);
const short = (t) => String(t || "").replace(/\.(US|TO|V|NEO|CN)$/, "");
function when(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-CA", { month: "short", day: "numeric" });
}
function clock() {
  return new Date().toLocaleTimeString("en-CA", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "America/Toronto" }) + " ET";
}
const corners = '<span class="corner tl"></span><span class="corner tr"></span><span class="corner bl"></span><span class="corner br"></span>';
function toast(text) {
  const t = document.createElement("div");
  t.className = "toast"; t.setAttribute("role", "status"); t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

/* In-page confirmation sheet. Native confirm()/prompt() are blocked in some app views. */
function ask({ title, body, ok, danger = false, input = null }) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "sheet-wrap";
    wrap.innerHTML = `<div class="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-t">
      <span class="label" id="sheet-t">${esc(title)}</span><p>${esc(body)}</p>
      ${input ? `<label class="label" for="sheet-in">${esc(input)}</label><input id="sheet-in" autocomplete="off">` : ""}
      <div class="grid2"><button class="btn" data-a="no">CANCEL</button>
      <button class="btn ${danger ? "danger" : "primary"}" data-a="yes">${esc(ok)}</button></div></div>`;
    const done = (yes) => { wrap.remove(); resolve({ ok: yes, value: yes && input ? wrap.querySelector("#sheet-in").value.trim() : "" }); };
    wrap.addEventListener("click", (e) => { const a = e.target.closest("[data-a]"); if (a) done(a.dataset.a === "yes"); else if (e.target === wrap) done(false); });
    wrap.addEventListener("keydown", (e) => { if (e.key === "Escape") done(false); });
    document.body.appendChild(wrap);
    (wrap.querySelector("#sheet-in") || wrap.querySelector('[data-a="yes"]')).focus();
  });
}

// ---------- api ----------
class AuthError extends Error {}
async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  const tok = store.get(TOKEN_KEY);
  if (tok) headers["X-Atlas-Token"] = tok;
  const resp = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  if (resp.status === 401 || (resp.status === 403 && !opts.method)) {
    const body = await resp.json().catch(() => ({}));
    throw new AuthError(body.detail || "passcode required");
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(body.detail || `HTTP ${resp.status}`);
  return body;
}

// ---------- shell ----------
function topBar(left, right = "") {
  return `<header class="top">${left}<span class="mono">${esc(clock())}</span>${right}</header>`;
}
function backBar(label, cls = "") {
  return `<header class="top"><a class="back" href="#home" aria-label="Back to home">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></a>
    <span class="${cls}">${label}</span><span class="mono">${esc(clock())}</span></header>`;
}
function modeTags(h) {
  const tags = [];
  if (h && h.trading === "paper") tags.push('<span class="tag amber">SIM MODE</span>');
  if (h && h.sample_prices) tags.push('<span class="tag amber">SAMPLE DATA</span>');
  if (state.session && state.session.demo) tags.push('<span class="tag cyan">DEMO</span>');
  return `<div class="tags">${tags.join("")}</div>`;
}
function setNav(view) {
  $nav.hidden = false;
  $nav.querySelectorAll(".navbtn").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + view));
  const n = state.home ? state.home.plans.plans.length : 0;
  const b = document.getElementById("planbadge");
  b.hidden = !n; b.textContent = n;
}

// ---------- login ----------
function renderLogin(message) {
  $nav.hidden = true;
  $app.innerHTML = `
    <section class="login" aria-labelledby="lt">
      <div class="kicker">ATLAS // SECURE LINK</div>
      <h1 id="lt" class="big">ENTER PASSCODE</h1>
      <p class="empty">Shown on your computer when you ran <span class="mono">atlas serve --phone</span>.</p>
      <form id="lf" class="kill">
        <label class="label" for="pc">Passcode</label>
        <input id="pc" name="pc" autocomplete="current-password" autocapitalize="off" spellcheck="false" required>
        ${message ? `<div class="err" role="alert">${esc(message)}</div>` : ""}
        <button class="btn primary wide" type="submit">LINK</button>
      </form>
    </section>`;
  document.getElementById("lf").addEventListener("submit", (e) => {
    e.preventDefault();
    store.set(TOKEN_KEY, document.getElementById("pc").value.trim());
    boot(true);
  });
}

// ---------- home ----------
function sparkline(snaps, total) {
  const pts = (snaps || []).map((s) => num(s.total)).filter((v) => v !== null);
  if (num(total) !== null) pts.push(num(total));
  const W = 356, H = 58;
  if (pts.length < 2) {
    return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true"><g stroke="#0F3A45"><line x1="0" y1="14" x2="${W}" y2="14"/><line x1="0" y1="29" x2="${W}" y2="29"/><line x1="0" y1="44" x2="${W}" y2="44"/></g>
      <line x1="0" y1="29" x2="${W - 4}" y2="29" stroke="var(--accent)" stroke-width="1.8"/><circle cx="${W - 4}" cy="29" r="3.5" fill="var(--accent)"/></svg>`;
  }
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const xy = pts.map((v, i) => [Math.round((i / (pts.length - 1)) * (W - 6)), Math.round(H - 8 - ((v - lo) / span) * (H - 16))]);
  const last = xy[xy.length - 1];
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" aria-hidden="true"><g stroke="#0F3A45"><line x1="0" y1="14" x2="${W}" y2="14"/><line x1="0" y1="29" x2="${W}" y2="29"/><line x1="0" y1="44" x2="${W}" y2="44"/></g>
    <polyline points="${xy.map((p) => p.join(",")).join(" ")}" fill="none" stroke="var(--accent)" stroke-width="1.8" stroke-linejoin="round"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="3.5" fill="var(--accent)"/><circle cx="${last[0]}" cy="${last[1]}" r="8" fill="none" stroke="var(--accent)" class="pulse"/></svg>`;
}

function donut(buckets) {
  const C = 2 * Math.PI * 62;
  let off = 0;
  const segs = buckets.filter((b) => num(b.pct) > 0).map((b) => {
    const len = (num(b.pct) / 100) * C;
    const seg = `<circle cx="80" cy="80" r="62" stroke="${COLORS[b.bucket] || "#888"}" stroke-dasharray="${Math.max(len - 2, 0.5).toFixed(1)} ${C.toFixed(1)}" stroke-dashoffset="${(-off).toFixed(1)}" ${b.outside_band ? 'class="pulse"' : ""}/>`;
    off += len;
    return seg;
  });
  const drifts = buckets.filter((b) => b.outside_band).length;
  const label = buckets.map((b) => `${b.label} ${b.pct}%${b.target_pct ? " vs " + b.target_pct + "% target" : ""}`).join(", ");
  return `<svg class="donut" viewBox="0 0 160 160" role="img" aria-label="${esc("Allocation: " + label)}">
    <circle cx="80" cy="80" r="77" fill="none" stroke="var(--accent)" stroke-width="1" stroke-dasharray="2 6" class="spin"/>
    <circle cx="80" cy="80" r="44" fill="none" stroke="#12505E" stroke-width="1" stroke-dasharray="10 4" class="spinr"/>
    <g transform="rotate(-90 80 80)" fill="none" stroke-width="10">${segs.join("")}</g>
    <text x="80" y="78" text-anchor="middle" fill="#D8F6FF" font-family="Orbitron, sans-serif" font-size="13" font-weight="700" letter-spacing="1">MIX</text>
    <text x="80" y="96" text-anchor="middle" fill="${drifts ? "#FFB020" : "#3FE0FF"}" font-family="Share Tech Mono, monospace" font-size="12">${drifts ? drifts + " DRIFT" : "ON PLAN"}</text></svg>`;
}

function trackedAssets(h) {
  const byTicker = new Map();
  for (const p of h.holdings.positions) {
    const cur = byTicker.get(p.ticker) || { ticker: p.ticker, name: p.name, value: 0, weight: 0, type: p.type };
    cur.value += num(p.value_cad) || 0; cur.weight += num(p.weight_pct) || 0;
    byTicker.set(p.ticker, cur);
  }
  const breached = new Set(h.risk.current_breaches.map((b) => b.subject));
  const soon = new Map(h.events.events.filter((e) => e.kind === "earnings").map((e) => [e.ticker, e]));
  const inNews = new Set(h.headlines.filter((x) => x.major).map((x) => x.ticker));
  const rows = [...byTicker.values()].sort((a, b) => b.value - a.value).map((a) => {
    let cls = "hold", st = "HOLD", note = "ON PLAN", mark = "◆";
    if (breached.has(a.ticker)) { cls = "trim"; st = "TRIM"; mark = "▲"; note = `OVER ${h.risk.rules.find((r) => r.key === "max_single_stock_pct")?.value || ""}% CAP`; }
    else if (inNews.has(a.ticker)) { cls = "watch"; st = "PAUSED"; mark = "◇"; note = "IN THE HEADLINES"; }
    else if (soon.has(a.ticker)) {
      const days = Math.max(0, Math.round((new Date(soon.get(a.ticker).at) - Date.now()) / 864e5));
      cls = "watch"; st = "WATCH"; mark = "◇"; note = `EARNINGS T-${days}D`;
    }
    return `<div class="asset ${cls}"><span aria-hidden="true">${mark}</span><span class="t">${esc(short(a.ticker))}</span>
      <span class="note">${esc(note)} · ${a.weight.toFixed(1)}%</span><span class="st">${esc(st)}</span></div>`;
  });
  return `<section class="list" aria-label="Tracked assets"><span class="label">TRACKED ASSETS · ${String(byTicker.size).padStart(2, "0")}</span>${rows.join("")}</section>`;
}

function alertsBlock(h) {
  const out = [];
  const ks = h.risk.kill_switch;
  if (ks.engaged) {
    out.push(`<section class="alert red" aria-label="Kill switch"><span class="head">■ KILL SWITCH ENGAGED</span>
      <p>All trading is stopped. ${esc(ks.reason || "")}</p><div class="btns"><a class="btn ghost-amber" href="#guard">GUARDRAILS</a></div></section>`);
  }
  const plans = h.plans.plans;
  h.risk.current_breaches.forEach((b, i) => {
    if (state.laterAlerts.has(b.subject)) return;
    const plan = plans.find((p) => p.legs.some((l) => l.ticker === b.subject)) || plans[0];
    const label = b.rule === "max_single_stock_pct" ? "POSITION LIMIT EXCEEDED" : b.rule === "max_sector_pct" ? "SECTOR LIMIT EXCEEDED" : "CASH BELOW FLOOR";
    out.push(`<section class="alert" aria-label="Alert"><span class="head"><span class="pulse">▲</span> ALERT ${String(i + 1).padStart(2, "0")} · ${label}</span>
      <p>${esc(b.detail)}${plan ? " Your board has a plan ready." : ""}</p>
      <div class="btns">${plan ? `<a class="btn amber" href="#plan/${encodeURIComponent(plan.plan_id)}">SHOW ME</a>` : ""}
      <button class="btn ghost-amber" data-later="${esc(b.subject)}">LATER</button></div></section>`);
  });
  const covered = new Set(h.risk.current_breaches.map((b) => b.subject));
  plans.filter((p) => !p.legs.some((l) => covered.has(l.ticker))).forEach((p) => {
    const legs = p.legs.map((l) => `${l.side.toLowerCase()} ${l.quantity} ${short(l.ticker)}`).join(", ");
    out.push(`<section class="alert" aria-label="Decision waiting"><span class="head">◆ DECISION WAITING · ${esc(p.proposed_by.toUpperCase())}</span>
      <p>${esc(legs)}</p><div class="btns"><a class="btn amber" href="#plan/${encodeURIComponent(p.plan_id)}">REVIEW</a></div></section>`);
  });
  return out.join("");
}

function renderHome() {
  const h = state.home;
  setNav("home");
  const ov = h.overview;
  const total = num(ov.net_worth), day = num(ov.day_change);
  const dayPct = day !== null && total ? (day / (total - day)) * 100 : null;
  const goal = h.goals.goals[0];
  const goalBar = goal ? (() => {
    const filled = Math.max(0, Math.min(10, Math.round(num(goal.progress_pct) / 10)));
    return `${esc(goal.id.toUpperCase().slice(0, 12))} ${"▮".repeat(filled)}${"▯".repeat(10 - filled)} ${Math.round(num(goal.progress_pct))}%`;
  })() : "";
  const buckets = h.allocation.buckets;
  const legend = buckets.map((b) => `<div class="li${b.outside_band ? " drift" : ""}"><span class="sw" style="background:${COLORS[b.bucket] || "#888"}"></span>
      <span class="nm">${esc(SHORT_LABEL[b.bucket] || b.label.toUpperCase())}</span><span class="nt">${Math.round(num(b.pct))}/${b.target_pct === null ? "–" : Math.round(num(b.target_pct))}</span></div>`).join("");
  const events = h.events.events.slice(0, 4).map((e) => `<div class="ev"><span class="d">${esc(when(e.at))}</span><span>${esc(e.title)}</span></div>`).join("");
  const flags = ov.data_flags.map((f) => `<div class="flag ${esc(f.severity)}">[${esc(f.severity)}] ${esc(f.message)}</div>`).join("");
  $app.innerHTML = `
    ${topBar('<span><span class="live pulse">◉</span> ATLAS // ONLINE</span>', "")}
    <div class="heading"><div><div class="kicker">COMMAND DECK</div><div class="title">YOUR MONEY</div></div>${modeTags(h)}</div>
    <section class="panel glow" aria-label="Net worth">${corners}
      <div class="row label"><span>NET WORTH // TOTAL ASSETS</span><span>CAD</span></div>
      <div class="networth"><span class="v">${esc(money(total))}</span>
        ${day !== null ? `<span class="mono ${day >= 0 ? "up" : "down"}">${day >= 0 ? "▲" : "▼"} ${Math.abs(dayPct).toFixed(1)}%</span>` : ""}</div>
      ${sparkline(h.history.snapshots, ov.net_worth)}
      <div class="row label"><span>Δ TODAY ${day !== null ? esc(signed(day)) : "n/a"}</span><span>${goalBar}</span></div>
    </section>
    ${alertsBlock(h)}
    <section class="panel" aria-label="Allocation"><div class="mix">${donut(buckets)}
      <div class="legend"><span class="label">ALLOCATION · NOW/TGT</span>${legend}</div></div></section>
    ${trackedAssets(h)}
    ${events ? `<section class="panel" aria-label="This week"><span class="label">THIS WEEK</span><div class="events">${events}</div></section>` : ""}
    ${flags ? `<section aria-label="Data notes" class="kill"><span class="label">DATA NOTES</span>${flags}</section>` : ""}
    <div class="note-line">Cash ${esc(money(ov.cash))} (${esc(ov.cash_pct)}%) · ${ov.accounts.length} accounts · updated ${esc(clock())}</div>`;
  $app.querySelectorAll("[data-later]").forEach((b) => b.addEventListener("click", () => { state.laterAlerts.add(b.dataset.later); renderHome(); }));
}

// ---------- plans ----------
function renderPlans() {
  setNav("plans");
  const plans = state.home.plans.plans;
  const body = plans.length ? plans.map((p) => `
    <a class="panel" style="text-decoration:none;color:inherit" href="#plan/${encodeURIComponent(p.plan_id)}">
      <div class="row"><span class="label">PROPOSED BY ${esc(p.proposed_by.toUpperCase())}</span><span class="label">${esc(when(p.created_at))}</span></div>
      ${p.legs.map((l) => `<div class="leg${l.status === "blocked" ? " blocked" : ""}"><span class="what">${esc(l.side)} ${esc(l.quantity)} ${esc(short(l.ticker))}</span><span class="rat">${esc(l.rationale)}</span></div>`).join("")}
      <span class="btn primary" style="align-self:flex-start">REVIEW</span></a>`).join("")
    : '<p class="empty">Nothing is waiting for your decision. When the Advisor proposes a trade, it appears here.</p>';
  $app.innerHTML = `${topBar('<span>◆ DECISIONS</span>', "")}
    <div class="heading"><div><div class="kicker">WAITING FOR YOU</div><div class="title">DECISIONS</div></div>${modeTags(state.home)}</div>${body}`;
}

async function renderPlan(id) {
  setNav("plans");
  $app.innerHTML = `${backBar("LOADING…")}<p class="empty">Loading the decision card…</p>`;
  let d, board;
  try {
    d = await api("/plans/" + encodeURIComponent(id));
    const subj = d.legs[0] ? d.legs[0].ticker : "portfolio";
    board = await api("/board?subject=" + encodeURIComponent(subj)).catch(() => null);
  } catch (e) { if (e instanceof AuthError) return renderLogin(e.message); $app.innerHTML = `${backBar("DECISION")}<p class="err">${esc(e.message)}</p>`; return; }
  const open = d.legs.filter((l) => l.status === "proposed");
  const sells = d.legs.filter((l) => l.side === "SELL");
  const total = d.legs.reduce((s, l) => s + (num(l.est_value_cad) || 0) * (l.side === "SELL" ? -1 : 1), 0);
  const headline = d.legs.length === 1
    ? `${d.legs[0].side === "SELL" ? "TRIM" : "BUY"} ${esc(short(d.legs[0].ticker))} <span style="color:var(--amber);text-shadow:0 0 12px #FFB02088">${esc(d.legs[0].side === "SELL" ? "−" : "+")}${esc(money(Math.abs(total)))}</span>`
    : `REBALANCE · ${d.legs.length} LEGS`;
  // Share bar for a single-stock trim, from the Risk Officer's own numbers.
  let bars = "";
  const capCheck = (d.legs[0]?.checks || []).find((c) => c.rule === "max_single_stock_pct" && c.applicable);
  const m = capCheck && /([\d.]+)% -> ([\d.]+)%/.exec(capCheck.detail);
  if (m && sells.length) {
    const cap = parseFloat(capCheck.limit);
    const cells = (v) => Array.from({ length: 20 }, (_, i) => `<i class="${i < Math.round(v) ? (v > cap ? "over" : "on") : ""}"></i>`).join("");
    const pos = Math.min(100, cap * 5);
    bars = `<section class="panel glow" aria-label="Share of portfolio">${corners}
      <span class="label">${esc(short(d.legs[0].ticker))} SHARE OF PORTFOLIO · │ = CAP</span>
      <div class="bars">
        <div class="bar-row"><span class="k">NOW</span><span class="cells">${cells(Number(m[1]))}<span class="cap" style="left:${pos}%"></span></span><span class="v" style="color:var(--amber)">${Number(m[1]).toFixed(0)}%</span></div>
        <div class="bar-row"><span class="k">AFTER</span><span class="cells">${cells(Number(m[2]))}<span class="cap" style="left:${pos}%"></span></span><span class="v up">${Number(m[2]).toFixed(0)}%</span></div>
      </div></section>`;
  }
  const reasons = d.reports.filter((r) => r.stance !== "NO VIEW").slice(0, 2);
  const allChecks = d.legs.flatMap((l) => l.checks.filter((c) => c.applicable !== false).map((c) => ({ ...c, leg: l })));
  const failed = allChecks.filter((c) => !c.passed);
  const taxNote = d.legs.find((l) => /taxable account/.test(l.rationale));
  const checkRows = d.legs.length === 1
    ? allChecks.map((c) => `<div class="c ${c.passed ? "ok" : "no"}"><span>${c.passed ? "[✓]" : "[!]"}</span><span>${esc(c.label.toUpperCase())}</span></div>`).join("")
    : (failed.length ? failed.map((c) => `<div class="c no"><span>[!]</span><span>${esc(short(c.leg.ticker))}: ${esc(c.label.toUpperCase())} – ${esc(c.detail)}</span></div>`).join("")
      : `<div class="c ok"><span>[✓]</span><span>ALL ${allChecks.length} CHECKS CLEAR ACROSS ${d.legs.length} LEGS</span></div>`);
  let boardLine = "";
  if (board) {
    const agree = Object.entries(board.votes).sort((a, b) => b[1] - a[1]);
    boardLine = agree.length ? `${agree.map(([s, n]) => `${n} ${s}`).join(" · ")}${board.dissent.length ? ` · dissent: ${board.dissent.join(", ")}` : ""}` : "No reports on this yet.";
  }
  const done = !open.length;
  $app.innerHTML = `
    ${backBar('<span class="pulse">▲</span> DIRECTIVE · SIM', "mono")}
    <div><div class="kicker">PROPOSED BY ${esc(d.proposed_by.toUpperCase())}</div><h1 class="big">${headline}</h1></div>
    ${bars}
    <section class="panel" aria-label="Legs"><span class="label">ORDERS · LIMIT · PAPER</span>
      ${d.legs.map((l) => `<div class="leg${l.status === "blocked" ? " blocked" : ""}">
        <div class="row"><span class="what">${esc(l.side)} ${esc(l.quantity)} ${esc(short(l.ticker))} @ ${esc(l.limit_price)}</span><span class="status">${esc(l.status.toUpperCase())}</span></div>
        <span class="rat">${esc(l.rationale)}</span></div>`).join("")}
    </section>
    <section class="why" aria-label="Why">
      ${reasons.map((r) => `<div class="w"><span class="k">${esc(r.agent === "risk_officer" ? "RULE" : "REASON")}</span><span>${esc(r.finding)}</span></div>`).join("")}
      ${taxNote ? `<div class="w"><span class="k amber">TAX</span><span>${esc(cap1(taxNote.rationale.split(": ").slice(1).join(": ") || taxNote.rationale))}</span></div>` : ""}
      ${board ? `<div class="w"><span class="k dim">BOARD</span><a href="#board/${encodeURIComponent(board.subject)}" style="color:var(--text)">${esc(boardLine)} ›</a></div>` : ""}
    </section>
    <section class="panel" aria-label="Protocol check"><span class="label">PROTOCOL CHECK · ${allChecks.length - failed.length}/${allChecks.length} CLEAR</span>
      <div class="checks">${checkRows}</div></section>
    <div class="kill">
      <button id="auth" class="btn primary wide" ${done ? "disabled" : ""}>${done ? "DECIDED" : "AUTHORIZE · SIM TRADE"}</button>
      <div class="grid2"><a class="btn" href="#chat" id="brief">BRIEF ME</a><button class="btn" id="stand" ${done ? "disabled" : ""}>STAND DOWN</button></div>
      <span class="note-line" style="text-align:center">SIMULATION · NO CAPITAL MOVES UNTIL A BROKER IS LINKED</span>
    </div>`;
  document.getElementById("brief").addEventListener("click", () => {
    state.chat.pending = `Brief me on plan ${d.plan_id}: what it does, why, and the risks.`;
  });
  const act = async (path, sheet, okText) => {
    if (!(await ask(sheet)).ok) return;
    try { await api(path, { method: "POST" }); toast(okText); await refreshHome(); renderPlan(id); }
    catch (e) { if (e instanceof AuthError) return renderLogin(e.message); toast(e.message); }
  };
  if (!done) {
    document.getElementById("auth").addEventListener("click", () => act(`/plans/${encodeURIComponent(id)}/approve`,
      { title: "AUTHORIZE · SIM TRADE", body: `Send ${open.length} paper order(s)? The Risk Officer checks each one again first. No real money moves.`, ok: "AUTHORIZE" },
      "Authorized. Orders sent to the paper broker."));
    document.getElementById("stand").addEventListener("click", () => act(`/plans/${encodeURIComponent(id)}/dismiss`,
      { title: "STAND DOWN", body: "Drop this plan? The Advisor can propose a new one tomorrow.", ok: "STAND DOWN" }, "Stood down."));
  }
}

// ---------- board ----------
async function renderBoard(subject) {
  setNav("board");
  $app.innerHTML = `${topBar('<span>◉ THE BOARD</span>')}<p class="empty">Gathering reports…</p>`;
  let b;
  try { b = await api("/board" + (subject ? "?subject=" + encodeURIComponent(subject) : "")); }
  catch (e) { if (e instanceof AuthError) return renderLogin(e.message); $app.innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  const live = b.agents.filter((a) => a.live).length;
  const votes = Object.entries(b.votes);
  const n = votes.reduce((s, [, c]) => s + c, 0);
  const majority = votes.sort((x, y) => y[1] - x[1])[0];
  const stanceColor = (s) => (s === "NO VIEW" ? "var(--dim)" : s === (majority && majority[0]) ? "var(--accent)" : "var(--dissent)");
  const cells = votes.flatMap(([s, c]) => Array.from({ length: c }, () => `<i style="background:${stanceColor(s)}"></i>`)).join("");
  const summary = n === 0 ? `No live agent has reported on ${b.subject} yet.`
    : `On ${b.subject}: ${votes.map(([s, c]) => `${c} ${s}`).join(", ")}.` + (b.dissent.length ? ` ${b.dissent.map((x) => x.replace("_", " ")).join(", ")} disagree${b.dissent.length === 1 ? "s" : ""}; hear the case before you decide.` : "");
  const cards = b.agents.map((a) => {
    const r = a.report;
    const dissenting = b.dissent.includes(a.agent);
    const stance = r ? r.stance : (a.live ? "—" : "OFF");
    const foot = !a.live ? "NOT BUILT YET" : r ? `REPORTED ${when(r.as_of)}` : a.last_report_at ? "NO REPORT ON THIS" : "IDLE";
    return `<div class="agent${a.live ? "" : " off"}${dissenting ? " dissent" : ""}">
      <div class="hdr"><span>${esc(a.code)}</span><span class="vote" style="color:${r ? stanceColor(r.stance) : "var(--off)"}">${esc(stance)}</span></div>
      <span class="role">${esc(a.role)}</span>
      <span class="find">${r ? esc(r.finding) : a.live ? "Nothing filed on this subject." : "Arrives in a later phase."}</span>
      <span class="foot"><span style="color:${a.live ? "var(--accent)" : "var(--off)"}">●</span> ${esc(foot)}</span></div>`;
  }).join("");
  $app.innerHTML = `
    ${topBar(`<span class="live"><span class="pulse">◉</span> THE BOARD · ${live} LIVE</span>`)}
    <div class="chips" role="tablist" aria-label="Subject">${b.subjects.map((s) => `<button class="chip${s === b.subject ? " on" : ""}" data-subj="${esc(s)}" role="tab" aria-selected="${s === b.subject}">${esc(short(s))}</button>`).join("")}</div>
    <section class="panel glow" aria-label="Atlas summary">${corners}
      <div><div class="title">ATLAS</div><div class="label">CHIEF OF STAFF · REPORTS TO YOU</div></div>
      <p style="margin:0">${esc(summary)}</p>
      ${n ? `<div class="votes" style="grid-template-columns:repeat(${n},minmax(0,1fr))" role="img" aria-label="${esc(votes.map(([s, c]) => c + " " + s).join(", "))}">${cells}</div>` : ""}
    </section>
    <div class="agents">${cards}</div>
    <div class="locked"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="5" y="11" width="14" height="10"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>
      <span>EXE · TRADER: PAPER ONLY. Acts only on trades you authorize.</span></div>
    <a class="btn wide" href="#chat" id="ask">ASK ATLAS ABOUT ${esc(short(b.subject))}</a>`;
  $app.querySelectorAll("[data-subj]").forEach((c) => c.addEventListener("click", () => { location.hash = "#board/" + encodeURIComponent(c.dataset.subj); }));
  document.getElementById("ask").addEventListener("click", () => { state.chat.pending = `What does the board say about ${b.subject}? Include any dissent.`; });
}

// ---------- chat ----------
const QUICK = ["Brief me", "Why am I up or down today?", "Any rule breaches?", "How are my goals?", "What's on the calendar this week?"];
function renderChat() {
  setNav("chat");
  const c = state.chat;
  const msgs = c.msgs.map((m) => `<div class="msg ${m.who}"><span class="who">&gt; ${m.who === "me" ? "YOU" : "ATLAS"}</span><div class="body">${esc(m.text)}</div></div>`).join("");
  const engine = state.session ? state.session.chat_engine : "";
  $app.innerHTML = `
    ${topBar(`<span class="live"><span class="pulse">◉</span> ATLAS · ${c.busy ? "TRANSMITTING" : "CH-01"}</span>`, `<span class="tag cyan">${esc(engine.toUpperCase())}</span>`)}
    ${c.msgs.length ? "" : `<div class="orbhead" aria-hidden="true"><svg viewBox="0 0 220 220">
        <circle cx="110" cy="110" r="106" fill="none" stroke="#12505E"/>
        <circle cx="110" cy="110" r="88" fill="none" stroke="var(--accent)" stroke-width="2" stroke-dasharray="60 18 10 18" class="spin"/>
        <circle cx="110" cy="110" r="76" fill="none" stroke="#2F7BFF" stroke-dasharray="4 4" class="spinr"/>
        <circle cx="110" cy="110" r="54" fill="#06141A" stroke="#12505E"/>
        <circle cx="110" cy="110" r="40" fill="var(--accent)" opacity="0.15" class="pulse"/>
        <circle cx="110" cy="110" r="26" fill="var(--accent)" opacity="0.55"/><circle cx="110" cy="110" r="13" fill="#E9FCFF"/></svg>
        <span class="label">ASK ABOUT YOUR MONEY</span></div>`}
    <section class="chat" aria-label="Conversation">${msgs}${c.busy ? '<span class="typing pulse">&gt; ATLAS IS THINKING…</span>' : ""}</section>
    <div class="quick">${QUICK.map((q) => `<button class="chip" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>
    <div class="chat-pad"></div>
    <form class="composer" id="cf"><label class="label" for="ci" hidden>Message</label>
      <textarea id="ci" rows="1" placeholder="Ask Atlas…" enterkeyhint="send"></textarea>
      <button class="btn primary" type="submit" ${c.busy ? "disabled" : ""}>SEND</button></form>`;
  const ci = document.getElementById("ci");
  document.getElementById("cf").addEventListener("submit", (e) => { e.preventDefault(); send(ci.value); });
  ci.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(ci.value); } });
  $app.querySelectorAll("[data-q]").forEach((b) => b.addEventListener("click", () => send(b.dataset.q)));
  window.scrollTo(0, document.body.scrollHeight);
  if (c.pending) { const q = c.pending; c.pending = null; send(q); }
}
async function send(text) {
  text = (text || "").trim();
  const c = state.chat;
  if (!text || c.busy) return;
  c.msgs.push({ who: "me", text }); c.busy = true; renderChat();
  try {
    const r = await api("/chat", { method: "POST", body: JSON.stringify({ message: text, session_id: c.sid }) });
    c.sid = r.session_id; c.msgs.push({ who: "atlas", text: r.reply });
  } catch (e) {
    if (e instanceof AuthError) { c.busy = false; return renderLogin(e.message); }
    c.msgs.push({ who: "atlas", text: "Link problem: " + e.message });
  }
  c.busy = false;
  if (location.hash.startsWith("#chat")) renderChat();
}

// ---------- guardrails ----------
function renderGuard() {
  setNav("guard");
  const r = state.home.risk;
  const ks = r.kill_switch;
  const unit = (x) => (x.unit === "CAD" ? ` CAD` : x.unit);
  $app.innerHTML = `
    ${topBar('<span>◆ GUARDRAILS</span>')}
    <section class="panel ${ks.engaged ? "" : "glow"} kill" aria-label="Kill switch">${corners}
      <span class="label">KILL SWITCH · STOPS ALL TRADING AT ONCE</span>
      <span class="state" style="color:${ks.engaged ? "var(--danger)" : "var(--accent)"}">${ks.engaged ? "ENGAGED" : "OFF"}</span>
      ${ks.engaged ? `<p style="margin:0">${esc(ks.reason)} <span class="note-line">by ${esc(ks.by)}</span></p>` : ""}
      ${ks.forced_by_env ? '<p class="note-line">Forced on by ATLAS_KILL_SWITCH on the computer.</p>' : `<button id="ks" class="btn wide ${ks.engaged ? "primary" : "danger"}">${ks.engaged ? "RELEASE" : "ENGAGE KILL SWITCH"}</button>`}
    </section>
    <section class="panel" aria-label="Rules"><span class="label">YOUR RULES · ENFORCED IN CODE</span>
      ${r.rules.map((x) => `<div class="rule"><div><div>${esc(x.label)}</div><div class="d">${x.enabled ? "on" : "OFF"} · set by ${esc(x.set_by)}</div></div><span class="v">${esc(x.value)}${esc(unit(x))}</span></div>`).join("")}
      ${r.fixed_rules.map((x) => `<div class="rule"><div class="d">${esc(x)}</div><span class="v">ALWAYS</span></div>`).join("")}
    </section>
    <p class="note-line">To change a rule, use your computer: atlas rules set KEY VALUE. Only you can loosen a rule.</p>
    <button id="out" class="btn">FORGET PASSCODE ON THIS PHONE</button>`;
  const btn = document.getElementById("ks");
  if (btn) btn.addEventListener("click", async () => {
    const answer = ks.engaged
      ? await ask({ title: "RELEASE KILL SWITCH", body: "Allow trading again? Every trade still needs your approval.", ok: "RELEASE" })
      : await ask({ title: "ENGAGE KILL SWITCH", body: "Stop all trading now and cancel open orders.", ok: "ENGAGE", danger: true, input: "Reason (optional)" });
    if (!answer.ok) return;
    const reason = answer.value;
    try {
      await api("/kill-switch", { method: "POST", body: JSON.stringify({ engaged: !ks.engaged, reason: reason || "engaged from the phone" }) });
      toast(ks.engaged ? "Kill switch released." : "Kill switch ENGAGED. Open orders cancelled.");
      await refreshHome(); renderGuard();
    } catch (e) { if (e instanceof AuthError) return renderLogin(e.message); toast(e.message); }
  });
  document.getElementById("out").addEventListener("click", () => { store.del(TOKEN_KEY); renderLogin(); });
}

// ---------- routing ----------
async function refreshHome() { state.home = await api("/home"); }
async function route() {
  const [view, arg] = (location.hash.replace(/^#/, "") || "home").split("/");
  const param = arg ? decodeURIComponent(arg) : null;
  try {
    if (!state.home) await refreshHome();
    if (view === "board") return renderBoard(param);
    if (view === "chat") return renderChat();
    if (view === "plans") return renderPlans();
    if (view === "plan" && param) return renderPlan(param);
    if (view === "guard") return renderGuard();
    return renderHome();
  } catch (e) {
    if (e instanceof AuthError) return renderLogin(store.get(TOKEN_KEY) ? "That passcode didn't work." : "");
    $app.innerHTML = `${topBar("<span>◉ ATLAS // OFFLINE</span>")}<section class="alert red"><span class="head">■ NO LINK</span>
      <p>Can't reach Atlas on your computer: ${esc(e.message)}. Is <span class="mono">atlas serve --phone</span> running, and is this phone on the same network?</p>
      <div class="btns"><button class="btn ghost-amber" id="retry">RETRY</button></div></section>`;
    document.getElementById("retry").addEventListener("click", () => boot());
  }
}

function bootScreen() {
  try { if (sessionStorage.getItem("atlas.booted")) return null; } catch { return null; }
  try { sessionStorage.setItem("atlas.booted", "1"); } catch { /* ignore */ }
  const el = document.createElement("div");
  el.className = "boot"; el.setAttribute("role", "status"); el.setAttribute("aria-label", "Starting up");
  const lines = ["> INITIALIZING ATLAS", "> LINKING MARKET FEEDS ...... OK", "> LOADING PORTFOLIO ........ OK", "> ARMING GUARDRAILS ........ OK"];
  el.innerHTML = `<svg width="120" height="120" viewBox="0 0 100 100" aria-hidden="true" style="filter:drop-shadow(0 0 12px #3FE0FF)">
    <circle cx="50" cy="50" r="47" fill="none" stroke="#3FE0FF" stroke-width="1.5" stroke-dasharray="18 6" class="spin"/>
    <circle cx="50" cy="50" r="38" fill="none" stroke="#2F7BFF" stroke-dasharray="2 3" class="spinr"/>
    <circle cx="50" cy="50" r="15" fill="#3FE0FF" class="pulse"/><circle cx="50" cy="50" r="6" fill="#E9FCFF"/></svg>
    <div class="lines">${lines.map((l, i) => `<span style="animation-delay:${0.1 + i * 0.22}s">${esc(l)}</span>`).join("")}</div>`;
  document.body.appendChild(el);
  return el;
}

async function boot(fromLogin = false) {
  // Passcode can arrive in the link (#token=...) printed by `atlas serve --phone`.
  const m = /[#&]token=([^&]+)/.exec(location.hash);
  if (m) { store.set(TOKEN_KEY, decodeURIComponent(m[1])); history.replaceState(null, "", "/#home"); }
  const splash = fromLogin ? null : bootScreen();
  const started = Date.now();
  try {
    state.session = await api("/session");
    state.home = null;
    await route();
  } catch (e) {
    if (e instanceof AuthError) renderLogin(fromLogin ? "That passcode didn't work." : "");
    else await route();
  }
  if (splash) setTimeout(() => { splash.classList.add("out"); setTimeout(() => splash.remove(), 400); }, Math.max(0, 1100 - (Date.now() - started)));
}

window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", async () => {
  if (document.visibilityState === "visible" && state.home && /^#?(home)?$/.test(location.hash.replace("#", ""))) {
    try { await refreshHome(); renderHome(); } catch { /* shown on next navigation */ }
  }
});
setInterval(async () => {
  if (document.visibilityState !== "visible" || !state.home) return;
  const v = location.hash.replace("#", "") || "home";
  if (v === "home") { try { await refreshHome(); renderHome(); } catch { /* ignore */ } }
}, 60000);
try {
  if ("serviceWorker" in navigator && window.isSecureContext && !window.ATLAS_DEMO) navigator.serviceWorker.register("/sw.js").catch(() => {});
} catch { /* not available in this view */ }
boot();
})();
