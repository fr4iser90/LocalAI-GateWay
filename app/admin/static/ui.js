(function () {
  document.querySelectorAll("[data-toast]").forEach(function (el) {
    el.classList.add("is-in");
    var btn = el.querySelector("[data-toast-dismiss]");
    if (btn) {
      btn.addEventListener("click", function () {
        el.classList.add("is-out");
        window.setTimeout(function () {
          el.remove();
        }, 220);
      });
    }
  });
})();
