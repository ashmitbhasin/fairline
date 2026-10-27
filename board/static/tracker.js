/* Fairline Tracker: what the code produces. The paper record from the recorded tape, the recorders
   right now, and history by day and by game. Reads /api/tracker (board/tracker.py); nothing on this
   page is entered by hand. */
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const TZ = "America/New_York";
  const POLL_MS = 15_000, POLL_BUSY_MS = 3_000;
  const SILENT_MS = 3 * 60_000;
  const MINUS = "−";
  const NS = "http://www.w3.org/2000/svg";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const store = {
    get(k, d) { try { const v = localStorage.getItem("fairline.tk." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("fairline.tk." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  // ---------- dates ----------
  const fYMD = new Intl.DateTimeFormat("en-CA", { timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit" });
  const fMD = new Intl.DateTimeFormat("en-US", { timeZone: TZ, month: "short", day: "numeric" });
  const fWD = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short" });
  const fHM = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
  const fOff = new Intl.DateTimeFormat("en-US", { timeZone: TZ, timeZoneName: "shortOffset" });
  const etDay = (ms) => fYMD.format(ms);
  const dayUTC = (s) => { const [y, m, d] = s.split("-").map(Number); return Date.UTC(y, m - 1, d); };
  const etMidnight = (s) => {
    const noon = dayUTC(s) + 12 * 3_600_000;
    const off = (fOff.formatToParts(noon).find((p) => p.type === "timeZoneName") || {}).value || "GMT-5";
    const h = Number((off.match(/GMT([+-]\d+)/) || [0, -5])[1]);
    return dayUTC(s) - h * 3_600_000;
  };
  const dayName = (s) => { const ms = etMidnight(s) + 12 * 3_600_000; return `${fWD.format(ms)} ${fMD.format(ms)}`; };
  const kickoff = (ms) => `${fWD.format(ms)} ${fMD.format(ms)}, ${fHM.format(ms)}`;
  const when = (ms) => `${fMD.format(ms)} ${fHM.format(ms)}`;
  const dur = (ms) => {
    const m = Math.max(0, Math.round(ms / 60_000));
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  };
  const ago = (ms, now) => {
    if (!ms) return "never";
    const s = Math.max(0, Math.round((now - ms) / 1000));
    if (s < 60) return `${s}s ago`;
    return `${dur(s * 1000)} ago`;
  };

  // ---------- numbers ----------
  const signOf = (x, digits) => { const r = Number(x.toFixed(digits)); return r > 0 ? "+" : r < 0 ? MINUS : ""; };
  const money = (n, signed = true) => {
    if (n == null) return "—";
    const body = "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (signed ? signOf(n, 2) : n < 0 ? MINUS : "") + body;
  };
  const cents = (x, d = 1, signed = true) => (x == null ? "—" : (signed ? signOf(x * 100, d) : x < 0 ? MINUS : "") + Math.abs(x * 100).toFixed(d) + "¢");
  const px = (x) => (x == null ? "—" : Math.round(x * 100) + "¢");
  const fairc = (x) => (x == null ? "—" : (x * 100).toFixed(1) + "¢");
  const pct = (x, d = 0, signed = false) => (x == null ? "—" : (signed ? signOf(x * 100, d) : "") + Math.abs(x * 100).toFixed(d) + "%");
  const int = (n) => (n == null ? "—" : Math.round(n).toLocaleString("en-US"));
  const qty = (n) => (n == null ? "—" : Number.isInteger(n) ? String(n) : n.toFixed(2));
  const bytes = (n) => {
    if (n == null) return "—";
    if (n >= 1e9) return (n / 1e9).toFixed(2) + " GB";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e8 ? 0 : 1) + " MB";
    if (n >= 1e3) return Math.round(n / 1e3) + " KB";
    return n + " B";
  };
  const tone = (x) => (x == null ? "" : x > 1e-9 ? "pos" : x < -1e-9 ? "neg" : "");
  const record = (t) => `${t.won}–${t.lost}${t.tied ? "–" + t.tied : ""}`;
  const minus = (s) => String(s).replace(/-/g, MINUS);

  // ---------- rules ----------
  const cnum = (p) => String(Number((p * 100).toFixed(1)));
  const vname = (v) => (v.rule === "take" ? `Take ≥ ${cnum(v.param)}¢` : `Quote ± ${cnum(v.param)}¢`);
  const vdesc = (v) => (v.rule === "take"
    ? `Buy at Kalshi's price when Pinnacle's fair value beats it by ${cnum(v.param)}¢ or more after the taker fee. One bet per market.`
    : `Rest a bid ${cnum(v.param)}¢ under fair and an offer ${cnum(v.param)}¢ over it, never crossing the book. Fills only when a trade prints through the quote; one fill per side per market.`);
  const KIND = { winner: "Winner", spread: "Spread", total: "Total" };
  const SERVICE = {
    "kalshi-live": "Kalshi live", "kalshi-backup": "Kalshi backup", odds: "Pinnacle odds", board: "Indexer", "board-web": "Board server", pull: "Tape copy",
  };
  const SOURCE = {
    "kalshi-live": "WebSocket, every book change", "kalshi-backup": "REST snapshots", odds: "The Odds API", board: "tape → board.db", "board-web": "this page",
    pull: "EC2 → this Mac",
  };

  // ---------- state ----------
  const K = { view: "markets", data: null, err: null, variant: store.get("variant", null), allBets: false, timer: null, loading: false, watch: false, computed: null };
  const D = () => K.data || {};
  const P = () => D().paper || {};
  const T = () => D().tape || {};
  const N = () => D().now || {};

  function variants() { return P().variants || []; }
  function current() {
    const vs = variants();
    return vs.find((v) => v.id === K.variant) || vs.find((v) => v.primary && v.rule === "take") || vs[0];
  }
  function betsOf(v) { return (P().bets || []).filter((b) => b.variant === v.id); }

  // ---------- network ----------
  async function load() {
    if (K.loading) return;
    K.loading = true;
    try {
      const r = await fetch("/api/tracker", { cache: "no-store" });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(body.detail || r.statusText);
      K.data = body;
      K.err = null;
    } catch (e) {
      K.err = e.message || String(e);
    } finally {
      K.loading = false;
    }
    if (K.view === "tracker") {
      if (K.data && K.computed === D().computed_ms && $("tk-now")) renderLive();
      else render();
    }
    schedule();
  }

  function schedule() {
    clearTimeout(K.timer);
    if (K.view !== "tracker") return;
    if (K.watch && !D().refreshing) K.watch = false;
    K.timer = setTimeout(() => { if (!document.hidden) load(); else schedule(); }, D().refreshing || K.watch ? POLL_BUSY_MS : POLL_MS);
  }

  async function refreshNow() {
    try {
      const r = await fetch("/api/tracker/refresh", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", "X-Fairline": "1" }, body: "{}" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      K.watch = true;
      if (K.data) K.data.refreshing = true;
      renderLine();
      schedule();
    } catch (e) {
      K.err = `Couldn't start a recompute: ${e.message}`;
      renderLine();
    }
  }

  // ---------- render ----------
  function render() {
    const root = $("tk");
    if (!K.data) {
      root.innerHTML = `<p class="tk-empty">${K.err ? "Couldn't load the tracker: " + esc(K.err) : "Loading…"}</p>`;
      return;
    }
    K.computed = D().computed_ms;
    root.innerHTML = `<div id="tk-line"></div>${recordHTML()}<section class="tk-sec" id="tk-now"></section>${historyHTML()}`;
    renderLive();
    drawCharts();
  }

  function renderLive() {
    renderLine();
    const now = $("tk-now");
    if (now) now.innerHTML = nowHTML();
  }

  function renderLine() {
    const box = $("tk-line");
    if (!box) return;
    const now = D().now_ms || Date.now();
    const n = N();
    const live = (n.services || []).find((s) => s.name === "kalshi-live");
    const ws = (n.health || []).find((h) => h.stream === "kalshi_ws");
    const lastSeen = Math.max(n.recorder?.ts_ms || 0, ws?.last_ms || 0);
    let state;
    if (live && live.running === null) state = `<span class="off">No report from the EC2 recorder ${n.remote?.reported_ms ? `since ${esc(when(n.remote.reported_ms))}` : "yet"}</span>`;
    else if (!live || !live.running) state = `<span class="off">Recorder stopped</span>`;
    else if (now - lastSeen > SILENT_MS) state = `<span class="off">Recorder silent since ${esc(when(lastSeen))}</span>`;
    else state = `<span><span class="on">Recording</span> · ${int(n.recorder?.msgs)} Kalshi messages in the last minute</span>`;
    const next = (n.upcoming || []).find((g) => g.kickoff_ms > now);
    const nextHTML = next ? `<span>Next: <b>${esc(next.label)}</b> ${esc(fWD.format(next.kickoff_ms))} ${esc(fHM.format(next.kickoff_ms))}, in ${dur(next.kickoff_ms - now)}</span>` : "";
    const busy = D().refreshing || K.watch;
    const upd = busy ? "Recomputing…" : `Numbers from ${ago(D().computed_ms, Date.now())}`;
    box.innerHTML = `<div class="tk-line">${state}${nextHTML}<span>${upd} · <button type="button" class="linkish" data-act="refresh" ${busy ? "disabled" : ""}>Recompute</button></span>
      ${K.err ? `<span class="neg">${esc(K.err)}</span>` : ""}${D().error ? `<span class="neg">Last recompute failed: ${esc(D().error)}</span>` : ""}
      <nav aria-label="Sections"><a href="#tk-record" data-jump="tk-record">Record</a><a href="#tk-now" data-jump="tk-now">Now</a><a href="#tk-history" data-jump="tk-history">History</a></nav></div>`;
  }

  // ---------- record ----------
  function recordHTML() {
    const p = P();
    const head = `<section class="tk-sec" id="tk-record"><h2>Record</h2>
      <p class="dek">Paper bets placed by the code's rules on the recorded tape, before kickoff, and scored when Kalshi settled the market.
      ${p.params ? `${p.params.size} contracts a bet (fewer when the book showed less), fees included.` : ""}
      The rules act only on a Pinnacle price received in the last ${p.params ? p.params.trade_fresh_min : 5} minutes and refreshed when it arrived,
      and skip minutes when the recorder was blind or the recorded book was crossed.</p>`;
    if (!p.available) return head + `<p class="tk-empty">${esc(p.reason || "Replaying the tape…")}</p></section>`;
    const v = current();
    const bets = betsOf(v).slice().sort((a, b) => b.ts_ms - a.ts_ms);
    return head + headlineHTML(v) + ladderHTML(v) + `
      <div class="tk-cols">
        <div><h3 class="tk-h">Running P&amp;L <span>${esc(vname(v))}, settled bets, against what they were worth when placed</span></h3><div class="tk-chart" id="ch-pnl"></div></div>
        <div><h3 class="tk-h">Splits <span>${esc(vname(v))}</span></h3>${splitsHTML(v)}</div>
      </div>
      <h3 class="tk-h">Bets <span>${esc(vname(v))} · ${bets.length} placed${v.pending ? `, ${v.pending} waiting on a result` : ""}</span></h3>
      ${betsHTML(bets)}
      ${p.settlement_error ? `<p class="tk-note warn">${esc(p.settlement_error)}</p>` : ""}
    </section>`;
  }

  function headlineHTML(v) {
    const ci = v.clv_ci != null ? `<small>±${cents(v.clv_ci, 1, false)} (95%)</small>` : "<small>&nbsp;</small>";
    const sample = v.settled === 0 ? "No settled bets yet under this rule."
      : v.settled < 30 ? `${v.settled} settled bet${v.settled === 1 ? "" : "s"}. A record this short is mostly luck; closing-line value says more, sooner.`
        : `${v.settled} settled bets.`;
    const form = v.form && v.form.length ? `<div class="form"><span>Last ${v.form.length}</span>${v.form.map((f) => `<i class="${f}">${f}</i>`).join("")}</div>` : "";
    return `<div class="vname"><b>${esc(vname(v))}</b><span>${esc(vdesc(v))}</span></div>
      <div class="xscroll"><table class="score" aria-label="Record for ${esc(vname(v))}">
        <tr><th>Record</th><th>P&amp;L</th><th>ROI</th><th>CLV a contract</th><th>Beat the close</th><th>Worth when placed</th><th>Waiting</th></tr>
        <tr><td class="big">${record(v)}<small>${v.win_pct == null ? "&nbsp;" : pct(v.win_pct, 1) + " won"}</small></td>
          <td class="big ${v.settled ? tone(v.pnl) : ""}">${money(v.settled ? v.pnl : null)}<small>${v.settled ? `on ${money(v.staked, false)} staked` : "&nbsp;"}</small></td>
          <td class="${tone(v.roi)}">${pct(v.roi, 1, true)}<small>&nbsp;</small></td>
          <td class="${tone(v.clv_mean)}">${cents(v.clv_mean, 2)}${ci}</td>
          <td>${pct(v.beat_close)}<small>${v.clv_n ? `of ${v.clv_n}` : "&nbsp;"}</small></td>
          <td class="${v.settled ? tone(v.ev) : ""}">${money(v.settled ? v.ev : null)}<small>edge at entry × size</small></td>
          <td>${int(v.pending)}<small>${v.pending ? "before or during a game" : "&nbsp;"}</small></td></tr>
      </table></div>${form}<p class="tk-note">${esc(sample)}</p>`;
  }

  function ladderHTML(sel) {
    const rows = (rule) => variants().filter((v) => v.rule === rule).map((v) => `
      <tr data-variant="${esc(v.id)}" tabindex="0" class="${v.id === sel.id ? "sel" : ""}" aria-selected="${v.id === sel.id}">
        <td class="l">${esc(vname(v))}${v.primary ? ' <span class="dim">· headline</span>' : ""}</td>
        <td>${int(v.bets)}</td><td>${int(v.settled)}</td><td>${record(v)}</td><td>${pct(v.win_pct)}</td>
        <td class="${v.settled ? tone(v.pnl) : ""}">${v.settled ? money(v.pnl) : "—"}</td><td class="${tone(v.roi)}">${pct(v.roi, 1, true)}</td>
        <td class="${tone(v.clv_mean)}">${cents(v.clv_mean, 2)}</td><td class="dim">${v.clv_ci == null ? "—" : "±" + cents(v.clv_ci, 2, false)}</td>
        <td>${pct(v.beat_close)}</td></tr>`).join("");
    return `<h3 class="tk-h">Every rule <span>The thresholds were fixed before any results came in. Pick one to see its bets.</span></h3>
      <div class="xscroll"><table class="st rules"><thead><tr><th class="l">Rule</th><th>Bets</th><th>Settled</th><th>W–L</th><th>Won</th>
        <th>P&amp;L</th><th>ROI</th><th>CLV</th><th>95% ±</th><th>Beat close</th></tr></thead>
      <tbody><tr class="grp"><td colspan="10">Take the gap</td></tr>${rows("take")}<tr class="grp"><td colspan="10">Quote around fair</td></tr>${rows("quote")}</tbody></table></div>`;
  }

  function splitsHTML(v) {
    const splits = v.splits || {};
    const body = Object.values(splits).filter((s) => s.rows.length).map((s) => `<tr class="grp"><td colspan="5">${esc(s.title)}</td></tr>` + s.rows.map((r) => `
      <tr><td class="l">${esc(r.label)}</td><td>${int(r.bets)}</td><td>${record(r)}</td>
        <td class="${r.settled ? tone(r.pnl) : ""}">${r.settled ? money(r.pnl) : "—"}</td><td class="${tone(r.clv_mean)}">${cents(r.clv_mean, 2)}</td></tr>`).join("")).join("");
    if (!body) return `<p class="tk-note">No bets under this rule yet.</p>`;
    return `<table class="st"><thead><tr><th class="l"></th><th>Bets</th><th>W–L</th><th>P&amp;L</th><th>CLV</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  function resultHTML(b, now) {
    if (b.status === "won") return `<span class="pos">Won</span>`;
    if (b.status === "lost") return `<span class="neg">Lost</span>`;
    if (b.status === "tie") return `<span>Tie, paid 50¢</span>`;
    if (b.status === "void") return `<span class="dim">Void</span>`;
    if (b.status === "open") return `<span class="dim">Kicks off in ${dur(b.kickoff_ms - now)}</span>`;
    return `<span class="dim">Waiting on Kalshi</span>`;
  }

  function betsHTML(bets) {
    if (!bets.length) return `<p class="tk-note">This rule hasn't placed a bet yet.</p>`;
    const now = D().now_ms || Date.now();
    const games = Object.fromEntries((P().games || []).map((g) => [g.key, g]));
    const shown = K.allBets ? bets : bets.slice(0, 25);
    const rows = shown.map((b) => `<tr>
        <td class="l">${esc(when(b.ts_ms))}<span class="sub">${dur(b.kickoff_ms - b.ts_ms)} before kickoff</span></td>
        <td class="l">${esc(games[b.game_key]?.label || b.game_key)}</td>
        <td class="l">${esc(minus(b.label))} <span class="dim">${esc(KIND[b.kind] || b.kind)}</span></td>
        <td class="l">${b.side.toUpperCase()}</td><td>${px(b.price)}</td><td>${qty(b.qty)}</td><td>${fairc(b.fair)}</td>
        <td class="${tone(b.edge)}">${cents(b.edge, 2)}</td><td>${fairc(b.close)}</td><td class="${tone(b.clv)}">${cents(b.clv, 2)}</td>
        <td class="l">${resultHTML(b, now)}</td><td class="${tone(b.pnl)}">${b.pnl == null ? "—" : money(b.pnl)}</td></tr>`).join("");
    const more = bets.length > 25 ? `<p class="tk-note"><button type="button" class="linkish" data-act="all-bets">${K.allBets ? "Show the latest 25" : `Show all ${bets.length}`}</button></p>` : "";
    return `<div class="xscroll"><table class="st"><thead><tr><th class="l">Placed (ET)</th><th class="l">Game</th><th class="l">Market</th><th class="l">Bought</th>
      <th>Price</th><th>Qty</th><th title="Pinnacle's fair value for the side bought, when the bet was placed">Fair then</th>
      <th title="Fair then minus price paid, fee included">Edge</th><th title="Pinnacle's last fair value before kickoff">Close</th>
      <th title="Close minus price paid, fee included">CLV</th><th class="l">Result</th><th>P&amp;L</th></tr></thead><tbody>${rows}</tbody></table></div>${more}`;
  }

  // ---------- now ----------
  function nowHTML() {
    const n = N();
    const now = D().now_ms || Date.now();
    const health = Object.fromEntries((n.health || []).map((h) => [h.stream, h]));
    const burn = T().odds_burn || {};
    const services = (n.services || []).map((s) => {
      let state = s.running === null ? `<span class="state-warn">No report</span>` : s.running ? `<span class="state-ok">Running</span>` : `<span class="state-bad">Stopped</span>`;
      if (s.running && s.stream && s.last_data_ms && now - s.last_data_ms > (s.stream === "odds" ? 40 * 60_000 : SILENT_MS)) state = `<span class="state-warn">Silent</span>`;
      let detail = "";
      if (s.name === "kalshi-live" && n.recorder && now - n.recorder.ts_ms < SILENT_MS) {
        detail = `${int(n.recorder.msgs)} messages, ${int(n.recorder.trades)} trades, ${int(n.recorder.gaps)} gaps last minute`;
        if (health.kalshi_ws?.disconnects_today) detail += ` · ${int(health.kalshi_ws.disconnects_today)} reconnects today`;
      } else if (s.running === null) detail = n.remote?.reported_ms ? `last report ${ago(n.remote.reported_ms, now)}` : "no report yet";
      else if (s.name === "kalshi-backup" && n.backup) detail = `${int(n.backup.markets)} markets, ${int(n.backup.changed)} changed books last cycle`;
      else if (s.name === "kalshi-backup" && health.kalshi_rest) detail = `${int(health.kalshi_rest.per_min)} books a minute`;
      else if (s.name === "odds" && n.odds) detail = `${int(n.odds.remaining)} credits left${burn.available ? `, ~${int(burn.per_day)} a day` : ""}`;
      else if (s.name === "board") detail = `board.db ${bytes(T().db_bytes)}`;
      else if (s.name === "board-web") detail = "serving this page";
      else if (s.name === "pull" && s.last_pull) detail = s.last_pull.ok ? `tape copied ${ago(s.last_pull.ts_ms, now)}` : `last copy failed ${ago(s.last_pull.ts_ms, now)}: ${s.last_pull.error || ""}`;
      return `<tr><td class="l">${esc(SERVICE[s.name] || s.name)}<span class="sub">${esc(SOURCE[s.name] || "")}${s.where ? ` · on ${esc(s.where)}` : ""}</span></td><td class="l">${state}</td>
        <td>${s.stream ? ago(s.last_data_ms, now) : "—"}</td><td>${s.running && s.started_ms ? dur(now - s.started_ms) : "—"}</td><td class="l wrap">${esc(detail)}</td></tr>`;
    }).join("");
    const games = (n.upcoming || []).map((g) => {
      const started = g.kickoff_ms <= now;
      return `<tr><td class="l">${esc(kickoff(g.kickoff_ms))}</td><td class="l">${esc(g.label)}</td><td>${int(g.markets)}</td><td>${int(g.matched)}</td>
        <td>${int(g.volume_today)}</td><td>${started ? '<span class="pos">In play</span>' : dur(g.kickoff_ms - now)}</td></tr>`;
    }).join("");
    const e = n.edges || { rows: [] };
    const edgeRows = (e.rows || []).map((r) => `<tr><td class="l">${esc(minus(r.label))} <span class="dim">${esc(KIND[r.kind] || r.kind)}</span></td><td class="l">${esc(r.game)}</td>
      <td>${dur(r.kickoff_ms - now)}</td><td>${px(r.bid)}</td><td>${px(r.ask)}</td><td>${fairc(r.fair)}${r.pulled ? `<span class="sub warn">line not refreshed</span>` : ""}</td>
      <td class="${tone(r.gap)}">${cents(r.gap, 1)}</td>
      <td class="l">${r.side.toUpperCase()} at ${px(r.price)}</td><td class="${!e.stale && !r.pulled && r.edge >= e.threshold - 1e-9 ? "pos" : "dim"}">${cents(r.edge, 2)}</td></tr>`).join("");
    const disk = n.disk ? `${bytes(n.disk.free)} free on ${n.disk.where || "this Mac"}` : "";
    const dek = n.remote
      ? `The recorders run on EC2 and report every minute${n.remote.reported_ms ? ` (last ${ago(n.remote.reported_ms, now)})` : ""}. The board here reads tape copied down every 10 minutes, so it runs up to an hour behind.`
      : "Live from the recorders and the board, refreshed every 15 seconds.";
    return `<h2>Now</h2><p class="dek">${dek}</p>
      <div class="tk-cols even">
        <div><h3 class="tk-h">Recorders <span>${esc(disk)}</span></h3><div class="xscroll"><table class="st"><thead><tr><th class="l">Service</th><th class="l">State</th><th>Last data</th><th>Up</th><th class="l"></th></tr></thead><tbody>${services}</tbody></table></div></div>
        <div><h3 class="tk-h">Next games</h3>${games ? `<div class="xscroll"><table class="st"><thead><tr><th class="l">Kickoff (ET)</th><th class="l">Game</th><th>Markets</th><th title="Markets with an identical Pinnacle line">Matched</th><th title="Contracts traded today across the game's markets">Volume today</th><th>Starts in</th></tr></thead><tbody>${games}</tbody></table></div>` : `<p class="tk-note">No games on the schedule.</p>`}</div>
      </div>
      <h3 class="tk-h">Edges right now <span>${int(e.matched)} markets before kickoff with a Pinnacle line; ${int(e.takeable)} worth taking at ${cnum(e.threshold || 0.02)}¢. The better side of each, after the taker fee.${e.stale ? ` Pinnacle was last polled ${ago(e.poll_ms, now)}; the rules wait for a price under ${e.fresh_min} minutes old.` : ""}</span></h3>
      ${edgeRows ? `<div class="xscroll"><table class="st"><thead><tr><th class="l">Market</th><th class="l">Game</th><th>Kickoff in</th><th>Bid</th><th>Ask</th><th>Fair</th><th title="Fair minus Kalshi mid">Gap</th><th class="l">Better side</th><th>Edge after fee</th></tr></thead><tbody>${edgeRows}</tbody></table></div>` : `<p class="tk-note">No markets with a Pinnacle line before kickoff right now.</p>`}`;
  }

  // ---------- history ----------
  function historyHTML() {
    const t = T();
    const head = `<section class="tk-sec" id="tk-history"><h2>History</h2><p class="dek">Everything recorded so far, by day and by game.</p>`;
    if (!t.per_day) return head + `<p class="tk-empty">Scanning the tape…</p></section>`;
    const up = t.uptime || {};
    const through = t.known_until_ms && t.scanned_ms - t.known_until_ms > 60_000 ? `, measured through ${esc(when(t.known_until_ms))} (the last copy from EC2)` : "";
    const upLine = up.available ? `${pct(up.uptime, 1)} of the last 7 days, ${pct(up.game_uptime, 1)} inside game windows (4 h before kickoff to the final)${through}` : esc(up.reason || "");
    const v = current();
    return head + `
      <h3 class="tk-h">When the recorder could see <span>${upLine}</span></h3>
      <div class="tk-chart" id="ch-uptime"></div>
      <p class="tk-note">Blue is recording. Gaps are a stopped process, a dropped connection or a sleeping Mac; amber marks game windows.</p>
      <h3 class="tk-h">By day <span>Eastern time.${v ? ` Bets and P&amp;L follow ${esc(vname(v))}.` : ""}</span></h3>
      ${daysHTML()}
      ${gapHTML(P().g2)}
      <h3 class="tk-h">Games <span>Every game with a Pinnacle line. Close: the home team's win price at kickoff on Kalshi and at Pinnacle.</span></h3>
      ${gamesHTML()}
      <h3 class="tk-h">Measured against the bars set on Oct 6 <span>From research/registry/. Each is read on its date, not before.</span></h3>
      ${barsHTML()}
    </section>`;
  }

  function daysHTML() {
    const t = T();
    const v = current();
    const act = (t.activity || {}).days || {};
    const gap = Object.fromEntries(((P().g2 || {}).days || []).map((d) => [d.day, d]));
    const bets = v ? betsOf(v) : [];
    const books = t.books || {};
    const all = t.per_day || [];
    const first = all.findIndex((d) => d.kalshi_ws.records || d.kalshi_rest.records || d.odds.records);
    const days = (first < 0 ? [] : all.slice(first)).reverse();
    const maxMsgs = Math.max(1, ...days.map((d) => d.kalshi_ws.records));
    const rows = days.map((d) => {
      const placed = bets.filter((b) => etDay(b.ts_ms) === d.day);
      const settled = bets.filter((b) => b.pnl != null && b.settled_ms && etDay(b.settled_ms) === d.day);
      const pnl = settled.reduce((s, b) => s + b.pnl, 0);
      const upd = (t.uptime_days || {})[d.day];
      const a = act[d.day] || {};
      const g = gap[d.day];
      const total = d.kalshi_ws.bytes + d.kalshi_rest.bytes + d.odds.bytes;
      const w = (100 * d.kalshi_ws.records) / maxMsgs;
      const bk = books[d.day];
      const crossed = bk && bk.books ? bk.crossed / bk.books : null;
      return `<tr><td class="l">${esc(dayName(d.day))}</td><td>${upd == null ? '<span class="dim">—</span>' : pct(upd, 0)}</td>
        <td><div class="bar-cell">${int(d.kalshi_ws.records)}<span class="track"><i style="width:${w.toFixed(1)}%"></i></span></div></td>
        <td>${int(a.trades)}</td><td>${int(a.contracts)}</td><td>${int(d.odds.records)}</td><td>${bytes(total)}</td>
        <td class="${crossed != null && crossed >= 0.01 ? "warn" : ""}">${crossed == null ? "—" : pct(crossed, 1)}</td>
        <td>${g ? (g.qualifying / 60).toFixed(1) : "—"}</td><td>${placed.length || "—"}</td>
        <td class="${tone(settled.length ? pnl : null)}">${settled.length ? money(pnl) : "—"}</td></tr>`;
    }).join("");
    return `<div class="xscroll"><table class="st"><thead><tr><th class="l">Day</th><th>Uptime</th><th>Kalshi messages</th><th title="Public trades on recorded markets">Trades</th>
      <th>Contracts</th><th>Odds polls</th><th>Data</th><th title="Share of recorded book states with bid at or above ask: a missed update">Crossed books</th>
      <th title="Pregame side-hours with Pinnacle 2¢ or more beyond half the Kalshi spread">Gap hours ≥ 2¢</th>
      <th>Bets placed</th><th>P&amp;L settled</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="tk-note">Uptime starts on the first day the recorder logged a heartbeat; before that a quiet market and a sleeping Mac look the same.
      A crossed book (bid at or above ask) can't exist on the exchange, so each one is an update the tape missed; the paper rules skip them.</p>`;
  }

  function gapHTML(g) {
    if (!g || !g.available) return `<h3 class="tk-h">Pregame gap, Pinnacle against Kalshi</h3><p class="tk-note">${esc((g && g.reason) || "Not measured yet.")}</p>`;
    const kinds = Object.entries(g.by_kind || {}).map(([k, x]) => `${KIND[k] || k}: ${(x.qualifying / 60).toFixed(1)} h of ${int(x.minutes / 60)} h`).join(" · ");
    return `<h3 class="tk-h">Pregame gap, Pinnacle against Kalshi <span>${int(g.markets)} markets, ${int(g.side_minutes / 60)} side-hours sampled once a minute</span></h3>
      <div class="tk-cols">
        <div class="tk-chart" id="ch-gap"></div>
        <div><table class="st"><tbody>
          <tr><td class="l">Median distance from fair to the Kalshi mid</td><td>${cents(g.median_gap, 2, false)}</td></tr>
          <tr><td class="l">90th percentile</td><td>${cents(g.p90_gap, 2, false)}</td></tr>
          <tr><td class="l">Mean</td><td>${cents(g.mean_abs_gap, 2, false)}</td></tr>
          <tr><td class="l">Side-hours 2¢ or more beyond half the spread</td><td>${g.qualifying_hours.toFixed(1)} h</td></tr>
          <tr><td class="l">… in the last ${g.recent_days} days</td><td>${g.recent_hours.toFixed(1)} h</td></tr>
        </tbody></table><p class="tk-note">${esc(kinds)}</p></div>
      </div>`;
  }

  function gamesHTML() {
    const games = P().games || [];
    if (!games.length) return `<p class="tk-note">No games with a Pinnacle line yet.</p>`;
    const v = current();
    const act = (T().activity || {}).games || {};
    const now = D().now_ms || Date.now();
    const rows = games.map((g) => {
      const a = act[g.key] || {};
      const r = (v && g.variants[v.id]) || { bets: 0, won: 0, lost: 0, tied: 0, settled: 0, pnl: 0, pending: 0 };
      const up = g.kickoff_ms > now;
      const diff = g.close ? g.close.mid - g.close.fair : null;
      return `<tr class="${up ? "upcoming" : ""}"><td class="l">${esc(kickoff(g.kickoff_ms))}</td><td class="l">${esc(g.label)}</td>
        <td>${int(a.markets)}</td><td>${int(g.matched)}</td><td>${int(a.contracts)}</td>
        <td>${up && g.close ? '<span class="dim">now</span> ' : ""}${g.close ? fairc(g.close.mid) : "—"}</td><td>${g.close ? fairc(g.close.fair) : "—"}</td>
        <td class="${!up && diff != null && Math.abs(diff) >= 0.02 ? "warn" : ""}">${cents(diff, 1)}</td>
        <td>${r.bets || "—"}</td><td>${r.settled ? record(r) : r.pending ? `<span class="dim">${r.pending} waiting</span>` : "—"}</td>
        <td class="${r.settled ? tone(r.pnl) : ""}">${r.settled ? money(r.pnl) : "—"}</td></tr>`;
    }).join("");
    return `<div class="xscroll"><table class="st"><thead><tr><th class="l">Kickoff (ET)</th><th class="l">Game</th><th>Markets</th>
      <th title="Markets with an identical Pinnacle line">Matched</th><th title="Contracts traded on Kalshi across the game's markets, as recorded">Contracts</th>
      <th>Kalshi close</th><th>Pinnacle close</th><th title="Kalshi minus Pinnacle, home team win">Diff</th>
      <th>Bets</th><th>W–L</th><th>P&amp;L</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function barsHTML() {
    const t = T();
    const up = t.uptime || {};
    const map = t.mapping || {};
    const g = P().g2 || {};
    const q = variants().find((v) => v.rule === "quote" && v.primary);
    const qn = q ? esc(vname(q)) : "Quote ± 2¢";
    const row = (what, bar, now, met, read) => `<tr><td class="l">${what}</td><td class="l">${bar}</td>
      <td class="${met ? "pos" : ""}">${now}</td><td class="l">${met == null ? '<span class="dim">—</span>' : met ? "Met so far" : '<span class="dim">Short</span>'}</td><td class="l">${read}</td></tr>`;
    const rows = [
      row("Recorder uptime, last 7 days", "99% or more", up.available ? pct(up.uptime, 1) : "—", up.available ? up.uptime >= 0.99 : null, "Oct 26"),
      row("Uptime inside game windows", "99% or more", up.game_uptime == null ? "—" : pct(up.game_uptime, 1), up.game_uptime == null ? null : up.game_uptime >= 0.99, "Oct 26"),
      row("Games mapped to a Pinnacle event", "98% or more", map.share == null ? "—" : `${pct(map.share, 0)} of ${int(map.games)}`, map.share == null ? null : map.share >= 0.98, "Oct 26"),
      row("Side-hours ≥ 2¢ beyond half the spread, last 7 days", "several dozen a week", g.available ? `${g.recent_hours.toFixed(1)} h` : "—", g.available ? g.recent_hours >= 36 : null, "Nov 2"),
      row(`${qn}: fills`, "300 or more", q ? int(q.bets) : "—", q ? q.bets >= 300 : null, "Nov 16"),
      row(`${qn}: mean CLV`, "above 0, 95% interval clear of 0", q && q.clv_mean != null ? `${cents(q.clv_mean, 2)}${q.clv_ci != null ? " ±" + cents(q.clv_ci, 2, false) : ""}` : "—",
        q && q.clv_mean != null && q.clv_ci != null ? q.clv_mean - q.clv_ci > 0 : null, "Nov 16"),
    ].join("");
    return `<div class="xscroll"><table class="st"><thead><tr><th class="l">Measure</th><th class="l">Bar</th><th>Now</th><th class="l"></th><th class="l">Read on</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  // ---------- charts ----------
  function svg(box, w, h) {
    box.textContent = "";
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", `0 0 ${w} ${h}`);
    s.setAttribute("height", h);
    box.appendChild(s);
    return s;
  }
  function el(parent, tag, attrs, text) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text != null) e.textContent = text;
    parent.appendChild(e);
    return e;
  }
  function niceStep(span, n) {
    const raw = span / n;
    const mag = 10 ** Math.floor(Math.log10(raw || 1));
    return [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  }

  function drawCharts() {
    const pnl = $("ch-pnl"), upt = $("ch-uptime"), gap = $("ch-gap");
    if (pnl && current()) drawPnl(pnl, current());
    if (upt) drawUptime(upt);
    if (gap && P().g2 && P().g2.available) drawGap(gap, P().g2);
  }

  function drawPnl(box, v) {
    const W = Math.max(320, box.clientWidth), H = 220, L = 52, R = 104, Tp = 12, B = 24;
    const s = svg(box, W, H);
    const pts = v.curve || [];
    if (!pts.length) {
      el(s, "text", { x: 0, y: 40, class: "msg" }, "Nothing settled yet under this rule.");
      return;
    }
    const placed = betsOf(v).filter((b) => b.pnl != null).map((b) => b.ts_ms);
    const x0 = Math.min(pts[0][0], ...placed), x1 = Math.max(pts[pts.length - 1][0], x0 + 3_600_000);
    const vals = pts.flatMap((p) => [p[1], p[2]]).concat(0);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (hi - lo < 1) { hi += 1; lo -= 1; }
    const step = niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
    const X = (t) => L + ((t - x0) / (x1 - x0)) * (W - L - R);
    const Y = (y) => Tp + (1 - (y - lo) / (hi - lo)) * (H - Tp - B);
    for (let y = lo; y <= hi + 1e-9; y += step) {
      el(s, "line", { x1: L, x2: W - R, y1: Y(y), y2: Y(y), class: Math.abs(y) < 1e-9 ? "zero" : "grid" });
      el(s, "text", { x: L - 8, y: Y(y) + 3.5, "text-anchor": "end" }, (y < -1e-9 ? MINUS : "") + "$" + Math.abs(y).toFixed(step < 1 ? 2 : 0));
    }
    el(s, "text", { x: X(x0), y: H - 6, "text-anchor": "start" }, fMD.format(x0));
    el(s, "text", { x: X(x1), y: H - 6, "text-anchor": "end" }, fMD.format(x1));
    const days = [];
    const ends = new Set([fMD.format(x0), fMD.format(x1)]);
    for (let t = etMidnight(etDay(x0)) + 86_400_000; t < x1; t += 86_400_000) {
      if (X(t) - X(x0) > 60 && X(x1) - X(t) > 60 && !ends.has(fMD.format(t + 3_600_000))) days.push(t);
    }
    const every = Math.max(1, Math.ceil(days.length / 5));
    days.forEach((t, i) => { if (i % every === 0) el(s, "text", { x: X(t), y: H - 6, "text-anchor": "middle" }, fMD.format(t + 3_600_000)); });
    const path = (idx) => {
      let d = `M${X(x0).toFixed(1)},${Y(0).toFixed(1)}`;
      let last = 0;
      for (const p of pts) { d += `H${X(p[0]).toFixed(1)}V${Y(p[idx]).toFixed(1)}`; last = p[idx]; }
      return { d: d + `H${X(x1).toFixed(1)}`, last };
    };
    const ev = path(2), real = path(1);
    el(s, "path", { d: ev.d, fill: "none", stroke: "var(--muted)", "stroke-width": 1.2, "stroke-dasharray": "4 3" });
    el(s, "path", { d: real.d, fill: "none", stroke: "var(--text)", "stroke-width": 1.6 });
    const settled = betsOf(v).filter((b) => b.pnl != null && b.status !== "void").sort((a, b) => (a.settled_ms || a.kickoff_ms) - (b.settled_ms || b.kickoff_ms) || a.ts_ms - b.ts_ms);
    let cum = 0;
    for (const b of settled) {
      cum += b.pnl;
      const dot = el(s, "circle", { cx: X(b.settled_ms || b.kickoff_ms), cy: Y(cum), r: 3, fill: b.status === "won" ? "var(--up)" : b.status === "lost" ? "var(--down)" : "var(--text-2)" });
      el(dot, "title", {}, `${minus(b.label)} ${b.side.toUpperCase()} at ${px(b.price)}: ${b.status}, ${money(b.pnl)}`);
    }
    const ly = [Y(real.last), Y(ev.last)];
    if (Math.abs(ly[0] - ly[1]) < 13) {
      const mid = (ly[0] + ly[1]) / 2, up = real.last >= ev.last ? -1 : 1;
      ly[0] = mid + 7 * up;
      ly[1] = mid - 7 * up;
    }
    el(s, "text", { x: W - R + 8, y: ly[0] + 3.5, class: "lbl" }, `${money(real.last)} actual`);
    el(s, "text", { x: W - R + 8, y: ly[1] + 3.5 }, `${money(ev.last)} worth`);
  }

  function drawUptime(box) {
    const tl = T().timeline;
    if (!tl) { box.textContent = ""; return; }
    const W = Math.max(360, box.clientWidth), L = 96, R = 52, ROW = 14, GAP = 12, Tp = 18;
    const days = [];
    for (let t = tl.from_ms; t <= tl.to_ms + 86_400_000; t += 3_600_000) {
      const d = etDay(Math.min(t, tl.to_ms));
      if (!days.includes(d)) days.push(d);
    }
    const H = Tp + days.length * (ROW + GAP);
    const s = svg(box, W, H);
    const X = (frac) => L + frac * (W - L - R);
    ["12a", "6a", "12p", "6p", "12a"].forEach((lab, i) => {
      el(s, "text", { x: X(i / 4), y: 10, "text-anchor": "middle" }, lab);
      el(s, "line", { x1: X(i / 4), x2: X(i / 4), y1: Tp - 4, y2: H - GAP + 2, class: "grid" });
    });
    const byDay = T().uptime_days || {};
    days.forEach((day, i) => {
      const y = Tp + i * (ROW + GAP);
      const a = Math.max(etMidnight(day), tl.from_ms), dayStart = etMidnight(day), b = dayStart + 86_400_000;
      const f = (t) => Math.min(1, Math.max(0, (t - dayStart) / (b - dayStart)));
      el(s, "text", { x: 0, y: y + ROW - 3 }, dayName(day));
      const measured = tl.measured_from == null ? b : Math.max(a, tl.measured_from);
      const to = Math.min(b, tl.to_ms);
      if (measured > a) {
        const end = Math.min(measured, to);
        if (end > a) {
          const r = el(s, "rect", { x: X(f(a)), y: y + ROW / 2 - 0.5, width: X(f(end)) - X(f(a)), height: 1, fill: "var(--line-2)" });
          el(r, "title", {}, "Before the recorder logged heartbeats: not measured");
        }
      }
      if (to > measured) {
        el(s, "rect", { x: X(f(measured)), y, width: Math.max(0.5, X(f(to)) - X(f(measured))), height: ROW, fill: "var(--bid)", opacity: 0.7 });
        for (const [da, db] of tl.down) {
          const ca = Math.max(da, measured), cb = Math.min(db, to);
          if (cb > ca) {
            const r = el(s, "rect", { x: X(f(ca)), y: y - 1, width: Math.max(0.6, X(f(cb)) - X(f(ca))), height: ROW + 2, fill: "var(--bg)" });
            el(r, "title", {}, `Blind ${fHM.format(ca)}–${fHM.format(cb)} (${dur(cb - ca)})`);
          }
        }
      }
      for (const [ga, gb, label] of tl.games) {
        const ca = Math.max(ga, dayStart), cb = Math.min(gb, b);
        if (cb > ca) {
          const r = el(s, "rect", { x: X(f(ca)), y: y - 5, width: X(f(cb)) - X(f(ca)), height: 2, fill: "var(--warn)" });
          el(r, "title", {}, `${label}: 4 h before kickoff to the final`);
        }
      }
      const u = byDay[day];
      el(s, "text", { x: W, y: y + ROW - 3, "text-anchor": "end", fill: u == null ? "var(--muted)" : "var(--text-2)" }, u == null ? "—" : pct(u, 0));
    });
  }

  function drawGap(box, g) {
    const hist = g.beyond_hist;
    const W = Math.max(320, box.clientWidth), H = 210, L = 40, R = 10, Tp = 18, B = 36;
    const s = svg(box, W, H);
    const n = hist.counts.length;
    const maxC = Math.max(...hist.counts);
    const top = 10 ** Math.ceil(Math.log10(Math.max(10, maxC)));
    const X = (i) => L + (i / n) * (W - L - R);
    const Y = (c) => (c <= 0 ? H - B : Tp + (1 - Math.log10(Math.max(1, c)) / Math.log10(top)) * (H - Tp - B));
    for (let p = 1; p <= top; p *= 10) {
      el(s, "line", { x1: L, x2: W - R, y1: Y(p), y2: Y(p), class: "grid" });
      el(s, "text", { x: L - 8, y: Y(p) + 3.5, "text-anchor": "end" }, p >= 1000 ? p / 1000 + "k" : String(p));
    }
    const edge = (i) => hist.lo + i * hist.bin;
    hist.counts.forEach((c, i) => {
      if (!c) return;
      const over = edge(i) >= 0.02 - 1e-9;
      const r = el(s, "rect", { x: X(i) + 0.5, y: Y(c), width: Math.max(1, X(i + 1) - X(i) - 1), height: Math.max(1, H - B - Y(c)), fill: over ? "var(--fair)" : "var(--line-2)" });
      const label = i === n - 1 ? `${cents(edge(i), 2)} or more` : `${cents(edge(i), 2)} to ${cents(edge(i + 1), 2)}`;
      el(r, "title", {}, `${label} beyond half the spread: ${int(c)} side-minutes`);
    });
    const at = (x) => (x - hist.lo) / hist.bin;
    for (const c of [-0.04, -0.02, 0, 0.02, 0.04, 0.06, 0.08]) {
      el(s, "text", { x: X(at(c)), y: H - B + 14, "text-anchor": "middle" }, (c > 0 ? "+" : c < 0 ? MINUS : "") + Math.round(Math.abs(c * 100)) + "¢");
    }
    el(s, "line", { x1: X(at(0.02)), x2: X(at(0.02)), y1: Tp - 6, y2: H - B, stroke: "var(--fair)", "stroke-width": 1 });
    el(s, "text", { x: X(at(0.02)) + 6, y: Tp - 4, class: "lbl" }, `qualifies: ${g.qualifying_hours.toFixed(1)} h`);
    el(s, "text", { x: L, y: H - 4 }, "Beyond half the spread. Side-minutes, log scale.");
  }

  // ---------- events ----------
  function keepScroll(fn) {
    const scroller = $("tracker");
    const top = scroller.scrollTop;
    fn();
    scroller.scrollTop = top;
  }

  function selectVariant(id) {
    if (!variants().some((v) => v.id === id)) return;
    K.variant = id;
    K.allBets = false;
    store.set("variant", id);
    history.replaceState(null, "", `#tracker/${id}`);
    keepScroll(render);
    const row = document.querySelector(`tr[data-variant="${CSS.escape(id)}"]`);
    if (row) row.focus({ preventScroll: true });
  }

  function onClick(e) {
    const row = e.target.closest("tr[data-variant]");
    if (row) return selectVariant(row.dataset.variant);
    const jump = e.target.closest("[data-jump]");
    if (jump) {
      e.preventDefault();
      const target = $(jump.dataset.jump);
      if (target) target.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      return;
    }
    const act = e.target.closest("[data-act]");
    if (!act) return;
    if (act.dataset.act === "refresh") refreshNow();
    if (act.dataset.act === "all-bets") { K.allBets = !K.allBets; keepScroll(render); }
  }

  function onKey(e) {
    const typing = e.target.matches("input, textarea, select");
    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "1" && K.view === "tracker") { setView("markets"); return; }
    if (e.key === "2" && K.view !== "tracker") { setView("tracker"); return; }
    if (K.view !== "tracker") return;
    const row = e.target.closest && e.target.closest("tr[data-variant]");
    if (!row) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectVariant(row.dataset.variant); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const rows = [...document.querySelectorAll("tr[data-variant]")];
      const next = rows[rows.indexOf(row) + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) selectVariant(next.dataset.variant);
    }
  }

  function setView(v) {
    K.view = v;
    const tracker = v === "tracker";
    document.body.classList.toggle("view-tracker", tracker);
    $("tracker").hidden = !tracker;
    $("tab-markets").setAttribute("aria-selected", String(!tracker));
    $("tab-tracker").setAttribute("aria-selected", String(tracker));
    document.title = tracker ? "Fairline Tracker" : "Fairline Board";
    history.replaceState(null, "", tracker ? `#tracker${K.variant ? "/" + K.variant : ""}` : location.pathname);
    if (tracker) { render(); load(); } else clearTimeout(K.timer);
  }

  function boot() {
    document.querySelector(".views").addEventListener("click", (e) => {
      const b = e.target.closest("[data-view]");
      if (b) setView(b.dataset.view);
    });
    $("tracker").addEventListener("click", onClick);
    document.addEventListener("keydown", onKey);
    let rt = null;
    window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (K.view === "tracker") drawCharts(); }, 150); });
    setInterval(() => { if (K.view === "tracker" && K.data) renderLine(); }, 30_000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden && K.view === "tracker") load(); });
    const m = location.hash.match(/^#tracker(?:\/([\w@.]+))?/);
    if (m) {
      if (m[1]) { K.variant = decodeURIComponent(m[1]); store.set("variant", K.variant); }
      setView("tracker");
    }
  }

  boot();
})();
