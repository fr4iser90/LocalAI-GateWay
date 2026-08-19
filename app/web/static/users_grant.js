/** Users table: source badges (models) · + (add sources) · × (remove on form submit). */
(function () {
  var table = document.querySelector(".users-table");
  if (!table) return;

  var openUser = null;
  var openSource = null;
  var openMode = null;
  var loading = false;

  function clearChrome() {
    table.querySelectorAll(".badge-link--active").forEach(function (a) {
      a.classList.remove("badge-link--active");
    });
    table.querySelectorAll(".badge-add--active").forEach(function (btn) {
      btn.classList.remove("badge-add--active");
    });
  }

  function collapse() {
    var row = table.querySelector(".grant-expand-row");
    if (row) row.remove();
    clearChrome();
    openUser = null;
    openSource = null;
    openMode = null;
    if (history.replaceState) history.replaceState(null, "", "/users");
  }

  function insertExpandRow(userRow, expandRow) {
    var old = table.querySelector(".grant-expand-row");
    if (old) old.remove();
    clearChrome();
    userRow.after(expandRow);
    var editor = expandRow.querySelector("[id$='-editor']") || expandRow;
    if (editor.scrollIntoView) {
      editor.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function markActive(userId, source) {
    var userRow = document.getElementById("user-row-" + userId);
    if (!userRow) return;
    userRow.querySelectorAll("a[data-grant-user][data-grant-source]").forEach(function (a) {
      if (a.getAttribute("data-grant-source") === source) {
        a.classList.add("badge-link--active");
      }
    });
  }

  function markAddActive(userId) {
    var userRow = document.getElementById("user-row-" + userId);
    if (!userRow) return;
    var btn = userRow.querySelector("[data-grant-add-user]");
    if (btn) btn.classList.add("badge-add--active");
  }

  function expandModels(userId, source) {
    if (!source || loading) return;
    if (String(openUser) === String(userId) && openSource === source && openMode === "models") {
      collapse();
      return;
    }
    var userRow = document.getElementById("user-row-" + userId);
    if (!userRow) return;

    loading = true;
    fetch("/users/" + userId + "/grant/partial?source=" + encodeURIComponent(source), {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    })
      .then(function (res) {
        if (!res.ok) throw new Error("grant partial " + res.status);
        return res.text();
      })
      .then(function (html) {
        var wrap = document.createElement("tbody");
        wrap.innerHTML = String(html).trim();
        var expandRow = wrap.querySelector("tr") || wrap.firstElementChild;
        if (!expandRow) throw new Error("empty grant partial");
        insertExpandRow(userRow, expandRow);
        openUser = userId;
        openSource = source;
        openMode = "models";
        markActive(userId, source);
        if (history.replaceState) {
          history.replaceState(null, "", "/users?grant=" + userId + "#source-" + source);
        }
      })
      .catch(function () {
        collapse();
      })
      .finally(function () {
        loading = false;
      });
  }

  function expandAddSources(userId) {
    if (loading) return;
    if (String(openUser) === String(userId) && openMode === "add") {
      collapse();
      return;
    }
    var userRow = document.getElementById("user-row-" + userId);
    if (!userRow) return;

    loading = true;
    fetch("/users/" + userId + "/grant/sources/partial", {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    })
      .then(function (res) {
        if (!res.ok) throw new Error("grant sources partial " + res.status);
        return res.text();
      })
      .then(function (html) {
        var wrap = document.createElement("tbody");
        wrap.innerHTML = String(html).trim();
        var expandRow = wrap.querySelector("tr") || wrap.firstElementChild;
        if (!expandRow) throw new Error("empty sources partial");
        insertExpandRow(userRow, expandRow);
        openUser = userId;
        openSource = null;
        openMode = "add";
        markAddActive(userId);
        if (history.replaceState) {
          history.replaceState(null, "", "/users?grant=" + userId + "&add=1");
        }
      })
      .catch(function () {
        collapse();
      })
      .finally(function () {
        loading = false;
      });
  }

  function expandLimits(userId) {
    if (loading) return;
    if (String(openUser) === String(userId) && openMode === "limits") {
      collapse();
      return;
    }
    var userRow = document.getElementById("user-row-" + userId);
    if (!userRow) return;

    loading = true;
    fetch("/users/" + userId + "/grant/limits/partial", {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    })
      .then(function (res) {
        if (!res.ok) throw new Error("grant limits partial " + res.status);
        return res.text();
      })
      .then(function (html) {
        var wrap = document.createElement("tbody");
        wrap.innerHTML = String(html).trim();
        var expandRow = wrap.querySelector("tr") || wrap.firstElementChild;
        if (!expandRow) throw new Error("empty limits partial");
        insertExpandRow(userRow, expandRow);
        openUser = userId;
        openSource = null;
        openMode = "limits";
        if (history.replaceState) {
          history.replaceState(null, "", "/users?grant=" + userId + "&limits=1");
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
    if (ev.target.closest(".grant-source-revoke-form")) return;

    var collapseBtn = ev.target.closest("[data-grant-collapse]");
    if (collapseBtn) {
      ev.preventDefault();
      collapse();
      return;
    }

    var enableAll = ev.target.closest("[data-grant-enable-all]");
    if (enableAll) {
      ev.preventDefault();
      var expandRow = enableAll.closest(".grant-expand-row");
      if (expandRow) {
        expandRow.querySelectorAll('input[name="models"]').forEach(function (cb) {
          cb.checked = true;
        });
      }
      return;
    }

    var disableAll = ev.target.closest("[data-grant-disable-all]");
    if (disableAll) {
      ev.preventDefault();
      var expandRow = disableAll.closest(".grant-expand-row");
      if (expandRow) {
        expandRow.querySelectorAll('input[name="models"]').forEach(function (cb) {
          cb.checked = false;
        });
      }
      return;
    }

    var addBtn = ev.target.closest("[data-grant-add-user]");
    if (addBtn && table.contains(addBtn)) {
      ev.preventDefault();
      var addUserId = addBtn.getAttribute("data-grant-add-user");
      if (addUserId) expandAddSources(addUserId);
      return;
    }

    var limitsBtn = ev.target.closest("[data-grant-limits-user]");
    if (limitsBtn && table.contains(limitsBtn)) {
      ev.preventDefault();
      var limitsUserId = limitsBtn.getAttribute("data-grant-limits-user");
      if (limitsUserId) expandLimits(limitsUserId);
      return;
    }

    // The expand <tr> also has data-grant-source — ignore it so checkboxes/Save work.
    if (ev.target.closest(".grant-expand-row")) return;

    var link = ev.target.closest("a[data-grant-user][data-grant-source]");
    if (!link || !table.contains(link)) return;
    ev.preventDefault();
    var userId = link.getAttribute("data-grant-user");
    var source = link.getAttribute("data-grant-source");
    if (!userId || !source) return;
    expandModels(userId, source);
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && openUser != null) collapse();
  });
})();
