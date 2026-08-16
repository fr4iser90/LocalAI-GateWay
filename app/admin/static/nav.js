/** Mobile nav drawer + a11y helpers for the admin shell. */
(function () {
  var layout = document.querySelector(".layout");
  var toggle = document.querySelector("[data-nav-toggle]");
  var backdrop = document.querySelector("[data-nav-backdrop]");
  if (!layout || !toggle) return;

  function setOpen(open) {
    layout.classList.toggle("nav-open", open);
    document.body.classList.toggle("nav-open", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    if (backdrop) {
      backdrop.hidden = !open;
      backdrop.setAttribute("aria-hidden", open ? "false" : "true");
    }
  }

  toggle.addEventListener("click", function () {
    setOpen(!layout.classList.contains("nav-open"));
  });
  if (backdrop) {
    backdrop.addEventListener("click", function () {
      setOpen(false);
    });
  }
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && layout.classList.contains("nav-open")) {
      setOpen(false);
      toggle.focus();
    }
  });
  // Close drawer after navigating (same-document links)
  document.querySelectorAll(".sidebar .nav a").forEach(function (a) {
    a.addEventListener("click", function () {
      if (window.matchMedia("(max-width: 800px)").matches) setOpen(false);
    });
  });
})();
