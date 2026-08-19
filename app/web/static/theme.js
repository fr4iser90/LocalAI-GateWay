/** Theme: dark | light. Persists in localStorage gw_theme. */
(function () {
  var KEY = "gw_theme";

  function current() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }

  function apply(theme) {
    var t = theme === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem(KEY, t); } catch (e) {}
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.setAttribute("aria-label", t === "light" ? "Switch to dark mode" : "Switch to light mode");
      btn.textContent = t === "light" ? "Dark" : "Light";
    });
  }

  function boot() {
    var saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) {}
    if (saved !== "light" && saved !== "dark") {
      try {
        saved = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
      } catch (e) {
        saved = "dark";
      }
    }
    apply(saved);
  }

  boot();

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-theme-toggle]");
    if (!btn) return;
    ev.preventDefault();
    apply(current() === "light" ? "dark" : "light");
  });
})();
