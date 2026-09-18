/* Hero terminal: types two real whale commands and prints their output.
   Output copied from `./whale digest --demo` and `./whale doctor --env /nonexistent/.env`,
   trimmed. Filer name replaced with a fictional one. Respects prefers-reduced-motion. */
(function () {
  "use strict";
  var log = document.getElementById("hw-log");
  if (!log) return;
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var SESSION = [
    { cmd: "whale digest --demo", out: [
      "WHALE DIGEST, 2026-09-17",
      "Top move: Jane Q. Example: OPEN-MARKET BUY $7.5M in Acme Micro Corp.",
      "",
      "TIER 2: NOTABLE",
      "1. Jane Q. Example: OPEN-MARKET BUY $7.5M in Acme Micro Corp 🇺🇸 (US), 2026-07-24, 0-day lag",
      "   History: First time this filer appears for this issuer in our records.",
      "",
      "Every figure traces to a filing field. Awareness tool, not investment advice."
    ]},
    { cmd: "whale doctor", out: [
      "  FAIL email settings",
      "  ok   AI writer (gemini)",
      "  ok   source sec_form4",
      "  ok   source sec_13dg",
      "  warn source fmp_insider",
      "       fix: FMP_API_KEY is empty: set it, or switch the source off",
      "  ok   source taiwan_mops",
      "  warn schedule",
      "       fix: no automatic emails; run `whale schedule on`",
      "  1 blocking problem(s)"
    ]}
  ];

  function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function paint(line) {
    var h = esc(line);
    h = h.replace(/^(\s*)(ok)(\s)/, '$1<span class="k-ok">$2</span>$3')
         .replace(/^(\s*)(warn)(\s)/, '$1<span class="k-warn">$2</span>$3')
         .replace(/^(\s*)(FAIL)(\s)/, '$1<span class="k-fail">$2</span>$3')
         .replace(/(fix:)/, '<span class="k-dim">$1</span>')
         .replace(/`([^`]+)`/g, '<span class="k-cmd">`$1`</span>')
         .replace(/\b([A-Z][A-Z0-9_]{3,}_[A-Z0-9_]+)\b/g, '<span class="k-var">$1</span>')
         .replace(/(\$[0-9.]+M)/g, '<span class="k-money">$1</span>')
         .replace(/\b(\d{4}-\d{2}-\d{2})\b/g, '<span class="k-date">$1</span>')
         .replace(/(OPEN-MARKET BUY)/g, '<span class="k-buy">$1</span>');
    if (/^(WHALE DIGEST|TIER \d)/.test(line)) h = '<b class="k-head">' + h + "</b>";
    if (/^(Every figure|\s+History:)/.test(line)) h = '<span class="k-dim">' + h + "</span>";
    if (/blocking problem/.test(line)) h = '<span class="k-fail">' + h + "</span>";
    return h;
  }
  var PROMPT = '<span class="k-prompt">~/whale-agent</span> <span class="k-arrow">$</span> ';

  if (reduce) {
    log.innerHTML = SESSION.map(function (s) {
      return PROMPT + '<span class="k-typed">' + esc(s.cmd) + "</span>\n" + s.out.map(paint).join("\n");
    }).join("\n\n");
    return;
  }

  var html = "", step = 0;
  function render(extra) { log.innerHTML = html + (extra || "") + '<i class="hw-caret"></i>'; log.scrollTop = log.scrollHeight; }
  function runCmd() {
    var s = SESSION[step % SESSION.length];
    if (step % SESSION.length === 0) html = "";
    var n = 0;
    (function type() {
      render(PROMPT + '<span class="k-typed">' + esc(s.cmd.slice(0, n)) + "</span>");
      if (n++ < s.cmd.length) return setTimeout(type, 55 + Math.random() * 60);
      html += PROMPT + '<span class="k-typed">' + esc(s.cmd) + "</span>\n";
      var i = 0;
      setTimeout(function print() {
        if (i < s.out.length) { html += paint(s.out[i++]) + "\n"; render(); return setTimeout(print, 110); }
        html += "\n"; step++;
        render(PROMPT);
        setTimeout(runCmd, step % SESSION.length === 0 ? 4200 : 1400);
      }, 450);
    })();
  }
  render(PROMPT);
  setTimeout(runCmd, 700);
})();
/* Mail window: keep the email at 1:1 when it fits; scale it down only on narrow screens. */
(function () {
  var view = document.querySelector(".hw-mail-view");
  if (!view) return;
  var box = view.querySelector(".hw-mail-scale");
  function fit() {
    var w = view.clientWidth, s = Math.min(1, w / 660);
    box.style.transform = s < 1 ? "scale(" + s + ")" : "";
    view.style.height = s < 1 ? Math.round(470 * s) + "px" : "";
  }
  window.addEventListener("resize", fit);
  fit();
})();
