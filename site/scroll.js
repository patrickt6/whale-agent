// Smooth scroll via Lenis (https://github.com/darkroomengineering/lenis, MIT),
// loaded from jsdelivr before this file. Off entirely for prefers-reduced-motion,
// where the plain CSS scroll-behavior in style.css still applies.
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || typeof Lenis === "undefined") return;

  var lenis = new Lenis({ duration: 1.05, smoothWheel: true });
  function raf(time) {
    lenis.raf(time);
    requestAnimationFrame(raf);
  }
  requestAnimationFrame(raf);

  document.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener("click", function (e) {
      var id = a.getAttribute("href").slice(1);
      var el = id && document.getElementById(id);
      if (!el) return;
      e.preventDefault();
      lenis.scrollTo(el, { offset: -70 });
    });
  });
})();
