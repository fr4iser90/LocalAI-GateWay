/** Live password checklist (length, classes, match). */
(function () {
  function q(sel, root) {
    return (root || document).querySelector(sel);
  }
  function qa(sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  }

  function bind(form) {
    var pw = q("[data-pw-new]", form);
    var pw2 = q("[data-pw-confirm]", form);
    var box = q("[data-pw-rules]", form);
    var submit = q("[data-pw-submit]", form) || q('button[type="submit"], input[type="submit"]', form);
    if (!pw || !box) return;

    var min = parseInt(box.getAttribute("data-pw-min") || "8", 10);
    var max = parseInt(box.getAttribute("data-pw-max") || "72", 10);

    function checks(v) {
      return {
        length: v.length >= min && v.length <= max,
        lower: /[a-z]/.test(v),
        upper: /[A-Z]/.test(v),
        digit: /\d/.test(v),
        symbol: /[^A-Za-z0-9]/.test(v),
        letter: /[A-Za-z]/.test(v),
      };
    }

    function setRow(key, ok) {
      var row = q('[data-pw-rule="' + key + '"]', box);
      if (!row) return;
      row.classList.toggle("is-ok", !!ok);
      row.classList.toggle("is-bad", !ok && (pw.value.length > 0 || key === "match"));
      var mark = q(".pw-rule-mark", row);
      if (mark) mark.textContent = ok ? "✓" : "·";
    }

    function refresh() {
      var v = pw.value || "";
      var c = checks(v);
      setRow("length", c.length);
      setRow("lower", c.lower);
      setRow("upper", c.upper);
      setRow("digit", c.digit);
      setRow("symbol", c.symbol);
      var match = !pw2 || ((pw2.value || "") === v && v.length > 0);
      setRow("match", match);

      var required =
        c.length && c.letter && c.digit && (!pw2 || match);
      if (submit) {
        if (submit.tagName === "INPUT") submit.disabled = !required;
        else submit.disabled = !required;
      }

      var meter = q("[data-pw-meter]", box);
      if (meter) {
        var score = [c.lower, c.upper, c.digit, c.symbol].filter(Boolean).length;
        if (!c.length) score = 0;
        meter.setAttribute("data-score", String(score));
        meter.style.setProperty("--pw-score", String(score));
        var label = q("[data-pw-meter-label]", box);
        if (label) {
          label.textContent =
            score <= 1 ? "Weak" : score === 2 ? "Fair" : score === 3 ? "Good" : "Strong";
        }
      }

      var count = q("[data-pw-count]", box);
      if (count) count.textContent = v.length + " / " + max;
    }

    pw.addEventListener("input", refresh);
    if (pw2) pw2.addEventListener("input", refresh);
    refresh();
  }

  document.addEventListener("DOMContentLoaded", function () {
    qa("form[data-pw-form]").forEach(bind);
  });
})();
