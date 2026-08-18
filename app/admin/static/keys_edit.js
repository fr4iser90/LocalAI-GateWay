/** Keys table: Edit expands under the row (one at a time), like Users grants. */
(function () {
  var table = document.querySelector(".keys-table");
  if (!table) return;

  var openKey = null;
  var loading = false;

  function collapse() {
    var row = table.querySelector(".key-expand-row");
    if (row) row.remove();
    table.querySelectorAll("[data-key-edit].is-active").forEach(function (btn) {
      btn.classList.remove("is-active");
    });
    openKey = null;
    if (history.replaceState) history.replaceState(null, "", "/keys");
  }

  function expand(keyId) {
    if (!keyId || loading) return;
    if (String(openKey) === String(keyId)) {
      collapse();
      return;
    }
    var keyRow = document.getElementById("key-row-" + keyId);
    if (!keyRow) return;

    loading = true;
    fetch("/keys/" + keyId + "/partial", {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    })
      .then(function (res) {
        if (!res.ok) throw new Error("key partial " + res.status);
        return res.text();
      })
      .then(function (html) {
        var wrap = document.createElement("tbody");
        wrap.innerHTML = String(html).trim();
        var expandRow = wrap.querySelector("tr") || wrap.firstElementChild;
        if (!expandRow) throw new Error("empty key partial");
        var old = table.querySelector(".key-expand-row");
        if (old) old.remove();
        table.querySelectorAll("[data-key-edit].is-active").forEach(function (btn) {
          btn.classList.remove("is-active");
        });
        keyRow.after(expandRow);
        var editBtn = keyRow.querySelector("[data-key-edit]");
        if (editBtn) editBtn.classList.add("is-active");
        openKey = keyId;
        if (window.initModelPickers) window.initModelPickers(expandRow);
        if (expandRow.scrollIntoView) {
          expandRow.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
        if (history.replaceState) {
          history.replaceState(null, "", "/keys?edit=" + keyId);
        }
      })
      .catch(function () {
        collapse();
      })
      .finally(function () {
        loading = false;
      });
  }

  table.addEventListener("click", function (ev) {
    var collapseBtn = ev.target.closest("[data-key-collapse]");
    if (collapseBtn) {
      ev.preventDefault();
      collapse();
      return;
    }
    if (ev.target.closest(".key-expand-row")) return;
    var link = ev.target.closest("[data-key-edit]");
    if (!link || !table.contains(link)) return;
    ev.preventDefault();
    var keyId = link.getAttribute("data-key-edit");
    if (keyId) expand(keyId);
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && openKey != null) collapse();
  });

  var params = new URLSearchParams(window.location.search);
  var initial = params.get("edit");
  if (initial) expand(initial);
})();
