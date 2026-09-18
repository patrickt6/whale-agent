/* Draws the scale figures from window.WHALE_SCALE (data/scale.js).
   Every mark is one row of a query result in that file. No library. */
(function () {
  "use strict";
  var D = window.WHALE_SCALE;
  if (!D) return;
  var NS = "http://www.w3.org/2000/svg";
  var WINDOW_START = "2026-07-08";

  function el(name, attrs, parent) {
    var n = document.createElementNS(NS, name);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function fmt(n) { return Number(n).toLocaleString("en-US"); }
  function day(s) { return Date.parse(s + "T00:00:00Z") / 86400000; }
  function iso(d) { return new Date(d * 86400000).toISOString().slice(0, 10); }
  function nice(s) {
    var m = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    return +s.slice(8) + " " + m[+s.slice(5, 7) - 1];
  }

  /* One tooltip for the page */
  var tip = document.createElement("div");
  tip.className = "fig-tip";
  tip.setAttribute("role", "status");
  tip.hidden = true;
  document.body.appendChild(tip);
  function show(e, html) {
    tip.innerHTML = html;
    tip.hidden = false;
    var x = e.clientX + 14, y = e.clientY + 14;
    var w = tip.offsetWidth;
    if (x + w > window.innerWidth - 8) x = e.clientX - w - 14;
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function hide() { tip.hidden = true; }

  /* Daily series inside the dense window, with absent dates filled as 0
     (a date missing from a GROUP BY result has no rows). */
  function dense(series) {
    var map = {};
    series.forEach(function (p) { map[p[0]] = p[1]; });
    var a = day(WINDOW_START), b = day(series[series.length - 1][0]);
    var out = [];
    for (var d = a; d <= b; d++) out.push([iso(d), map[iso(d)] || 0]);
    return out;
  }

  var W = 300, H = 220, PL = 4, PR = 4, PT = 16, PB = 18;

  function axis(svg, pts, max) {
    el("line", { x1: PL, x2: W - PR, y1: H - PB + .5, y2: H - PB + .5, class: "fig-axis" }, svg);
    var t1 = el("text", { x: PL, y: H - 4, class: "fig-hint" }, svg); t1.textContent = nice(pts[0][0]);
    var t2 = el("text", { x: W - PR, y: H - 4, class: "fig-hint", "text-anchor": "end" }, svg); t2.textContent = nice(pts[pts.length - 1][0]) + " 2026";
    var t3 = el("text", { x: PL, y: 10, class: "fig-hint" }, svg); t3.textContent = "max " + fmt(max) + "/day";
  }

  function hoverBands(svg, pts, x, label) {
    var bw = (W - PL - PR) / pts.length;
    var mark = el("line", { class: "fig-cursor", y1: PT, y2: H - PB, x1: -9, x2: -9 }, svg);
    pts.forEach(function (p, i) {
      var r = el("rect", { x: x(i) - bw / 2, y: PT, width: bw, height: H - PT - PB, class: "fig-hit" }, svg);
      r.addEventListener("mousemove", function (e) {
        mark.setAttribute("x1", x(i)); mark.setAttribute("x2", x(i));
        show(e, "<b>" + fmt(p[1]) + "</b> " + label + "<br>" + p[0]);
      });
      r.addEventListener("mouseleave", function () { hide(); mark.setAttribute("x1", -9); mark.setAttribute("x2", -9); });
    });
  }

  function area(svg) {
    var pts = dense(D.events_per_day);
    var max = Math.max.apply(null, pts.map(function (p) { return p[1]; }));
    var x = function (i) { return PL + (i + .5) * (W - PL - PR) / pts.length; };
    var y = function (v) { return H - PB - v / max * (H - PT - PB); };
    var line = pts.map(function (p, i) { return (i ? "L" : "M") + x(i).toFixed(1) + " " + y(p[1]).toFixed(1); }).join(" ");
    el("path", { d: line + " L" + x(pts.length - 1).toFixed(1) + " " + (H - PB) + " L" + x(0).toFixed(1) + " " + (H - PB) + " Z", class: "fig-area" }, svg);
    el("path", { d: line, class: "fig-line" }, svg);
    axis(svg, pts, max);
    hoverBands(svg, pts, x, "events disclosed");
    var shown = pts.reduce(function (s, p) { return s + p[1]; }, 0);
    return shown;
  }

  function bars(svg) {
    var pts = dense(D.new_filers_per_day);
    var max = Math.max.apply(null, pts.map(function (p) { return p[1]; }));
    var bw = (W - PL - PR) / pts.length;
    var x = function (i) { return PL + (i + .5) * bw; };
    pts.forEach(function (p, i) {
      var h = p[1] / max * (H - PT - PB);
      el("rect", { x: x(i) - bw * .32, y: H - PB - h, width: bw * .64, height: h, class: "fig-bar" }, svg);
    });
    axis(svg, pts, max);
    hoverBands(svg, pts, x, "new filers first seen");
    return pts.reduce(function (s, p) { return s + p[1]; }, 0);
  }

  function grid(svg) {
    var cells = D.thirteenf_cells;
    var periods = {}, funds = {};
    cells.forEach(function (c) { periods[c[1]] = 1; funds[c[0]] = (funds[c[0]] || 0) + c[2]; });
    var P = Object.keys(periods).sort();
    var F = Object.keys(funds).sort(function (a, b) { return funds[b] - funds[a]; });
    var max = Math.max.apply(null, cells.map(function (c) { return c[2]; }));
    var gx = PL, gy = PT, gw = W - PL - PR, gh = H - PT - PB;
    var cw = gw / F.length, ch = gh / P.length;
    for (var r = 0; r < P.length; r++) for (var f = 0; f < F.length; f++) {
      el("rect", { x: gx + f * cw + .6, y: gy + r * ch + .8, width: cw - 1.2, height: ch - 1.6, class: "fig-empty" }, svg);
    }
    cells.forEach(function (c) {
      var f = F.indexOf(c[0]), r = P.indexOf(c[1]);
      var o = .25 + .75 * Math.log(1 + c[2]) / Math.log(1 + max);
      var rect = el("rect", { x: gx + f * cw + .6, y: gy + r * ch + .8, width: cw - 1.2, height: ch - 1.6, class: "fig-cell", "fill-opacity": o.toFixed(2) }, svg);
      rect.addEventListener("mousemove", function (e) {
        show(e, "<b>" + fmt(c[2]) + "</b> holding rows<br>fund CIK " + c[0] + "<br>period " + c[1]);
      });
      rect.addEventListener("mouseleave", hide);
    });
    var t1 = el("text", { x: PL, y: 10, class: "fig-hint" }, svg); t1.textContent = "rows: " + P[0].slice(0, 4) + " to " + P[P.length - 1].slice(0, 4);
    var t2 = el("text", { x: PL, y: H - 4, class: "fig-hint" }, svg); t2.textContent = "48 funds, largest first";
  }

  function jur(svg) {
    var rows = D.by_jurisdiction, total = D.totals.events;
    var names = { US: "United States", JP: "Japan", TW: "Taiwan" };
    var x = 0, gw = 1000;
    rows.forEach(function (r, i) {
      var w = Math.max(r[1] / total * gw, 3);
      var rect = el("rect", { x: x, y: 0, width: w - 2, height: 18, class: "fig-seg s" + i }, svg);
      rect.addEventListener("mousemove", function (e) { show(e, "<b>" + fmt(r[1]) + "</b> events<br>" + names[r[0]] + " (" + (r[1] / total * 100).toFixed(1) + "%)"); });
      rect.addEventListener("mouseleave", hide);
      x += w;
    });
  }

  var m;
  if ((m = document.getElementById("fig-events"))) {
    var shown = area(m);
    var s = document.getElementById("fig-events-shown");
    if (s) s.textContent = fmt(shown);
  }
  if ((m = document.getElementById("fig-holdings"))) grid(m);
  if ((m = document.getElementById("fig-filers"))) bars(m);
  if ((m = document.getElementById("fig-jur"))) jur(m);
})();
