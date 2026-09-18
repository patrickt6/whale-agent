/* Renders the sample daily table. No framework, no build step.
   Reads window.WHALE_SAMPLE, set by data/sample-filings.js (loaded before this file).
   A script tag works from file:// and from any static host, so there is one copy only. */

(function () {
  "use strict";

  var mount = document.getElementById("filings-table");
  if (!mount) return;

  function esc(s) {
    return String(s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function num(n) {
    return Number(n).toLocaleString("en-US");
  }

  function usdExact(n) {
    return "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 2 });
  }

  function actionClass(row) {
    if (row.status !== "included") return "";
    if (row.action.indexOf("SELL") > -1) return "sell";
    if (row.action.indexOf("BUY") > -1) return "buy";
    return "";
  }

  function usdClass(row) {
    if (row.usd_value === null) return "none";
    if (row.status !== "included") return "none";
    return row.action.indexOf("SELL") > -1 ? "neg" : "pos";
  }

  var STATUS_LABEL = {
    included: "sent",
    below_threshold: "under $5M",
    not_priced: "unpriced",
    withheld: "withheld"
  };

  /* The plain-language note. Every figure here reads a field off the row. */
  function noteHtml(row) {
    var who = row.filer_name + (row.filer_role ? ", " + row.filer_role + "," : "");
    var verb = row.action.indexOf("SELL") > -1 ? "sold" : "bought";
    var sentences = [];

    if (row.status === "included") {
      if (row.action === "ACTIVIST 13D") {
        sentences.push(
          esc(row.filer_name) + " disclosed a stake of " + esc(row.percent_of_company) +
          "% in " + esc(row.issuer_name) + " on " + esc(row.disclosure_date) +
          ", worth " + esc(row.usd_display) + " at " + usdExact(row.price_used) +
          " a share. A 13D says the filer intends to influence the company, which a passive holder does not file."
        );
        sentences.push(
          "It is in the email because " + esc(row.usd_display) + " is above the " +
          esc(DATA.threshold_display) + " threshold" +
          (row.usd_is_estimate ? ", and because the filer declared activist intent. The dollar figure is an estimate: the price came from the closing price on the filing date, not from the filing itself." : ", and because the filer declared activist intent.")
        );
      } else {
        sentences.push(
          esc(who) + " " + verb + " " + num(row.share_count) + " shares of " +
          esc(row.issuer_name) + (row.ticker ? " (" + esc(row.ticker) + ")" : "") +
          " at " + usdExact(row.price_used) + " a share on " + esc(row.transaction_date) +
          ". That is " + esc(row.usd_display) + "."
        );
        sentences.push(
          "The filing was published on " + esc(row.disclosure_date) + ", " +
          (row.lag_days === 0 ? "the same day" : row.lag_days + " day" + (row.lag_days === 1 ? "" : "s") + " later") +
          ". It is in the email because " + esc(row.usd_display) + " is above the " +
          esc(DATA.threshold_display) + " threshold."
        );
      }
    } else if (row.status === "not_priced") {
      sentences.push(
        esc(row.filer_name) + " is reported holding " + num(row.share_count) + " shares of " +
        esc(row.issuer_name) + " (" + esc(row.ticker) + "), which is " +
        esc(row.percent_of_company) + "% of the company, disclosed on " + esc(row.disclosure_date) + "."
      );
      sentences.push(esc(row.exclusion_reason));
      if (row.as_filed) {
        var af = row.as_filed;
        sentences.push(
          'As filed: <span lang="' + esc(af.lang) + '">' + esc(af.filer_name) +
          (af.filer_role ? " (" + esc(af.filer_role) + ")" : "") + ", " + esc(af.issuer_name) +
          "</span>. " + esc(row.name_note || "")
        );
      }
    } else {
      sentences.push(
        esc(who) + " " + verb + " " + num(row.share_count) + " shares of " +
        esc(row.issuer_name) + (row.ticker ? " (" + esc(row.ticker) + ")" : "") +
        " on " + esc(row.transaction_date) + "."
      );
      sentences.push(esc(row.exclusion_reason));
    }

    var dl = [];
    dl.push(["Shares", num(row.share_count)]);
    if (row.price_used !== null) {
      dl.push(["Price a share", usdExact(row.price_used) + " (" + esc(row.price_source) + ")"]);
    }
    if (row.usd_value !== null) {
      dl.push(["USD value", usdExact(row.usd_value) + (row.usd_is_estimate ? " (estimate)" : "")]);
    }
    if (row.percent_of_company !== null) {
      dl.push(["Share of company", esc(row.percent_of_company) + "%"]);
    }
    dl.push(["Filed in", esc(row.native_currency) + " / " + esc(row.jurisdiction)]);
    if (row.why) dl.push(["Why it ranked", esc(row.why)]);
    dl.push(["Trading pattern", esc(row.routine_label)]);

    var dlHtml = dl.map(function (p) {
      return "<dt>" + p[0] + "</dt><dd>" + p[1] + "</dd>";
    }).join("");

    return (
      '<div class="note">' +
      "<h4>" + esc(row.filer_name) + " and " + esc(row.issuer_name) + "</h4>" +
      sentences.map(function (s) { return "<p>" + s + "</p>"; }).join("") +
      "<dl>" + dlHtml + "</dl>" +
      '<p class="src">Source: ' + esc(row.filing_type) + ", disclosed " + esc(row.disclosure_date) +
      ". Sample row built from " + esc(row.fixture) + " in the repository.</p>" +
      "</div>"
    );
  }

  function rowHtml(row, i) {
    var cls = "row" + (row.status === "included" ? "" : " excluded");
    return (
      '<tr class="' + cls + '" tabindex="0" role="button" aria-expanded="false" ' +
      'aria-controls="note-' + esc(row.id) + '" data-i="' + i + '">' +
      '<td class="cell-filer">' + esc(row.filer_name) +
      (row.filer_role ? '<span class="cell-role">' + esc(row.filer_role) + "</span>" : "") +
      "</td>" +
      '<td><span class="pill ' + actionClass(row) + '">' + esc(row.action) + "</span></td>" +
      '<td class="cell-issuer">' + esc(row.issuer_name) +
      (row.ticker ? ' <span class="cell-ticker">' + esc(row.ticker) + "</span>" : "") +
      "</td>" +
      '<td class="cell-usd ' + usdClass(row) + '">' + esc(row.usd_display) + "</td>" +
      '<td class="cell-ticker">' + esc(row.jurisdiction) + "</td>" +
      '<td class="cell-date">' + esc(row.disclosure_date) +
      '<span class="cell-lag">' + row.lag_days + "-day lag</span></td>" +
      '<td class="cell-ticker">' + esc(STATUS_LABEL[row.status]) + "</td>" +
      '<td class="chev" aria-hidden="true">+</td>' +
      "</tr>" +
      '<tr class="note-row" id="note-' + esc(row.id) + '" hidden><td colspan="8">' +
      noteHtml(row) + "</td></tr>"
    );
  }

  var DATA = null;

  function render(data) {
    DATA = data;
    var head =
      "<thead><tr>" +
      "<th>Filer</th><th>What</th><th>Issuer</th>" +
      '<th style="text-align:right">USD size</th>' +
      "<th>Where</th><th>Disclosed</th><th>Outcome</th><th></th>" +
      "</tr></thead>";
    var body = "<tbody>" + data.rows.map(rowHtml).join("") + "</tbody>";
    mount.innerHTML =
      '<div class="table-scroll"><table class="filings">' + head + body + "</table></div>";

    mount.addEventListener("click", function (e) {
      var tr = e.target.closest("tr.row");
      if (tr) toggle(tr);
    });
    mount.addEventListener("keydown", function (e) {
      if (e.key !== "Enter" && e.key !== " ") return;
      var tr = e.target.closest("tr.row");
      if (!tr) return;
      e.preventDefault();
      toggle(tr);
    });
  }

  function toggle(tr) {
    var note = document.getElementById(tr.getAttribute("aria-controls"));
    var open = tr.getAttribute("aria-expanded") === "true";
    tr.setAttribute("aria-expanded", open ? "false" : "true");
    note.hidden = open;
    tr.querySelector(".chev").textContent = open ? "+" : "−";
  }

  if (window.WHALE_SAMPLE) {
    render(window.WHALE_SAMPLE);
  } else {
    mount.innerHTML = '<p class="table-hint">The sample data could not be loaded.</p>';
  }
})();
