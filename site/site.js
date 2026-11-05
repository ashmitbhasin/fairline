/* Fairline project site: fills the panels from data.js (aggregates written by board/export_site.py),
   runs the repo's devig and fee formulas in the calculator, and drives the kickoff clock from the
   scroll position of its pinned act. The scrollcraft engine handles the pan, the reveal, the counts
   and the pins; everything specific to this page lives here. */
(function () {
  'use strict';
  var D = window.FAIRLINE_DATA || null;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var MINUS = '−';
  var NS = 'http://www.w3.org/2000/svg';
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------- formatting ----------
  function int(n) { return n == null || !isFinite(n) ? 'n/a' : Math.round(n).toLocaleString('en-US'); }
  function signed(s) { return s.charAt(0) === '-' ? MINUS + s.slice(1) : s; }
  function cents(x, d) {
    if (x == null || !isFinite(x)) return 'n/a';
    return signed((x * 100).toFixed(d == null ? 1 : d)) + '¢';
  }
  function money(x) {
    if (x == null || !isFinite(x)) return 'n/a';
    var s = '$' + Math.abs(x).toFixed(2);
    return x < 0 ? MINUS + s : (x > 0 ? '+' + s : s);
  }
  function pct(x, d) { return x == null || !isFinite(x) ? 'n/a' : (x * 100).toFixed(d == null ? 1 : d) + '%'; }
  function svg(tag, attrs, text) {
    var el = document.createElementNS(NS, tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    if (text != null) el.textContent = text;
    return el;
  }
  var fDay = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' });
  var fWeek = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short' });
  var fWhen = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  function dayParts(s) { var p = s.split('-').map(Number); var t = Date.UTC(p[0], p[1] - 1, p[2], 12); return { d: fDay.format(t), w: fWeek.format(t) }; }
  function setF(name, text) { $$('[data-f="' + name + '"]').forEach(function (el) { el.textContent = text; }); }

  // ---------- 1 · desk ----------
  function fillDesk() {
    setF('as_of', D.as_of);
    setF('owner_name', D.owner.name);
    var counts = { messages: D.tape.messages, games: D.desk.games, markets: D.desk.markets, side_minutes: D.desk.side_minutes };
    Object.keys(counts).forEach(function (k) {
      var el = $('[data-f="' + k + '"]');
      if (!el || counts[k] == null) return;
      var target = int(counts[k]);
      el.textContent = reduce ? target : '0';
      el.setAttribute('data-sc-count', '0 ' + target);
      el.setAttribute('data-sc-count-ms', '1300');
    });
    var read = D.gates.filter(function (g) { // a verdict, not a look or a gate that can't be read
      return (g.reads || []).some(function (r) { return r.verdict === 'go' || r.verdict === 'no-go'; });
    }).length;
    setF('gates_read', String(read));
  }

  // ---------- 2 · tape ----------
  function fillTape() {
    var list = $('.tape__days');
    var cut = new Date(D.tape.cutover_ms);
    var cutDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(cut);
    var server = false;
    D.tape.days.forEach(function (row) {
      if (row.day === cutDay) server = true;
      var li = document.createElement('li');
      var parts = dayParts(row.day);
      var up = row.uptime;
      li.className = 'day' + (server && row.day !== cutDay ? ' day--server' : '') + (up == null ? ' day--none' : '') + (row.day === cutDay ? ' day--move' : '');
      var label = parts.w + ' ' + parts.d + ': ' + (up == null ? 'not measured' : pct(up, 1) + ' up');
      li.setAttribute('aria-label', label);
      li.title = label + (row.messages ? ', ' + int(row.messages) + ' messages' : '');
      li.innerHTML = '<span class="day__pct">' + (up == null ? '&nbsp;' : Math.round(up * 100) + '%') + '</span>' +
        '<span class="day__track"><span class="day__fill" style="height:' + (up == null ? 0 : Math.max(up * 100, 0.5)) + '%"></span></span>' +
        '<span class="day__d">' + parts.d.split(' ')[1] + '</span><span class="day__w">' + parts.d.split(' ')[0] + '</span>';
      if (row.day === cutDay) {
        var flag = document.createElement('span');
        flag.className = 'day__flag';
        flag.textContent = 'moved to the server, ' + fWhen.format(cut).split(', ')[1];
        li.appendChild(flag);
      }
      list.appendChild(li);
    });
    var s = D.tape.since_cutover;
    var g1b = D.gates.filter(function (g) { return g.id === 'G1b' && g.reads.length; }).map(function (g) { return g.reads[0]; })[0];
    var end = $('.tape__end');
    end.innerHTML = '<p class="tape__end-h">Since the move</p>' +
      '<p><span class="num">' + pct(s.uptime, 2) + '</span> of the time seen, over <span class="num">' + int(s.hours) + '</span> hours: ' +
      '<span class="num">' + int(s.blind_s) + '</span> seconds blind across <span class="num">' + int(s.outages) + '</span> reconnects, ' +
      '<span class="num">' + int(s.gaps) + '</span> sequence gaps.</p>' +
      '<p>The first read of G1, on Oct 26, failed at <span class="num">' + pct(D.gates[0].reads[0].uptime, 1) + '</span>: the laptop slept. ' +
      (g1b ? 'Over the first seven days on the server the NFL tape passed, <span class="num">' + pct(g1b.game_uptime, 2) +
        '</span> seen in game windows with no sequence gaps (gate G1b). G1 itself stays open: NBA has not been recorded.' : '') + '</p>';
  }

  // ---------- 3 · fair value: the same formulas as fairvalue/odds.py, devig.py and fees.py ----------
  function americanToProb(o) {
    if (!isFinite(o) || o === 0 || (o > -100 && o < 100)) throw new Error('American odds are at most -100 or at least +100.');
    return o < 0 ? -o / (-o + 100) : 100 / (o + 100);
  }
  function powerDevig(q) {
    var sum = q[0] + q[1];
    if (Math.abs(sum - 1) < 1e-12) return q.slice();
    var lo = 0.01, hi = 50;
    for (var i = 0; i < 200; i++) {
      var k = (lo + hi) / 2;
      if (Math.pow(q[0], k) + Math.pow(q[1], k) - 1 > 0) lo = k; else hi = k;
    }
    var kk = (lo + hi) / 2;
    return [Math.pow(q[0], kk), Math.pow(q[1], kk)];
  }
  function orderFee(price, n, coef) { // dollars per order, rounded up to the cent
    var raw = coef * n * price * (1 - price);
    return Math.ceil(Number((raw * 100).toFixed(9))) / 100;
  }
  var TAKER = 0.07, MAKER = 0.0175;
  function parseOdds(s) { return Number(String(s).trim().replace(/^\+/, '').replace(/[−–]/, '-')); }

  function feeChart(price) {
    var el = $('.fee__svg');
    el.textContent = '';
    var W = 520, H = 220, L = 44, R = 14, T = 14, B = 30;
    var x = function (p) { return L + p * (W - L - R); };
    var y = function (c) { return T + (H - T - B) * (1 - c / 2); }; // 0 to 2 cents
    [0, 0.5, 1, 1.5, 2].forEach(function (c) {
      el.appendChild(svg('line', { x1: L, x2: W - R, y1: y(c), y2: y(c), class: 'svg-grid' }));
      el.appendChild(svg('text', { x: L - 8, y: y(c) + 4, 'text-anchor': 'end', class: 'svg-lbl' }, c + '¢'));
    });
    [0, 0.25, 0.5, 0.75, 1].forEach(function (p) {
      el.appendChild(svg('text', { x: x(p), y: H - 10, 'text-anchor': 'middle', class: 'svg-lbl' }, Math.round(p * 100) + '¢'));
    });
    function curve(coef) {
      var d = '';
      for (var i = 0; i <= 100; i++) { var p = i / 100; d += (i ? 'L' : 'M') + x(p).toFixed(1) + ' ' + y(coef * p * (1 - p) * 100).toFixed(1); }
      return d;
    }
    el.appendChild(svg('path', { d: curve(TAKER), class: 'fee__taker' }));
    el.appendChild(svg('path', { d: curve(MAKER), class: 'fee__maker' }));
    el.appendChild(svg('text', { x: x(0.5), y: y(1.75) - 8, 'text-anchor': 'middle', class: 'svg-lbl svg-lbl--ink' }, 'Taker'));
    el.appendChild(svg('text', { x: x(0.5), y: y(0.4375) - 8, 'text-anchor': 'middle', class: 'svg-lbl' }, 'Maker'));
    if (price != null) el.appendChild(svg('circle', { cx: x(price), cy: y(TAKER * price * (1 - price) * 100), r: 5, class: 'fee__dot' }));
  }

  function runCalc() {
    var a = $('#calc-a'), b = $('#calc-b'), n = $('#calc-n'), err = $('.calc__err');
    var out = function (k, v) { $('[data-c="' + k + '"]').textContent = v; };
    [a, b, n].forEach(function (i) { i.removeAttribute('aria-invalid'); });
    try {
      var qa, qb;
      try { qa = americanToProb(parseOdds(a.value)); } catch (e) { a.setAttribute('aria-invalid', 'true'); throw e; }
      try { qb = americanToProb(parseOdds(b.value)); } catch (e) { b.setAttribute('aria-invalid', 'true'); throw e; }
      var contracts = Math.round(Number(n.value));
      if (!(contracts >= 1 && contracts <= 100000)) { n.setAttribute('aria-invalid', 'true'); throw new Error('Contracts runs from 1 to 100,000.'); }
      var fair = powerDevig([qa, qb]);
      var mult = qa / (qa + qb);
      var price = Math.min(0.99, Math.max(0.01, Math.round(fair[0] * 100) / 100)); // the nearest Kalshi tick
      var taker = orderFee(price, contracts, TAKER) / contracts;
      var maker = orderFee(price, contracts, MAKER) / contracts;
      out('implied', cents(qa, 1) + ' / ' + cents(qb, 1));
      out('margin', cents(qa + qb - 1, 2));
      out('fair', cents(fair[0], 2));
      out('spread', cents(fair[0] - mult, 2));
      out('taker', cents(taker, 2) + ' a contract');
      out('maker', cents(maker, 2) + ' a contract');
      out('breakeven', cents(fair[0] - taker, 2) + ' or less');
      err.hidden = true;
      feeChart(price);
    } catch (e) {
      err.textContent = e.message;
      err.hidden = false;
    }
  }

  // ---------- 4 · kickoff: the clock is the scroll position ----------
  var K = { rows: [], max: 2.5, lastLead: null, dwellSince: 0, linger: null, lingerBest: 0, dwell: {} };
  var CLOCK_IN = 0.08, CLOCK_OUT = 0.92; // authored silence before the clock starts; a hold at kickoff

  function kickSetup() {
    var k = D.kickoff;
    var byLead = {};
    k.rows.forEach(function (r) { byLead[r.lead_min] = r; });
    for (var lead = 175; lead >= 0; lead -= 5) K.rows.push(byLead[lead] || null);
    var peak = 0;
    K.rows.forEach(function (r) { if (r) peak = Math.max(peak, r.p90 * 100); });
    K.fee = k.taker_fee_at_50 * 100;
    K.max = Math.max(2, Math.ceil(Math.max(peak, K.fee) * 1.15 * 2) / 2);
    var el = $('.kick__svg');
    var W = 720, H = 360, L = 48, R = 18, T = 18, B = 34;
    K.geom = { W: W, H: H, L: L, R: R, T: T, B: B };
    K.x = function (m) { return L + (180 - m) / 180 * (W - L - R); };
    K.y = function (c) { return T + (H - T - B) * (1 - c / K.max); };
    for (var c = 0; c <= K.max + 1e-9; c += K.max > 4 ? 1 : 0.5) {
      el.appendChild(svg('line', { x1: L, x2: W - R, y1: K.y(c), y2: K.y(c), class: 'svg-grid' }));
      el.appendChild(svg('text', { x: L - 8, y: K.y(c) + 4, 'text-anchor': 'end', class: 'svg-lbl' }, (c % 1 ? c.toFixed(1) : c) + '¢'));
    }
    [180, 150, 120, 90, 60, 30, 0].forEach(function (m) {
      el.appendChild(svg('text', { x: K.x(m), y: H - 10, 'text-anchor': m === 180 ? 'start' : m === 0 ? 'end' : 'middle', class: 'svg-lbl' + (m % 60 ? ' tick-minor' : '') },
        m === 0 ? 'Kickoff' : 'T' + MINUS + (m >= 60 && m % 60 === 0 ? m / 60 + ' h' : m + ' min')));
    });
    el.appendChild(svg('line', { x1: L, x2: W - R, y1: K.y(K.fee), y2: K.y(K.fee), class: 'kick__fee' }));
    el.appendChild(svg('text', { x: W - R, y: K.y(K.fee) - 7, 'text-anchor': 'end', class: 'svg-lbl kick__fee-lbl' },
      'Taker fee at 50¢: ' + K.fee.toFixed(2) + '¢'));
    K.p90 = el.appendChild(svg('path', { class: 'kick__p90', d: '' }));
    K.median = el.appendChild(svg('path', { class: 'kick__median', d: '' }));
    K.p90lbl = el.appendChild(svg('text', { class: 'svg-lbl kick__p90-lbl', x: 0, y: 0 }, '90th percentile'));
    K.medlbl = el.appendChild(svg('text', { class: 'svg-lbl svg-lbl--ink', x: 0, y: 0 }, 'Median'));
    K.head = el.appendChild(svg('line', { class: 'kick__head', y1: T, y2: H - B, x1: K.x(180), x2: K.x(180) }));
    K.dot = el.appendChild(svg('circle', { class: 'kick__dot', r: 5, cx: -20, cy: -20 }));
    K.empty = el.appendChild(svg('text', { class: 'kick__empty', x: (W + L) / 2, y: H / 2, 'text-anchor': 'middle' }, 'Three hours out. Nothing measured yet.'));
    K.el = $('#kickoff');
    K.clock = $('[data-k="clock"]');
    K.status = $('[data-k="status"]');
    var all = 0, beyond = 0, medSum = 0, medN = 0;
    K.rows.forEach(function (r) { if (r) { all += r.minutes; beyond += r.qualifying; medSum += r.median * r.minutes; medN += r.minutes; } });
    K.total = { minutes: all, qualifying: beyond, median: medN ? medSum / medN : null };
  }

  function bucketAt(m) { // m minutes to kickoff -> row index 0 (175-180) .. 35 (0-5)
    var i = 35 - Math.min(35, Math.floor(Math.max(0, m - 1e-9) / 5));
    return Math.max(0, Math.min(35, i));
  }

  function statusFor(m, p) {
    if (p < CLOCK_IN) return 'Three hours to kickoff. Scroll to run the clock.';
    if (p >= CLOCK_OUT) {
      var t = K.total, under = t.median != null && t.median * 100 < K.fee;
      return 'Kickoff. The replay stops here. Over the last three hours the typical gap was ' + cents(t.median, 2) +
        (under ? ', under the fee a taker pays at 50¢.' : ', above the fee a taker pays at 50¢.');
    }
    if (m > 120) return 'From three hours out, the poller asks Pinnacle for its line every two minutes.';
    if (m > 60) return 'A Pinnacle price counts from the moment it arrives, and goes stale after five minutes.';
    if (m > 15) return 'Minutes when the recorder was blind, or its book was crossed, are skipped.';
    return 'The last minutes before the snap.';
  }

  function pathUpTo(m, key) {
    var d = '', started = false, lastX = null, lastY = null;
    for (var i = 0; i < 36; i++) {
      var r = K.rows[i];
      var center = 175 - i * 5 + 2.5;
      if (center < m) break;
      if (!r) { started = false; continue; }
      var x = K.x(center), yv = K.y(r[key] * 100);
      d += (started ? 'L' : 'M') + x.toFixed(1) + ' ' + yv.toFixed(1);
      started = true; lastX = x; lastY = yv;
    }
    return { d: d, x: lastX, y: lastY };
  }

  function kickFrame() {
    var p = parseFloat(K.el.style.getPropertyValue('--sc-p')) || 0;
    var u = Math.min(1, Math.max(0, (p - CLOCK_IN) / (CLOCK_OUT - CLOCK_IN)));
    var m = p < CLOCK_IN ? 180 : 180 * (1 - u); // minutes to kickoff
    var secs = Math.round(m * 60);
    var hh = Math.floor(secs / 3600), mm = Math.floor(secs % 3600 / 60), ss = secs % 60;
    K.clock.textContent = hh + ':' + String(mm).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
    K.el.classList.toggle('kick--zero', p >= CLOCK_OUT);
    var status = statusFor(m, p);
    if (K.status.textContent !== status) K.status.textContent = status;
    var med = pathUpTo(m, 'median'), p90 = pathUpTo(m, 'p90');
    K.median.setAttribute('d', med.d);
    K.p90.setAttribute('d', p90.d);
    K.empty.style.opacity = med.d ? 0 : 1;
    var x = K.x(m);
    K.head.setAttribute('x1', x); K.head.setAttribute('x2', x);
    if (med.x != null) {
      K.dot.setAttribute('cx', med.x); K.dot.setAttribute('cy', med.y);
      var flip = med.x > K.geom.W - K.geom.R - 130; // near the right edge the labels sit left of the line
      var dx = flip ? -10 : 8, anchor = flip ? 'end' : 'start';
      K.medlbl.setAttribute('x', med.x + dx); K.medlbl.setAttribute('y', med.y + 16); K.medlbl.setAttribute('text-anchor', anchor);
      K.p90lbl.setAttribute('x', p90.x + dx); K.p90lbl.setAttribute('y', p90.y - 9); K.p90lbl.setAttribute('text-anchor', anchor);
    } else {
      K.dot.setAttribute('cx', -20);
      K.medlbl.setAttribute('x', -200); K.p90lbl.setAttribute('x', -200);
    }
    var row = p < CLOCK_IN ? null : K.rows[bucketAt(m)];
    var set = function (k, v) { var el = $('[data-k="' + k + '"]'); if (el.textContent !== v) el.textContent = v; };
    set('median', row ? cents(row.median, 2) : 'n/a');
    set('p90', row ? cents(row.p90, 2) : 'n/a');
    set('beyond', row ? pct(row.minutes ? row.qualifying / row.minutes : null, 1) : 'n/a');
    set('n', row ? int(row.minutes) : 'n/a');
    trackLinger(m, p);
  }

  // Where the visitor stopped the clock longest is carried into the note at the end.
  function trackLinger(m, p) {
    var now = performance.now();
    var lead = p > CLOCK_IN && p < CLOCK_OUT ? Math.round(m) : null;
    if (lead !== K.lastLead) { K.lastLead = lead; K.dwellSince = now; return; }
    if (lead == null) return;
    var held = now - K.dwellSince;
    if (held > 900 && held > K.lingerBest) { K.lingerBest = held; K.linger = lead; prefillNote(); }
  }

  function kickLoop() {
    var visible = false;
    var io = new IntersectionObserver(function (es) { visible = es[0].isIntersecting; }, { rootMargin: '20% 0px' });
    io.observe(K.el);
    (function frame() {
      if (visible) kickFrame();
      requestAnimationFrame(frame);
    })();
    kickFrame();
  }

  // ---------- 5 · record ----------
  function ruleName(v) { return (v.rule === 'take' ? 'Take at ' : 'Quote at ') + (v.param * 100).toFixed(v.param * 100 % 1 ? 1 : 0) + '¢'; }
  function fillRecord() {
    var n = D.record.naive;
    $('[data-r="naive_pnl"]').textContent = money(n.pnl);
    $('[data-r="naive_line"]').textContent = 'Take at 2¢: ' + n.won + ' won, ' + n.lost + ' lost, on the replay of ' + dayParts(n.day).d + '.';
    var body = $('[data-r="ladder"]');
    D.record.variants.forEach(function (v) {
      var tr = document.createElement('tr');
      if (v.primary) tr.className = 'primary';
      var clv = v.clv_mean == null ? '<span class="dim">none yet</span>' :
        '<span class="' + (v.clv_mean < 0 ? 'neg' : '') + '">' + cents(v.clv_mean, 1) + '</span>' +
        (v.clv_ci != null ? '<span class="dim"> ± ' + cents(v.clv_ci, 1).replace(MINUS, '') + '</span>' : '');
      var wl = v.settled ? v.won + '–' + v.lost + (v.tied ? '–' + v.tied : '') : '<span class="dim">none settled</span>';
      tr.innerHTML = '<td class="l">' + ruleName(v) + (v.primary ? ' <span class="dim">(headline)</span>' : '') + '</td>' +
        '<td>' + int(v.bets) + '</td><td>' + wl + '</td>' +
        '<td class="' + (v.pnl < 0 ? 'neg' : '') + '">' + (v.settled ? money(v.pnl) : '<span class="dim">n/a</span>') + '</td><td>' + clv + '</td>';
      body.appendChild(tr);
    });
    var p = D.record.params || {};
    $('[data-r="foot"]').textContent = 'Up to ' + int(p.size) + ' contracts a bet, held to settlement. Closing-line value is Pinnacle’s last fair price before kickoff minus the price paid, fee included, with a 95% interval. A negative mean means the price moved against the bet. The headline rows are the 2¢ rules; the others show where an edge would have to appear.';
  }

  // ---------- 6 · gates ----------
  var fDate = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' });
  function dateLabel(s) { var p = s.split('-').map(Number); return fDate.format(Date.UTC(p[0], p[1] - 1, p[2], 12)); }
  function tidy(s) { // the registry is written for a terminal: >= 2c reads as \u2265 2\u00a2 here
    return String(s).replace(/>=/g, '\u2265').replace(/<=/g, '\u2264').replace(/(\d)c\b/g, '$1\u00a2');
  }
  function fillGates() {
    var body = $('[data-g="rows"]');
    D.gates.forEach(function (g) {
      var tr = document.createElement('tr');
      var read;
      if (g.reads && g.reads.length) {
        var r = g.reads[g.reads.length - 1];
        var label = r.label || (r.verdict === 'go' ? 'Go, ' + pct(r.game_uptime, 2) + ' in game windows' : 'No-go, ' + pct(r.uptime, 1) + ' uptime');
        var cls = r.verdict === 'go' ? ' verdict--go' : r.verdict === 'no-go' ? ' verdict--no' : '';
        read = '<span class="when">' + dateLabel(r.day) + '</span><span class="verdict' + cls + '">' + label + '</span>' +
          (g.next_read_ms ? '<span class="verdict">Read after ' + fWhen.format(new Date(g.next_read_ms)) + '</span>' : '');
      } else {
        read = '<span class="when">' + (g.earliest_read ? dateLabel(g.earliest_read) : 'n/a') + '</span><span class="verdict">Not read yet</span>';
      }
      tr.innerHTML = '<td><span class="gate-id">' + g.id + '</span><span class="gate-name"></span></td><td class="gate-q"></td><td class="gate-bar"></td><td class="gate-read">' + read + '</td>';
      tr.querySelector('.gate-name').textContent = g.name + (g.registered > '2026-10-06' ? ', added ' + dateLabel(g.registered) : '');
      tr.querySelector('.gate-q').textContent = tidy(g.hypothesis);
      tr.querySelector('.gate-bar').textContent = tidy(g.go);
      body.appendChild(tr);
    });
  }

  // ---------- 7 · email ----------
  function prefillNote() {
    var box = $('#note-body');
    if (box.dataset.touched) return;
    box.value = 'Hi ' + D.owner.name.split(' ')[0] + ',\n\nI read about Fairline' +
      (K.linger != null ? ' and stopped the kickoff clock around T' + MINUS + K.linger + ' minutes' : '') + '.\n\n';
  }
  function fillNote() {
    var form = $('.note__form'), box = $('#note-body'), link = $('[data-f="email_link"]');
    form.setAttribute('action', 'mailto:' + D.owner.email);
    link.href = 'mailto:' + D.owner.email;
    link.textContent = D.owner.email;
    box.addEventListener('input', function () { box.dataset.touched = '1'; });
    prefillNote();
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      location.href = 'mailto:' + D.owner.email + '?subject=' + encodeURIComponent('Fairline') + '&body=' + encodeURIComponent(box.value);
    });
  }

  // ---------- chrome: the tab for the section in view ----------
  function trackTabs() { // the tab of the section crossing the middle of the screen, exactly one
    var tabs = $$('.chrome__tabs a'), current = null, queued = false;
    var sections = tabs.map(function (a) { return document.getElementById(a.getAttribute('href').slice(1)); });
    function update() {
      queued = false;
      var mid = innerHeight * 0.5, pick = 0;
      sections.forEach(function (s, i) { if (s && s.getBoundingClientRect().top <= mid) pick = i; });
      if (pick === current) return;
      current = pick;
      tabs.forEach(function (a, i) { if (i === pick) a.setAttribute('aria-current', 'location'); else a.removeAttribute('aria-current'); });
    }
    addEventListener('scroll', function () { if (!queued) { queued = true; requestAnimationFrame(update); } }, { passive: true });
    addEventListener('resize', update);
    update();
  }

  function boot() {
    if (!D) {
      document.body.insertAdjacentHTML('afterbegin', '<p class="noscript">site/data.js is missing: run python -m board.export_site.</p>');
      return;
    }
    fillDesk();
    fillTape();
    fillRecord();
    fillGates();
    fillNote();
    ['#calc-a', '#calc-b', '#calc-n'].forEach(function (s) { $(s).addEventListener('input', runCalc); });
    $('#calc').addEventListener('submit', function (e) { e.preventDefault(); });
    runCalc();
    kickSetup();
    trackTabs();
    window.ScrollCraft.mount(document.body); // after the panels exist, so the pan measures the real rail
    kickLoop();
  }
  boot();
})();
