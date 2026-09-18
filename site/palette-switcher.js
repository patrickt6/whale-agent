/* Whale Agent site: palette switcher.
   Floating control, bottom-right. Only renders when the page is loaded
   with a ?palette= query param, or when the page is palettes.html.
   Precedence for the palette actually applied: ?palette= (URL) > the
   value remembered in localStorage from a previous swatch click > no
   attribute (falls back to the :root defaults in style.css, i.e. "teal").

   The URL value is read-only: it is NOT written back to localStorage.
   palettes.html loads six copies of index.html at once, each with a
   different ?palette=, all on the same origin/localStorage; writing on
   load would make each iframe stomp the others' saved choice. Only a
   direct swatch click writes to localStorage. */
(function () {
  "use strict";

  var PALETTES = [
    { id: "teal", label: "Teal (current)" },
    { id: "whale-deep", label: "Whale Deep" },
    { id: "whale-light", label: "Whale Light" },
    { id: "abyss", label: "Abyss" },
    { id: "electric-teal", label: "Electric Teal" },
    { id: "deep-sea", label: "Deep Sea" }
  ];
  var STORAGE_KEY = "wa-palette";

  function getUrlPalette() {
    try {
      var params = new URLSearchParams(window.location.search);
      var p = params.get("palette");
      if (p && PALETTES.some(function (x) { return x.id === p; })) return p;
    } catch (e) { /* ignore */ }
    return null;
  }

  function getStoredPalette() {
    try {
      var p = window.localStorage.getItem(STORAGE_KEY);
      if (p && PALETTES.some(function (x) { return x.id === p; })) return p;
    } catch (e) { /* ignore */ }
    return null;
  }

  function storePalette(id) {
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch (e) { /* ignore: private mode, blocked storage, etc. */ }
  }

  function applyPalette(id) {
    if (id) {
      document.documentElement.dataset.palette = id;
    } else {
      document.documentElement.dataset.palette = "whale-deep";
    }
  }

  function isPalettesPage() {
    return /(^|\/)palettes\.html$/.test(window.location.pathname);
  }

  function shouldShowControl(urlPalette) {
    return Boolean(urlPalette) || isPalettesPage();
  }

  function buildControl(activeId) {
    var wrap = document.createElement("div");
    wrap.id = "wa-palette-switcher";
    wrap.setAttribute(
      "style",
      [
        "position:fixed", "right:16px", "bottom:16px", "z-index:9999",
        "display:flex", "gap:6px", "padding:8px",
        "background:rgba(0,0,0,.55)", "border-radius:8px",
        "backdrop-filter:blur(4px)", "font-family:system-ui,sans-serif"
      ].join(";")
    );

    PALETTES.forEach(function (p) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.title = p.label;
      btn.setAttribute("aria-label", p.label);
      btn.dataset.paletteSwatch = p.id;
      var swatchBg = getComputedSwatchColor(p.id);
      var isActive = p.id === activeId;
      btn.setAttribute(
        "style",
        [
          "width:22px", "height:22px", "border-radius:50%", "cursor:pointer",
          "padding:0", "background:" + swatchBg,
          "border:2px solid " + (isActive ? "#fff" : "rgba(255,255,255,.35)"),
          "box-shadow:0 1px 3px rgba(0,0,0,.4)"
        ].join(";")
      );
      btn.addEventListener("click", function () {
        applyPalette(p.id);
        storePalette(p.id);
        Array.prototype.forEach.call(
          wrap.querySelectorAll("button"),
          function (b) {
            b.style.border =
              "2px solid " +
              (b.dataset.paletteSwatch === p.id ? "#fff" : "rgba(255,255,255,.35)");
          }
        );
      });
      wrap.appendChild(btn);
    });

    return wrap;
  }

  /* Small fixed swatch-colour table so the control renders correct dots
     even before/without palettes.css having been read for computed values. */
  function getComputedSwatchColor(id) {
    var swatches = {
      "teal": "#006a62",
      "whale-deep": "#2e5696",
      "whale-light": "#6fa0e6",
      "abyss": "#141c2d",
      "electric-teal": "#037c6c",
      "deep-sea": "#0b4f6c"
    };
    return swatches[id] || "#888";
  }

  function init() {
    var urlPalette = getUrlPalette();
    var initial = urlPalette || getStoredPalette();
    applyPalette(initial);

    if (!shouldShowControl(urlPalette)) return;

    var render = function () {
      document.body.appendChild(buildControl(initial));
    };
    if (document.body) render();
    else document.addEventListener("DOMContentLoaded", render);
  }

  init();
})();
