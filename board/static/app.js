/* Fairline Board client. Polls /api/updates once a second and patches only what changed. */
"use strict";
(() => {
  const KIND_ORDER = { winner: 0, spread: 1, total: 2 };
  const KIND_BADGE = { winner: "WIN", spread: "SPR", total: "TOT" };
  const STREAMS = [
    ["kalshi_ws", "Kalshi live", 60_000],
    ["kalshi_rest", "Kalshi backup", 180_000],
    ["odds", "Odds", 2 * 3600_000 + 15 * 60_000],
  ];
  // With the recorders on EC2 the tape arrives an hour at a time, copied every 10 minutes.
  const REMOTE_STALE_MS = 75 * 60_000;
  const GAME_LENGTH = { nfl: 3.5 * 3600_000, nba: 2.75 * 3600_000 };
  const FAIR_STALE_MS = 2 * 3600_000 + 15 * 60_000;
  const INDEXER_STALE_MS = 15_000;
  const POLL_MS = 1000;
  const RANGES = ["15m", "1h", "6h", "24h", "all"];
  const MINUS = "−";
  const $ = (id) => document.getElementById(id);

  const S = {
    markets: new Map(), games: new Map(), health: [], winners: new Map(),
    rev: 0, tapeId: 0, skew: 0, lastSync: 0, lastTs: 0, online: false, failures: 0, error: null,
    filter: { kind: "all", fairOnly: false, q: "", game: null },
    sort: { key: "game", dir: 1 },
    selected: null, range: "6h", history: null, depth: null, sparks: new Map(), orderSig: "", visible: [],
  };
  const now = () => Date.now() + S.skew;

  // ---------- formatting ----------
  const cents = (p) => (p == null ? "—" : String(Math.round(p * 100)));
  const cents1 = (p) => (p == null ? "—" : (p * 100).toFixed(1));
  const signed = (x, d = 1) => {
    if (x == null || !isFinite(x)) return "—";
    const s = Math.abs(x * 100).toFixed(d);
    if (Number(s) === 0) return (0).toFixed(d);
    return (x > 0 ? "+" : MINUS) + s;
  };
  const qty = (n) => {
    if (n == null) return "—";
    const a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
    return String(Math.round(n));
  };
  const age = (ms) => {
    if (ms == null) return "—";
    const s = Math.max(0, Math.round((now() - ms) / 1000));
    if (s < 60) return s + "s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m";
    const h = Math.floor(m / 60);
    return h < 48 ? h + "h" : Math.floor(h / 24) + "d";
  };
  const TZ = "America/New_York";
  const fTime = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
  const fClock = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
  const fHM = new Intl.DateTimeFormat("en-US", { timeZone: TZ, hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const fDayHM = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const fDay = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "long", month: "short", day: "numeric" });
  const fWk = new Intl.DateTimeFormat("en-US", { timeZone: TZ, weekday: "short" });
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const setText = (el, text) => { if (el.textContent !== text) el.textContent = text; };
  const setHTML = (el, html) => { if (el._html !== html) { el.innerHTML = html; el._html = html; } };
  const store = {
    get(k, d) { try { const v = localStorage.getItem("fairline." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem("fairline." + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  // ---------- data ----------
  function derive(m) {
    m.spread = m.bid != null && m.ask != null ? m.ask - m.bid : null;
    m.gap = m.fair != null && m.mid != null ? m.fair - m.mid : null;
    m.chg = m.mid != null && m.open_mid != null ? m.mid - m.open_mid : null;
    m.size = m.bid_qty == null && m.ask_qty == null ? null : (m.bid_qty || 0) + (m.ask_qty || 0);
    return m;
  }

  function indexWinners() {
    S.winners.clear();
    for (const m of S.markets.values()) {
      if (m.kind !== "winner" || !m.team) continue;
      const w = S.winners.get(m.game_key) || {};
      w[m.team] = m.ticker;
      S.winners.set(m.game_key, w);
    }
  }

  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    let body = {};
    try { body = await r.json(); } catch { /* non-JSON error page */ }
    if (!r.ok) {
      const e = new Error(body.detail || r.statusText || "request failed");
      e.code = body.error || "http_" + r.status;
      throw e;
    }
    return body;
  }

  function ingestCommon(d) {
    S.skew = d.server_ms - Date.now();
    S.lastSync = Date.now();
    S.lastTs = d.last_ts_ms;
    S.health = d.health || [];
    S.remote = !!d.recorders_remote;
    S.games = new Map((d.games || []).map((g) => [g.key, g]));
  }

  async function loadSnapshot() {
    const d = await getJSON("/api/snapshot");
    ingestCommon(d);
    S.markets = new Map(d.markets.map((m) => [m.ticker, derive(m)]));
    rowEls.clear();
    grpEls.clear();
    S.orderSig = "";
    S.rev = d.rev;
    S.tapeId = d.tape.length ? d.tape[0].id : 0;
    indexWinners();
    renderTape(d.tape, true);
    renderAll();
  }

  async function poll() {
    try {
      const d = await getJSON(`/api/updates?rev=${S.rev}&tape=${S.tapeId}`);
      if (d.resync) {
        await loadSnapshot();
      } else {
        ingestCommon(d);
        const changed = [];
        let added = false;
        for (const m of d.markets) {
          const prev = S.markets.get(m.ticker);
          if (!prev) added = true;
          S.markets.set(m.ticker, derive(m));
          changed.push([m, prev]);
        }
        S.rev = d.rev;
        if (added) indexWinners();
        if (d.tape.length) {
          S.tapeId = Math.max(S.tapeId, d.tape[0].id);
          renderTape(d.tape, false);
        }
        applyChanges(changed, added);
      }
      setOnline(true);
    } catch (e) {
      setOnline(false, e);
    } finally {
      setTimeout(poll, S.online ? POLL_MS : Math.min(5000, 1000 * (1 + S.failures)));
    }
  }

  function setOnline(ok, err) {
    S.online = ok;
    S.failures = ok ? 0 : S.failures + 1;
    S.error = ok ? null : err;
    renderStatus();
  }

  // ---------- board table ----------
  const rowEls = new Map();
  const grpEls = new Map();

  function makeRow(m) {
    const tr = document.createElement("tr");
    tr.className = "row";
    tr.dataset.t = m.ticker;
    tr.innerHTML =
      `<td class="l"><div class="mk"><span class="badge ${esc(m.kind)}">${KIND_BADGE[m.kind] || ""}</span><span class="lb"></span><span class="gk"></span></div></td>` +
      `<td class="bid"></td><td class="ask"></td><td class="mid"></td><td></td><td class="c-size"></td><td></td><td class="fair"></td><td></td><td class="c-chg"></td><td></td><td></td>`;
    const td = tr.children;
    const r = {
      tr, lb: tr.querySelector(".lb"), gk: tr.querySelector(".gk"),
      bid: td[1], ask: td[2], mid: td[3], spr: td[4], sz: td[5], last: td[6], fair: td[7], gap: td[8], chg: td[9], vol: td[10], upd: td[11],
    };
    rowEls.set(m.ticker, r);
    fillRow(r, m, null);
    return r;
  }

  function gapClass(g) {
    const c = g * 100;
    if (c >= 1.5) return "p2";
    if (c <= -1.5) return "n2";
    if (c >= 0.5) return "p1";
    if (c <= -0.5) return "n1";
    return "";
  }

  function flash(el, a, b) {
    if (a == null || b == null || a === b) return;
    el.classList.remove("fu", "fd");
    void el.offsetWidth; // restart the animation
    el.classList.add(a > b ? "fu" : "fd");
  }

  function fillRow(r, m, prev) {
    setText(r.lb, m.label);
    r.tr.title = `${m.title || m.label} · ${m.ticker}`;
    setText(r.bid, cents(m.bid));
    setText(r.ask, cents(m.ask));
    setText(r.mid, cents1(m.mid));
    setText(r.spr, m.spread == null ? "—" : String(Math.round(m.spread * 100)));
    setHTML(r.sz, m.size == null ? '<span class="dim">—</span>' : `${qty(m.bid_qty)}<span class="dim"> × </span>${qty(m.ask_qty)}`);
    setHTML(r.last, m.last_price == null ? '<span class="dim">—</span>' :
      `<span class="${m.last_side === "no" ? "side-no" : "side-yes"}">${m.last_side === "no" ? "▼" : "▲"}</span> ${cents(m.last_price)}`);
    setText(r.fair, cents1(m.fair));
    r.fair.classList.toggle("stale", m.fair != null && m.fair_ms != null && now() - m.fair_ms > FAIR_STALE_MS);
    r.fair.title = m.fair == null ? "No identical Pinnacle line" : `Pinnacle, margin removed · line updated ${age(m.fair_ms)} ago`;
    setHTML(r.gap, m.gap == null ? '<span class="dim">—</span>' : `<span class="gap ${gapClass(m.gap)}">${signed(m.gap)}</span>`);
    setHTML(r.chg, m.chg == null ? '<span class="dim">—</span>' : `<span class="${m.chg > 0 ? "up" : m.chg < 0 ? "down" : ""}">${signed(m.chg)}</span>`);
    setText(r.vol, m.volume_today ? qty(m.volume_today) : "—");
    setText(r.upd, age(m.updated_ms));
    if (prev) {
      flash(r.bid, m.bid, prev.bid);
      flash(r.ask, m.ask, prev.ask);
      flash(r.mid, m.mid, prev.mid);
      if (m.last_trade_ms && m.last_trade_ms !== prev.last_trade_ms) flash(r.last, m.last_side === "no" ? 0 : 1, m.last_side === "no" ? 1 : 0);
    }
  }

  function visibleMarkets() {
    const words = S.filter.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const out = [];
    for (const m of S.markets.values()) {
      if (S.filter.kind !== "all" && m.kind !== S.filter.kind) continue;
      if (S.filter.game && m.game_key !== S.filter.game) continue;
      if (S.filter.fairOnly && m.fair == null) continue;
      if (words.length) {
        const g = S.games.get(m.game_key);
        const hay = `${m.label} ${m.ticker} ${g ? g.label : ""} ${m.title || ""}`.toLowerCase();
        if (!words.every((w) => hay.includes(w))) continue;
      }
      out.push(m);
    }
    return out;
  }

  const SORT_VALUE = {
    label: (m) => m.label, bid: (m) => m.bid, ask: (m) => m.ask, mid: (m) => m.mid, spread: (m) => m.spread,
    size: (m) => m.size, last: (m) => m.last_trade_ms, fair: (m) => m.fair, gap: (m) => (m.gap == null ? null : Math.abs(m.gap)),
    chg: (m) => (m.chg == null ? null : Math.abs(m.chg)), vol: (m) => m.volume_today, upd: (m) => m.updated_ms,
  };

  function compare(a, b, dir) {
    const an = a == null, bn = b == null;
    if (an && bn) return 0;
    if (an) return 1; // empty values always sink
    if (bn) return -1;
    return typeof a === "string" ? dir * a.localeCompare(b, undefined, { numeric: true }) : dir * (a - b);
  }

  function gameOrder(a, b) {
    const ga = S.games.get(a.game_key), gb = S.games.get(b.game_key);
    const ka = ga && ga.kickoff_ms != null ? ga.kickoff_ms : 9e15, kb = gb && gb.kickoff_ms != null ? gb.kickoff_ms : 9e15;
    if (ka !== kb) return ka - kb;
    if (a.game_key !== b.game_key) return a.game_key < b.game_key ? -1 : 1;
    if (a.kind !== b.kind) return (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9);
    return (a.sort_key ?? 0) - (b.sort_key ?? 0);
  }

  function gameStatus(g) {
    if (!g || g.kickoff_ms == null) return { text: "", cls: "" };
    const t = now(), k = g.kickoff_ms, end = k + (GAME_LENGTH[g.league] || GAME_LENGTH.nfl);
    if (t >= end) return { text: "ended", cls: "" };
    if (t >= k) return { text: "LIVE", cls: "live" };
    const mins = Math.round((k - t) / 60_000);
    if (mins < 60) return { text: `in ${mins}m`, cls: "soon" };
    if (mins < 24 * 60) return { text: `in ${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m`, cls: mins <= 180 ? "soon" : "" };
    return { text: `${fWk.format(k)} ${fTime.format(k)}`, cls: "" };
  }

  function kickoffText(g) {
    if (!g || g.kickoff_ms == null) return "";
    return `${fWk.format(g.kickoff_ms)} ${fTime.format(g.kickoff_ms)} ET${g.kickoff_estimated ? " (est.)" : ""}`;
  }

  function groupRow(key, count) {
    let tr = grpEls.get(key);
    if (!tr) {
      tr = document.createElement("tr");
      tr.className = "grp";
      tr.innerHTML = '<td colspan="12"></td>';
      tr.dataset.g = key;
      grpEls.set(key, tr);
    }
    tr.dataset.count = count;
    fillGroup(tr);
    return tr;
  }

  function fillGroup(tr) {
    const g = S.games.get(tr.dataset.g);
    const st = gameStatus(g);
    const status = st.cls === "live" ? '<span class="live">● LIVE</span>' : esc(st.text);
    setHTML(tr.firstChild, `${esc(g ? g.label : tr.dataset.g)}<span class="gmeta">${esc(kickoffText(g))}${status ? " · " + status : ""} · ${tr.dataset.count} markets</span>`);
  }

  function renderRows() {
    const list = visibleMarkets();
    const grouped = S.sort.key === "game";
    if (grouped) list.sort(gameOrder);
    else {
      const f = SORT_VALUE[S.sort.key];
      list.sort((a, b) => compare(f(a), f(b), S.sort.dir) || gameOrder(a, b));
    }
    S.visible = list;
    const sig = (grouped ? "g" : S.sort.key + S.sort.dir) + "|" + list.map((m) => m.ticker).join(",");
    if (sig !== S.orderSig) {
      const counts = new Map();
      if (grouped) for (const m of list) counts.set(m.game_key, (counts.get(m.game_key) || 0) + 1);
      const frag = document.createDocumentFragment();
      let lastGame = null;
      for (const m of list) {
        if (grouped && m.game_key !== lastGame) {
          frag.appendChild(groupRow(m.game_key, counts.get(m.game_key)));
          lastGame = m.game_key;
        }
        frag.appendChild((rowEls.get(m.ticker) || makeRow(m)).tr);
      }
      $("rows").replaceChildren(frag);
      S.orderSig = sig;
    }
    const showGame = !grouped;
    for (const m of list) {
      const r = rowEls.get(m.ticker);
      const g = S.games.get(m.game_key);
      setText(r.gk, showGame && g ? g.label : "");
      r.tr.classList.toggle("sel", m.ticker === S.selected);
    }
    for (const th of document.querySelectorAll("#board th")) {
      const on = th.dataset.sort === S.sort.key;
      th.classList.toggle("sorted", on);
      th.classList.toggle("asc", on && S.sort.dir === 1);
      th.setAttribute("aria-sort", on ? (S.sort.dir === 1 ? "ascending" : "descending") : "none");
    }
    setText($("row-count"), `${list.length.toLocaleString()} of ${S.markets.size.toLocaleString()} markets`);
    renderEmpty(list.length);
  }

  function renderEmpty(n) {
    const box = $("empty");
    if (S.error && S.error.code === "no_database") {
      box.innerHTML = "<b>No board database yet</b>Start the indexer and it will build one from your tape in a few seconds:<br><code>.venv/bin/python -m tape.ctl start board</code>";
      box.hidden = false;
    } else if (!S.markets.size && !S.online) {
      box.innerHTML = "<b>Can't reach the board server</b>Retrying… If it isn't running:<br><code>.venv/bin/python -m tape.ctl start board-web</code>";
      box.hidden = false;
    } else if (!S.markets.size) {
      box.innerHTML = "<b>No markets recorded yet</b>The board fills in as soon as a recorder writes its first market list.";
      box.hidden = false;
    } else if (!n) {
      box.innerHTML = "<b>No markets match</b>Clear the search or switch filters.";
      box.hidden = false;
    } else {
      box.hidden = true;
    }
  }

  function applyChanges(changed, added) {
    for (const [m, prev] of changed) {
      const r = rowEls.get(m.ticker);
      if (r) fillRow(r, m, prev);
      if (m.ticker === S.selected) renderDrawerStats();
    }
    const valueSort = S.sort.key !== "game";
    const filtered = S.filter.fairOnly || S.filter.q;
    if (added || (changed.length && (valueSort || filtered))) renderRows();
    renderGames();
    renderStatus();
  }

  // ---------- games sidebar ----------
  const cardEls = new Map();
  let gamesSig = "";

  function sortedGames() {
    return [...S.games.values()].sort((a, b) => (a.kickoff_ms ?? 9e15) - (b.kickoff_ms ?? 9e15) || a.key.localeCompare(b.key));
  }

  function renderGames() {
    const games = sortedGames();
    const sig = games.map((g) => g.key + ":" + (g.kickoff_ms ?? "")).join(",");
    const list = $("games-list");
    if (sig !== gamesSig) {
      const frag = document.createDocumentFragment();
      const all = document.createElement("button");
      all.type = "button";
      all.className = "game all";
      all.dataset.key = "";
      all.innerHTML = '<div class="g1"><span class="gl">All games</span><span class="gt" id="all-count"></span></div>';
      frag.appendChild(all);
      let day = null;
      for (const g of games) {
        const d = g.kickoff_ms != null ? fDay.format(g.kickoff_ms) : "Date to be set";
        if (d !== day) {
          const h = document.createElement("div");
          h.className = "day-head";
          h.textContent = d;
          frag.appendChild(h);
          day = d;
        }
        frag.appendChild(card(g));
      }
      list.replaceChildren(frag);
      gamesSig = sig;
    }
    for (const g of games) fillCard(g);
    for (const el of list.querySelectorAll(".game")) el.classList.toggle("on", (el.dataset.key || null) === S.filter.game);
    const allCount = $("all-count");
    if (allCount) setText(allCount, `${S.markets.size} mkts`);
    setText($("games-count"), String(S.games.size));
  }

  function card(g) {
    let c = cardEls.get(g.key);
    if (!c) {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "game";
      el.dataset.key = g.key;
      el.innerHTML = '<div class="g1"><span class="gl"></span><span class="gt"></span></div><div class="g2"><div class="teams"></div><svg class="spark" viewBox="0 0 72 26" preserveAspectRatio="none" aria-hidden="true"></svg></div>';
      c = { el, gl: el.querySelector(".gl"), gt: el.querySelector(".gt"), teams: el.querySelector(".teams"), spark: el.querySelector(".spark") };
      cardEls.set(g.key, c);
    }
    return c.el;
  }

  function fillCard(g) {
    const c = cardEls.get(g.key);
    if (!c) return;
    setText(c.gl, g.label);
    const st = gameStatus(g);
    setText(c.gt, st.cls === "live" ? "● LIVE" : st.text);
    c.gt.className = "gt " + st.cls;
    const w = S.winners.get(g.key) || {};
    const lines = [g.away, g.home].filter(Boolean).map((team) => {
      const m = S.markets.get(w[team]);
      const fair = m && m.fair != null ? `<span class="f num">${cents1(m.fair)}</span>` : "";
      return `<div class="team"><span>${esc(team)}</span><span><span class="num">${m ? cents1(m.mid) : "—"}</span>${fair}</span></div>`;
    });
    setHTML(c.teams, lines.join(""));
    const homeTicker = w[g.home];
    setHTML(c.spark, sparkPath(S.sparks.get(homeTicker), 72, 26));
    c.el.title = `${g.label} · ${kickoffText(g)} · sparkline: ${g.home || "home"} win price, last 24h`;
  }

  function sparkPath(points, w, h) {
    if (!points || points.length < 2) return "";
    const t0 = now() - 24 * 3600_000, t1 = now();
    let lo = Infinity, hi = -Infinity;
    for (const [, v] of points) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    if (hi - lo < 0.01) { lo -= 0.005; hi += 0.005; }
    const X = (t) => ((Math.max(t, t0) - t0) / (t1 - t0)) * w;
    const Y = (v) => 2 + (1 - (v - lo) / (hi - lo)) * (h - 4);
    let d = "";
    points.forEach(([t, v], i) => { d += (i ? `H${X(t).toFixed(1)}V` : `M${X(t).toFixed(1)},`) + Y(v).toFixed(1); });
    d += `H${w}`;
    const area = `${d}V${h}H${X(points[0][0]).toFixed(1)}Z`;
    return `<path class="area" d="${area}"/><path d="${d}"/>`;
  }

  async function refreshSparks() {
    const tickers = [];
    for (const g of S.games.values()) {
      const w = S.winners.get(g.key);
      if (w && w[g.home]) tickers.push(w[g.home]);
    }
    if (!tickers.length) return;
    try {
      const d = await getJSON(`/api/sparks?range=24h&tickers=${encodeURIComponent(tickers.join(","))}`);
      for (const [t, pts] of Object.entries(d.series)) S.sparks.set(t, pts);
      renderGames();
    } catch { /* next refresh will retry */ }
  }

  // ---------- header and status ----------
  function renderStatus() {
    const t = now();
    const live = $("live");
    const indexerAge = S.health.length ? t - Math.max(...S.health.map((h) => h.updated_ms || 0)) : Infinity;
    let cls = "ok", text;
    if (!S.online && !S.lastSync && !S.error) { cls = ""; text = "Connecting…"; }
    else if (!S.online) { cls = "down"; text = "Server offline"; }
    else if (indexerAge > INDEXER_STALE_MS) { cls = "warn"; text = "Indexer paused"; }
    else text = `Live · data ${age(S.lastTs)}`;
    live.className = "live " + cls;
    setText($("live-text"), text);

    setHTML($("streams"), STREAMS.map(([key, label, staleMs]) => {
      const h = S.health.find((x) => x.stream === key);
      if (!h) return `<span class="pill"><i></i><b>${label}</b><span class="num">—</span></span>`;
      const dataAge = h.last_ms ? t - h.last_ms : Infinity;
      let state = "ok";
      if (h.running === 0) state = "down";
      else if (dataAge > (S.remote ? Math.max(staleMs, REMOTE_STALE_MS) : staleMs) || (key === "kalshi_ws" && h.connected === 0)) state = "warn";
      const extra = key === "odds" && h.credits ? ` · ${h.credits} cr` : "";
      const tip = [`${label}: ${h.running === 0 ? "recorder not running" : h.running === 1 ? "recorder running" : "recorder status unknown"}`,
        `last data ${age(h.last_ms)} ago`, h.per_min ? `${h.per_min}/min` : "", h.gaps_today ? `${h.gaps_today} gaps today` : "",
        h.last_error ? `last error: ${h.last_error}` : ""].filter(Boolean).join("\n");
      return `<span class="pill ${state}" title="${esc(tip)}"><i></i><b>${label}</b><span class="num">${age(h.last_ms)}${extra}</span></span>`;
    }).join(""));

    const ws = S.health.find((x) => x.stream === "kalshi_ws");
    setHTML($("counters"), `<span><b>${S.markets.size}</b> markets</span><span><b>${S.games.size}</b> games</span>` +
      `<span><b>${ws ? qty(ws.per_min || 0) : "—"}</b> msgs/min</span><span><b>${ws ? ws.gaps_today || 0 : "—"}</b> gaps today</span>`);

    const banner = $("banner");
    const down = S.health.filter((h) => h.running === 0).map((h) => STREAMS.find((s) => s[0] === h.stream)?.[1] || h.stream);
    let msg = "", bad = false;
    if (!S.online && S.lastSync) { msg = `Lost contact with the board server ${age(S.lastSync + S.skew)} ago. Retrying… If it stopped: <code>.venv/bin/python -m tape.ctl start board-web</code>`; bad = true; }
    else if (S.online && S.health.length && indexerAge > INDEXER_STALE_MS) { msg = `The indexer stopped updating ${age(t - indexerAge)} ago, so prices are frozen. Start it: <code>.venv/bin/python -m tape.ctl start board</code>`; bad = true; }
    else if (down.length) msg = `Recorder not running: <b>${down.join(", ")}</b>. Check it: <code>.venv/bin/python -m tape.ctl status</code>`;
    banner.hidden = !msg;
    banner.className = "banner" + (bad ? " down" : "");
    setHTML(banner, msg);
  }

  function tick() {
    setText($("clock"), fClock.format(now()) + " ET");
    for (const m of S.visible) {
      const r = rowEls.get(m.ticker);
      if (r) setText(r.upd, age(m.updated_ms));
    }
    for (const tr of grpEls.values()) if (tr.isConnected) fillGroup(tr);
    for (const g of S.games.values()) fillCard(g);
    renderStatus();
    if (S.selected) renderDrawerStats();
  }

  // ---------- tape ----------
  function tapeItem(ev) {
    const li = document.createElement("li");
    li.dataset.t = ev.ticker;
    const g = S.games.get(ev.game_key);
    let what;
    if (ev.kind === "trade") {
      const yes = ev.side !== "no";
      what = `<span class="${yes ? "side-yes" : "side-no"}">TRADE</span> ${qty(ev.qty)} @ ${cents(ev.new)}¢ <span class="dim">${yes ? "YES" : "NO"} taker</span>`;
    } else {
      const up = ev.new > ev.old;
      what = `<span class="${up ? "up" : "down"}">${up ? "▲" : "▼"}</span> mid ${cents1(ev.old)} → <b>${cents1(ev.new)}</b>`;
    }
    li.innerHTML = `<span class="t">${fClock.format(ev.ts_ms)}</span><span class="g">${esc(g ? g.label : "")}</span><span class="m">${esc(ev.label || ev.ticker)}</span><span>${what}</span>`;
    return li;
  }

  function renderTape(items, reset) {
    const ol = $("tape-list");
    if (reset) ol.replaceChildren();
    for (const ev of [...items].reverse()) {
      const li = tapeItem(ev);
      if (!reset) li.classList.add("new");
      ol.prepend(li);
    }
    while (ol.children.length > 150) ol.lastChild.remove();
    setText($("tape-count"), ol.children.length ? `latest ${ol.children.length}` : "");
  }

  // ---------- drawer ----------
  let historyTimer = null, depthTimer = null, chartGeom = null;

  function openDrawer(ticker) {
    if (!S.markets.has(ticker)) return;
    const switching = S.selected !== ticker;
    S.selected = ticker;
    $("layout").classList.add("with-drawer");
    const dr = $("drawer");
    dr.hidden = false;
    if (switching) {
      S.history = null;
      S.depth = null;
      buildDrawer();
    }
    for (const r of rowEls.values()) r.tr.classList.toggle("sel", r.tr.dataset.t === ticker);
    const r = rowEls.get(ticker);
    if (r && r.tr.isConnected) r.tr.scrollIntoView({ block: "nearest" });
    loadHistory();
    loadDepth();
    clearInterval(historyTimer);
    clearInterval(depthTimer);
    historyTimer = setInterval(loadHistory, 5000);
    depthTimer = setInterval(loadDepth, 1500);
  }

  function closeDrawer() {
    S.selected = null;
    clearInterval(historyTimer);
    clearInterval(depthTimer);
    $("drawer").hidden = true;
    $("layout").classList.remove("with-drawer");
    for (const r of rowEls.values()) r.tr.classList.remove("sel");
  }

  function buildDrawer() {
    const m = S.markets.get(S.selected);
    const g = S.games.get(m.game_key);
    const ladderTitle = m.kind === "winner" ? "Both sides" : m.kind === "spread" ? `${esc(m.team)} spread ladder` : "Total ladder";
    $("drawer").innerHTML = `
      <div class="dr-head">
        <div class="dr-title"><span class="badge ${esc(m.kind)}">${KIND_BADGE[m.kind] || ""}</span><h2>${esc(m.label)}</h2>
          <button type="button" class="dr-close" id="dr-close" aria-label="Close detail">✕</button></div>
        <div class="dr-sub">${esc(g ? g.label : "")} · ${esc(kickoffText(g))}<br>${esc(m.title || "")} · <span class="num">${esc(m.ticker)}</span></div>
      </div>
      <div class="stats" id="dr-stats"></div>
      <div class="section">
        <h3>Price<span class="spacer"></span><span class="ranges" id="dr-ranges">${RANGES.map((r) => `<button type="button" data-r="${r}" class="${r === S.range ? "on" : ""}">${r.toUpperCase()}</button>`).join("")}</span></h3>
        <div class="chart" id="dr-chart"><div class="msg">Loading…</div></div>
        <div class="legend"><span><i></i>Mid</span><span><i class="bid"></i>Bid</span><span><i class="ask"></i>Ask</span><span><i class="fair"></i>Pinnacle fair</span><span><i class="dot"></i>Trades (YES / NO taker)</span></div>
      </div>
      <div class="section"><h3>Order book<span class="spacer"></span><span class="muted small" style="text-transform:none;letter-spacing:0">YES side · top 10 levels</span></h3><div class="depth" id="dr-depth"></div></div>
      <div class="section"><h3>${ladderTitle}</h3><table class="ladder" id="dr-ladder"></table>
        <p class="note">Fair value appears only where Pinnacle offers the identical line, devigged with the power method. Click a row to switch.</p></div>`;
    $("dr-close").addEventListener("click", closeDrawer);
    $("dr-ranges").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-r]");
      if (!b) return;
      S.range = b.dataset.r;
      store.set("range", S.range);
      for (const x of $("dr-ranges").children) x.classList.toggle("on", x === b);
      S.history = null;
      $("dr-chart").innerHTML = '<div class="msg">Loading…</div>';
      loadHistory();
    });
    $("dr-ladder").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-t]");
      if (tr) openDrawer(tr.dataset.t);
    });
    renderDrawerStats();
  }

  function renderDrawerStats() {
    const m = S.markets.get(S.selected);
    const box = $("dr-stats");
    if (!m || !box) return;
    const stat = (k, v, cls = "", small = "") => `<div class="stat"><div class="k">${k}</div><div class="v ${cls}">${v}${small ? `<small>${small}</small>` : ""}</div></div>`;
    const gapCls = m.gap == null ? "" : m.gap > 0 ? "up" : m.gap < 0 ? "down" : "";
    setHTML(box,
      stat("Bid", cents(m.bid), "bid", m.bid_qty != null ? qty(m.bid_qty) : "") +
      stat("Ask", cents(m.ask), "ask", m.ask_qty != null ? qty(m.ask_qty) : "") +
      stat("Mid", cents1(m.mid)) +
      stat("Fair", cents1(m.fair), "fair", m.fair_ms ? age(m.fair_ms) : "") +
      stat("Gap", m.gap == null ? "—" : signed(m.gap), gapCls) +
      stat("Last", m.last_price == null ? "—" : cents(m.last_price), m.last_side === "no" ? "down" : m.last_side ? "up" : "", m.last_trade_ms ? age(m.last_trade_ms) : "") +
      stat("Vol today", m.volume_today ? qty(m.volume_today) : "—", "", m.trades_today ? `${m.trades_today} trades` : "") +
      stat("Δ day", m.chg == null ? "—" : signed(m.chg), m.chg > 0 ? "up" : m.chg < 0 ? "down" : ""));
    renderLadder();
  }

  async function loadHistory() {
    const ticker = S.selected;
    if (!ticker) return;
    try {
      const h = await getJSON(`/api/history?ticker=${encodeURIComponent(ticker)}&range=${S.range}`);
      if (ticker !== S.selected) return;
      S.history = h;
      drawChart();
    } catch (e) {
      if (ticker === S.selected && !S.history) $("dr-chart").innerHTML = `<div class="msg">Couldn't load history (${esc(e.message)})</div>`;
    }
  }

  async function loadDepth() {
    const ticker = S.selected;
    if (!ticker) return;
    try {
      const d = await getJSON(`/api/depth?ticker=${encodeURIComponent(ticker)}`);
      if (ticker !== S.selected) return;
      S.depth = d;
      renderDepth();
    } catch { /* keep the last book on screen */ }
  }

  function renderDepth() {
    const d = S.depth, box = $("dr-depth");
    if (!d || !box) return;
    const all = [...d.bids, ...d.asks].map((l) => l[1]);
    const max = Math.sqrt(Math.max(1, ...all));
    const side = (rows, cls, label) => `<table class="${cls}"><thead><tr><th>${label}</th><th>Contracts</th></tr></thead><tbody>${
      rows.length ? rows.map(([p, q], i) => `<tr class="${i === 0 ? "best" : ""}"><td><span>${cents(p)}¢</span></td><td><div class="bar" style="width:${((Math.sqrt(q) / max) * 100).toFixed(1)}%"></div><span>${qty(q)}</span></td></tr>`).join("")
        : '<tr><td colspan="2" class="dim">empty</td></tr>'}</tbody></table>`;
    setHTML(box, side(d.bids, "bids", "Bid") + side(d.asks, "asks", "Ask"));
  }

  function renderLadder() {
    const m = S.markets.get(S.selected), box = $("dr-ladder");
    if (!m || !box) return;
    const same = [...S.markets.values()].filter((x) => x.game_key === m.game_key && x.kind === m.kind && (m.kind !== "spread" || x.team === m.team));
    same.sort((a, b) => (a.strike ?? 0) - (b.strike ?? 0) || (a.sort_key ?? 0) - (b.sort_key ?? 0));
    const rows = same.map((x) => `<tr data-t="${esc(x.ticker)}" class="${x.ticker === m.ticker ? "cur" : ""}"><td>${esc(x.label)}</td><td class="bid">${cents(x.bid)}</td><td class="ask">${cents(x.ask)}</td>` +
      `<td><span class="pbar" style="width:${x.mid == null ? 0 : Math.round(x.mid * 60)}px"></span> ${cents1(x.mid)}</td><td style="color:var(--fair)">${cents1(x.fair)}</td>` +
      `<td><span class="gap ${x.gap == null ? "" : gapClass(x.gap)}">${x.gap == null ? "—" : signed(x.gap)}</span></td></tr>`).join("");
    setHTML(box, `<thead><tr><th>Line</th><th>Bid</th><th>Ask</th><th>Mid</th><th>Fair</th><th>Gap</th></tr></thead><tbody>${rows}</tbody>`);
  }

  // ---------- chart ----------
  function stepPath(points, X, Y, xEnd) {
    let d = "", open = false;
    for (const [t, v] of points) {
      const x = X(t).toFixed(1);
      if (v == null) { if (open) { d += `H${x}`; open = false; } continue; }
      const y = Y(v).toFixed(1);
      d += open ? `H${x}V${y}` : `M${x},${y}`;
      open = true;
    }
    if (open) d += `H${xEnd.toFixed(1)}`;
    return d;
  }

  function niceStep(span) {
    for (const s of [0.5, 1, 2, 5, 10, 20, 25, 50]) if (span / s <= 6) return s;
    return 100;
  }

  function drawChart() {
    const box = $("dr-chart"), h = S.history;
    if (!box || !h) return;
    if (!h.tob.length) { box.innerHTML = '<div class="msg">No price changes recorded in this window</div>'; chartGeom = null; return; }
    const W = box.clientWidth, H = box.clientHeight, P = { l: 42, r: 12, t: 12, b: 24 };
    const x0 = h.from_ms, x1 = Math.max(h.to_ms, now());
    let lo = Infinity, hi = -Infinity;
    const see = (v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
    for (const [, b, a] of h.tob) { see(b); see(a); }
    for (const [, f] of h.fair) see(f);
    for (const [, p] of h.trades) see(p);
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const pad = Math.max(0.01, (hi - lo) * 0.12);
    lo = Math.max(0, lo - pad);
    hi = Math.min(1, hi + pad);
    const X = (t) => P.l + ((Math.min(Math.max(t, x0), x1) - x0) / Math.max(1, x1 - x0)) * (W - P.l - P.r);
    const Y = (p) => P.t + (1 - (p - lo) / (hi - lo)) * (H - P.t - P.b);

    const step = niceStep((hi - lo) * 100) / 100;
    let grid = "", axis = "";
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
      const y = Y(v).toFixed(1);
      grid += `<line x1="${P.l}" x2="${W - P.r}" y1="${y}" y2="${y}"/>`;
      axis += `<text x="${P.l - 6}" y="${(+y + 3.5).toFixed(1)}" text-anchor="end">${(v * 100).toFixed(step < 0.01 ? 1 : 0)}¢</text>`;
    }
    const multiDay = x1 - x0 > 20 * 3600_000;
    const ticks = Math.max(2, Math.floor((W - P.l - P.r) / 90));
    for (let i = 0; i <= ticks; i++) {
      const t = x0 + ((x1 - x0) * i) / ticks;
      axis += `<text x="${X(t).toFixed(1)}" y="${H - 7}" text-anchor="${i === 0 ? "start" : i === ticks ? "end" : "middle"}">${(multiDay ? fDayHM : fHM).format(t)}</text>`;
    }

    let band = "";
    for (let i = 0; i < h.tob.length; i++) {
      const [t, b, a] = h.tob[i];
      if (b == null || a == null) continue;
      const xa = X(t), xb = i + 1 < h.tob.length ? X(h.tob[i + 1][0]) : X(x1);
      band += `M${xa.toFixed(1)},${Y(a).toFixed(1)}H${xb.toFixed(1)}V${Y(b).toFixed(1)}H${xa.toFixed(1)}Z`;
    }
    const mids = h.tob.map(([t, b, a]) => [t, b != null && a != null ? (b + a) / 2 : null]);
    const trades = h.trades.map(([t, p, q, s]) => `<circle class="${s === "no" ? "trade-no" : "trade-yes"}" cx="${X(t).toFixed(1)}" cy="${Y(p).toFixed(1)}" r="${(1.6 + Math.min(5, Math.sqrt(q || 0) / 6)).toFixed(1)}"/>`).join("");
    box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Price history">
        <g class="grid">${grid}</g>
        <path class="band" d="${band}"/>
        <path class="bidl" d="${stepPath(h.tob.map((r) => [r[0], r[1]]), X, Y, X(x1))}"/>
        <path class="askl" d="${stepPath(h.tob.map((r) => [r[0], r[2]]), X, Y, X(x1))}"/>
        <path class="midl" d="${stepPath(mids, X, Y, X(x1))}"/>
        <path class="fairl" d="${stepPath(h.fair, X, Y, X(x1))}"/>
        <g>${trades}</g>
        <g class="axis">${axis}</g>
        <line class="cross" id="cross" y1="${P.t}" y2="${H - P.b}" x1="-10" x2="-10"/>
        <rect id="hit" x="${P.l}" y="${P.t}" width="${W - P.l - P.r}" height="${H - P.t - P.b}" fill="transparent"/>
      </svg><div class="tip" id="tip" hidden></div>`;
    chartGeom = { X, x0, x1, P, W };
    const hit = box.querySelector("#hit");
    hit.addEventListener("mousemove", onChartMove);
    hit.addEventListener("mouseleave", () => { $("tip").hidden = true; box.querySelector("#cross").setAttribute("x1", -10); box.querySelector("#cross").setAttribute("x2", -10); });
  }

  function lastAtOrBefore(rows, t) {
    let lo = 0, hi = rows.length - 1, ans = -1;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (rows[mid][0] <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
    return ans >= 0 ? rows[ans] : null;
  }

  function onChartMove(e) {
    const box = $("dr-chart"), h = S.history, g = chartGeom;
    if (!h || !g) return;
    const rect = box.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const t = g.x0 + ((px - g.P.l) / (g.W - g.P.l - g.P.r)) * (g.x1 - g.x0);
    const r = lastAtOrBefore(h.tob, t), f = lastAtOrBefore(h.fair, t);
    const cross = box.querySelector("#cross");
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    const bid = r ? r[1] : null, ask = r ? r[2] : null, mid = bid != null && ask != null ? (bid + ask) / 2 : null;
    const fair = f ? f[1] : null;
    const tip = $("tip");
    tip.innerHTML = `<b>${fDayHM.format(t)} ET</b><br>bid <b style="color:var(--bid)">${cents(bid)}</b> · ask <b style="color:var(--ask)">${cents(ask)}</b> · mid <b>${cents1(mid)}</b>` +
      (fair != null ? `<br>fair <b style="color:var(--fair)">${cents1(fair)}</b> · gap <b>${mid != null ? signed(fair - mid) : "—"}</b>` : "");
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = `${Math.min(Math.max(8, px + 12), rect.width - tw - 8)}px`;
    tip.style.top = "10px";
  }

  // ---------- events ----------
  function renderAll() {
    renderRows();
    renderGames();
    renderStatus();
    if (S.selected && S.markets.has(S.selected)) renderDrawerStats();
  }

  function bind() {
    $("kind-filter").addEventListener("click", (e) => {
      const b = e.target.closest("button[data-kind]");
      if (!b) return;
      S.filter.kind = b.dataset.kind;
      for (const x of $("kind-filter").children) x.classList.toggle("on", x === b);
      renderRows();
    });
    $("fair-only").addEventListener("change", (e) => { S.filter.fairOnly = e.target.checked; renderRows(); });
    $("search").addEventListener("input", (e) => { S.filter.q = e.target.value; renderRows(); });
    $("games-list").addEventListener("click", (e) => {
      const b = e.target.closest(".game");
      if (!b) return;
      const key = b.dataset.key || null;
      S.filter.game = S.filter.game === key ? null : key;
      renderGames();
      renderRows();
      $("table-wrap").scrollTop = 0;
    });
    document.querySelector("#board thead").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort]");
      if (!th) return;
      const key = th.dataset.sort;
      if (key === "label" && S.sort.key === "label" && S.sort.dir === -1) S.sort = { key: "game", dir: 1 }; // third click returns to game order
      else if (S.sort.key === key) S.sort.dir *= -1;
      else S.sort = { key, dir: key === "label" ? 1 : -1 };
      store.set("sort", S.sort);
      renderRows();
    });
    $("rows").addEventListener("click", (e) => {
      const tr = e.target.closest("tr.row");
      if (tr) openDrawer(tr.dataset.t);
    });
    $("tape-list").addEventListener("click", (e) => {
      const li = e.target.closest("li[data-t]");
      if (li) openDrawer(li.dataset.t);
    });
    $("tape-toggle").addEventListener("click", () => {
      const closed = $("tape").classList.toggle("closed");
      $("tape-toggle").setAttribute("aria-expanded", String(!closed));
      store.set("tapeClosed", closed);
    });
    document.addEventListener("keydown", (e) => {
      if (document.body.classList.contains("view-tracker")) return; // the tracker handles its own keys
      const typing = e.target.matches("input, textarea, select");
      if (e.key === "/" && !typing) { e.preventDefault(); $("search").focus(); $("search").select(); return; }
      if (e.key === "Escape") {
        if (typing && e.target.value) { e.target.value = ""; S.filter.q = ""; renderRows(); return; }
        if (typing) { e.target.blur(); return; }
        if (S.selected) closeDrawer();
        return;
      }
      if (!typing && S.selected && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        e.preventDefault();
        const i = S.visible.findIndex((m) => m.ticker === S.selected);
        const next = S.visible[i + (e.key === "ArrowDown" ? 1 : -1)];
        if (next) openDrawer(next.ticker);
      }
    });
    let resizeTimer = null;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawChart, 120); });
  }

  async function boot() {
    try {
      await loadSnapshot();
      setOnline(true);
      setTimeout(poll, POLL_MS);
      refreshSparks();
      setInterval(refreshSparks, 60_000);
    } catch (e) {
      setOnline(false, e);
      renderEmpty(0);
      setTimeout(boot, 2000);
    }
  }

  const savedSort = store.get("sort", null);
  if (savedSort && (savedSort.key === "game" || SORT_VALUE[savedSort.key])) S.sort = savedSort;
  S.range = RANGES.includes(store.get("range", "6h")) ? store.get("range", "6h") : "6h";
  if (store.get("tapeClosed", false)) { $("tape").classList.add("closed"); $("tape-toggle").setAttribute("aria-expanded", "false"); }
  bind();
  setInterval(tick, 1000);
  tick();
  boot();
})();
