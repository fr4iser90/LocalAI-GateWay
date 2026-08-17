/** Model picker: source sidebar + filter + all/none (+ optional VL pairing). */
(function () {
  function visibleItems(scope) {
    return Array.prototype.slice
      .call(scope.querySelectorAll(".model-picker-item"))
      .filter(function (el) {
        return el.style.display !== "none" && !el.hidden;
      });
  }

  function activePane(root) {
    return root.querySelector(".model-picker-pane.is-active");
  }

  function setActiveSource(root, source) {
    root.querySelectorAll("[data-nav-source]").forEach(function (btn) {
      var on = btn.getAttribute("data-nav-source") === source;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
    root.querySelectorAll(".model-picker-pane").forEach(function (pane) {
      pane.classList.toggle("is-active", pane.getAttribute("data-source") === source);
    });
  }

  function setItemEnabled(item, on) {
    item.hidden = !on;
    item.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      cb.disabled = !on;
    });
  }

  function applyVlCouple(root, on) {
    root.classList.toggle("is-vl-coupled", !!on);
    root.querySelectorAll('[data-mode="flat"]').forEach(function (el) {
      setItemEnabled(el, !on);
    });
    root.querySelectorAll('[data-mode="paired"]').forEach(function (el) {
      setItemEnabled(el, !!on);
      if (on) {
        var a = el.querySelector("[data-pair-a]");
        var b = el.querySelector("[data-pair-b]");
        var t = el.querySelector("[data-pair-toggle]");
        if (a && b && t) {
          var either = a.checked || b.checked;
          a.checked = either;
          b.checked = either;
          t.checked = either;
        }
      }
    });
    root.querySelectorAll('[data-mode="both"]').forEach(function (el) {
      setItemEnabled(el, true);
    });
    applyFilter(root);
  }

  function syncPair(el) {
    var a = el.querySelector("[data-pair-a]");
    var b = el.querySelector("[data-pair-b]");
    var t = el.querySelector("[data-pair-toggle]");
    if (!a || !b || !t) return;
    a.checked = t.checked;
    b.checked = t.checked;
  }

  function applyFilter(root) {
    var filter = root.querySelector(".model-picker-filter");
    var q = (filter && filter.value ? filter.value : "").trim().toLowerCase();

    root.querySelectorAll(".model-picker-pane").forEach(function (pane) {
      var n = 0;
      pane.querySelectorAll(".model-picker-item").forEach(function (item) {
        if (item.hidden) {
          item.style.display = "none";
          return;
        }
        var hay = item.getAttribute("data-search") || "";
        var show = !q || hay.indexOf(q) !== -1;
        item.style.display = show ? "" : "none";
        if (show) n += 1;
      });
      var empty = pane.querySelector(".model-picker-empty");
      if (empty) empty.hidden = n > 0;
      var countEl = pane.querySelector("[data-pane-visible]");
      if (countEl) countEl.textContent = "(" + n + ")";

      var source = pane.getAttribute("data-source");
      var nav = root.querySelector('[data-nav-source="' + source + '"]');
      if (nav) {
        var badge = nav.querySelector("[data-nav-count]");
        if (badge) badge.textContent = String(n);
        nav.classList.toggle("is-dimmed", q && n === 0);
      }
    });

    var active = activePane(root);
    if (active && q) {
      var activeCount = visibleItems(active).length;
      if (activeCount === 0) {
        var first = null;
        root.querySelectorAll(".model-picker-pane").forEach(function (pane) {
          if (!first && visibleItems(pane).length) first = pane;
        });
        if (first) setActiveSource(root, first.getAttribute("data-source"));
      }
    }
  }

  function linkSourceChips(root) {
    var pickerId = root.id.replace(/^picker-/, "");
    if (!pickerId) return;
    var chipRoot = document.querySelector('[data-source-chips-for="' + pickerId + '"]');
    if (!chipRoot) return;
    chipRoot.querySelectorAll("[data-source-chip]").forEach(function (chip) {
      chip.addEventListener("click", function () {
        var source = chip.getAttribute("data-source-chip");
        if (source) setActiveSource(root, source);
      });
    });
  }

  function bindPicker(root) {
    var filter = root.querySelector(".model-picker-filter");
    var allBtn = root.querySelector("[data-picker-all]");
    var noneBtn = root.querySelector("[data-picker-none]");

    linkSourceChips(root);

    root.querySelectorAll("[data-nav-source]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        setActiveSource(root, btn.getAttribute("data-nav-source"));
      });
    });

    if (filter) filter.addEventListener("input", function () {
      applyFilter(root);
    });

    function setVisibleChecked(checked) {
      var pane = activePane(root) || root;
      visibleItems(pane).forEach(function (item) {
        var toggle = item.querySelector("[data-pair-toggle]");
        if (toggle) {
          toggle.checked = checked;
          syncPair(item);
          return;
        }
        var cb = item.querySelector('input[type="checkbox"]:not([hidden])');
        if (!cb) cb = item.querySelector('input[type="checkbox"]');
        if (cb) cb.checked = checked;
      });
    }

    if (allBtn) allBtn.addEventListener("click", function () {
      setVisibleChecked(true);
    });
    if (noneBtn) noneBtn.addEventListener("click", function () {
      setVisibleChecked(false);
    });

    root.querySelectorAll("[data-group]").forEach(function (g) {
      var ga = g.querySelector("[data-group-all]");
      var gn = g.querySelector("[data-group-none]");
      if (ga) {
        ga.addEventListener("click", function () {
          visibleItems(g).forEach(function (item) {
            var toggle = item.querySelector("[data-pair-toggle]");
            if (toggle) {
              toggle.checked = true;
              syncPair(item);
              return;
            }
            var cb = item.querySelector('input[type="checkbox"]:not([hidden])');
            if (cb) cb.checked = true;
          });
        });
      }
      if (gn) {
        gn.addEventListener("click", function () {
          visibleItems(g).forEach(function (item) {
            var toggle = item.querySelector("[data-pair-toggle]");
            if (toggle) {
              toggle.checked = false;
              syncPair(item);
              return;
            }
            var cb = item.querySelector('input[type="checkbox"]:not([hidden])');
            if (cb) cb.checked = false;
          });
        });
      }
    });

    root.querySelectorAll("[data-pair]").forEach(function (el) {
      var t = el.querySelector("[data-pair-toggle]");
      if (t) t.addEventListener("change", function () { syncPair(el); });
    });

    if (root.hasAttribute("data-vl-picker")) {
      var couple = document.querySelector("[data-vl-couple]");
      if (couple) {
        applyVlCouple(root, couple.checked);
        couple.addEventListener("change", function () {
          applyVlCouple(root, couple.checked);
        });
      } else {
        applyVlCouple(root, false);
      }
    }

    root.querySelectorAll("[data-favorite-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        var item = btn.closest(".model-picker-item");
        var cb = item && item.querySelector(".model-picker-fav-cb");
        if (!cb) return;
        cb.checked = !cb.checked;
        btn.classList.toggle("is-on", cb.checked);
        btn.setAttribute("aria-pressed", cb.checked ? "true" : "false");
      });
    });
  }

  function init(scope) {
    (scope || document).querySelectorAll("[data-picker]").forEach(function (el) {
      if (el.getAttribute("data-picker-bound") === "1") return;
      el.setAttribute("data-picker-bound", "1");
      bindPicker(el);
    });
  }
  window.initModelPickers = init;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      init();
    });
  } else {
    init();
  }
})();