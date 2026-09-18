/* Animated comparison on checks.html. Gate constants live in the <template> blocks.
   Sources (whale-agent repo):
   price band 0.01 / 1,000,000   enrichment/plausibility.py:51-52 (used by ingestion/vendor_types.py:90)
   MAX_SHARE_COUNT 1e13          ingestion/vendor_types.py:41
   int64 sentinels               ingestion/vendor_types.py:36; plausibility.py:76-77 (SENTINEL_MAGNITUDE 2**62)
   ABSOLUTE_CEILING_USD 250B     plausibility.py:36; EGRESS_CEILING_USD = it, delivery/email.py:39
   MARKET_CAP_TOLERANCE 1.5      plausibility.py:41
   UNVERIFIED_CEILING_USD 25B    plausibility.py:60
   SHARES_OUTSTANDING_TOL 1.5    plausibility.py:64
   MAX_VALUE_DISCREPANCY 10      plausibility.py:69
   PLACEHOLDER_ISSUER_NAMES      plausibility.py:80
   figure check                  summarization/provenance.py:55,121,152; summarization/llm.py:424
   egress screen                 delivery/email.py:67
*/
(function () {
  var root = document.getElementById('ck-diagram');
  if (!root) return;
  var detail = document.getElementById('ck-detail');
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function show(n) {
    var tpl = document.getElementById('ck-gate-' + n);
    if (!tpl) return;
    detail.innerHTML = '';
    detail.appendChild(tpl.content.cloneNode(true));
    root.querySelectorAll('.ck-gate').forEach(function (g) {
      g.classList.toggle('is-active', g.getAttribute('data-gate') === String(n));
    });
  }
  root.querySelectorAll('.ck-gate').forEach(function (g) {
    var n = g.getAttribute('data-gate');
    g.addEventListener('mouseenter', function () { show(n); });
    g.addEventListener('focus', function () { show(n); });
    g.addEventListener('click', function () { show(n); });
    g.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); show(n); }
    });
  });

  // Stops for each lane: y positions of the node centres the token visits.
  var lanes = {
    trace: { stops: [40, 150, 260, 370, 480], end: 4 },
    gates: { stops: [40, 128], end: 1 }
  };

  function place(lane, y) { lane.querySelector('.ck-token').setAttribute('cy', y); }
  function hit(lane, i) {
    var nodes = lane.querySelectorAll('.ck-node');
    if (nodes[i]) nodes[i].classList.add('is-hit');
  }

  if (reduce) {
    root.classList.add('is-static');
    Object.keys(lanes).forEach(function (k) {
      var lane = root.querySelector('[data-lane="' + k + '"]');
      var s = lanes[k].stops;
      place(lane, s[s.length - 1]);
      for (var i = 0; i < s.length; i++) hit(lane, i);
    });
    return;
  }

  function run() {
    root.classList.remove('is-done');
    var t0 = null, step = 700;
    Object.keys(lanes).forEach(function (k) {
      var lane = root.querySelector('[data-lane="' + k + '"]');
      lane.querySelectorAll('.is-hit').forEach(function (n) { n.classList.remove('is-hit'); });
      place(lane, lanes[k].stops[0]);
    });
    function frame(ts) {
      if (t0 === null) t0 = ts;
      var t = (ts - t0) / step, done = true;
      Object.keys(lanes).forEach(function (k) {
        var lane = root.querySelector('[data-lane="' + k + '"]');
        var s = lanes[k].stops, i = Math.min(Math.floor(t), s.length - 1);
        var f = Math.min(t - i, 1);
        var y = i >= s.length - 1 ? s[s.length - 1] : s[i] + (s[i + 1] - s[i]) * f;
        place(lane, y);
        for (var j = 0; j <= i; j++) hit(lane, j);
        if (t < s.length - 1) done = false;
      });
      if (done) { root.classList.add('is-done'); setTimeout(run, 3500); return; }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  if ('IntersectionObserver' in window) {
    var started = false;
    new IntersectionObserver(function (es, obs) {
      if (!started && es[0].isIntersecting) { started = true; obs.disconnect(); run(); }
    }, { threshold: 0.3 }).observe(root);
  } else {
    run();
  }
})();
