/* Hero mail client (scales the real email to fit, slides the new email in)
   and the pipeline walk-through. Respects prefers-reduced-motion. */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- Mail client: scale the 640px email into the reading pane ---- */
  var pane = document.getElementById("mc-read");
  if (pane) {
    var frame = pane.querySelector("iframe");
    var EMAIL_W = 660, CROP = 12;
    var fit = function () {
      var s = pane.clientWidth / EMAIL_W;
      frame.style.width = (EMAIL_W + 2 * CROP) + "px";
      frame.style.transform = "translateX(" + (-CROP * s) + "px) scale(" + s + ")";
      try {
        var t = frame.contentDocument.querySelector("table"); var h = t ? t.offsetHeight : 0;
        if (h) { frame.style.height = h + "px"; pane.style.height = Math.max(300, Math.round(h * s)) + "px"; }
      } catch (e) { /* cross-origin: keep CSS height */ }
    };
    frame.addEventListener("load", fit);
    window.addEventListener("resize", fit);
    fit();

    var client = document.getElementById("mailclient");
    if (!reduce) {
      client.classList.add("pre");
      setTimeout(function () { client.classList.remove("pre"); client.classList.add("arrive"); }, 900);
    }
  }

  /* ---- Raw filing cards fade in one after another ---- */
  var cards = Array.prototype.slice.call(document.querySelectorAll(".raw-card"));
  if (reduce || !("IntersectionObserver" in window)) {
    cards.forEach(function (c) { c.classList.add("seen"); });
  } else {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (!e.isIntersecting) return;
        cards.forEach(function (c, k) { setTimeout(function () { c.classList.add("seen"); }, k * 180); });
        io.disconnect();
      });
    }, { threshold: .2 });
    if (cards[0]) io.observe(cards[0]);
  }

  /* ---- Pipeline: a packet walks node to node ---- */
  var pipe = document.getElementById("pipe");
  if (!pipe) return;
  var nodes = Array.prototype.slice.call(pipe.querySelectorAll(".pipe-nodes li"));
  var say = document.getElementById("pipe-say");
  var packet = document.createElement("i");
  packet.className = "packet";
  packet.setAttribute("aria-hidden", "true");
  pipe.querySelector(".pipe-nodes").appendChild(packet);

  function place(i) {
    var n = nodes[i];
    var narrow = window.innerWidth <= 860;
    var x = narrow ? 38 : n.offsetLeft + n.offsetWidth / 2;
    var y = narrow ? n.offsetTop + n.offsetHeight / 2 : n.offsetTop + n.offsetHeight;
    packet.style.transform = "translate(" + x + "px," + y + "px)";
  }

  if (reduce) {
    nodes.forEach(function (n) { n.classList.add("on"); });
    packet.hidden = true;
    return;
  }

  var i = 0, run = 0;
  function step() {
    nodes.forEach(function (n, k) { n.classList.toggle("on", k === i); n.classList.toggle("done", k < i); });
    /* every second pass, the bad vendor row is refused at Check */
    var refuse = run % 2 === 1 && nodes[i].querySelector("b").textContent === "Check";
    packet.classList.toggle("bad", run % 2 === 1);
    place(i);
    if (refuse) {
      nodes[i].classList.add("refused");
      say.textContent = "Refused: shares 40,000,000 x price 40,000,000 = $1.6 quadrillion. That is more than the whole world stock market, so the row is held back.";
      setTimeout(function () { nodes[i].classList.remove("refused"); i = 0; run++; step(); }, 3600);
      return;
    }
    say.textContent = nodes[i].getAttribute("data-say");
    var last = i === nodes.length - 1;
    setTimeout(function () { if (last) { i = 0; run++; } else { i++; } step(); }, last ? 2600 : 1900);
  }
  window.addEventListener("resize", function () { place(i); });
  step();
})();
