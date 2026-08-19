(function () {
  function dismiss(el) {
    el.classList.add("is-out");
    window.setTimeout(function () {
      el.remove();
    }, 200);
  }

  document.querySelectorAll("[data-toast]").forEach(function (el) {
    el.classList.add("is-in");
    var btn = el.querySelector("[data-toast-dismiss]");
    if (btn) {
      btn.addEventListener("click", function () {
        dismiss(el);
      });
    }
    window.setTimeout(function () {
      if (el.isConnected) dismiss(el);
    }, 4000);
  });
})();
