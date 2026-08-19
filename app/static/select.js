(function () {
  "use strict";
  var sha = document.body.dataset.sha;
  var pageCount = parseInt(document.body.dataset.pageCount, 10);
  var outline = JSON.parse(document.getElementById("outline-data").textContent);
  var rangeInput = document.getElementById("range-input");
  var estimateEl = document.getElementById("estimate");
  var goBtn = document.getElementById("go-btn");

  function parseRange(str) {
    var pages = new Set();
    str.split(",").forEach(function (part) {
      part = part.trim();
      if (!part) return;
      if (part.indexOf("-") !== -1) {
        var bounds = part.split("-");
        var start = parseInt(bounds[0], 10);
        var end = parseInt(bounds[1], 10);
        if (isNaN(start) || isNaN(end)) return;
        for (var p = start; p <= end; p++) pages.add(p);
      } else {
        var n = parseInt(part, 10);
        if (!isNaN(n)) pages.add(n);
      }
    });
    return Array.from(pages).sort(function (a, b) { return a - b; });
  }

  function setRangeFromPages(pages) {
    rangeInput.value = pages.join(", ");
    updateSelectionUI();
  }

  function addPageToRange(pageNo) {
    var pages = new Set(parseRange(rangeInput.value));
    if (pages.has(pageNo)) pages.delete(pageNo);
    else pages.add(pageNo);
    setRangeFromPages(Array.from(pages).sort(function (a, b) { return a - b; }));
  }

  function updateSelectionUI() {
    var pages = parseRange(rangeInput.value);
    var pageSet = new Set(pages);

    document.querySelectorAll(".thumb").forEach(function (el) {
      var p = parseInt(el.dataset.page, 10);
      el.classList.toggle("selected", pageSet.has(p));
    });

    if (pages.length === 0) {
      estimateEl.textContent = "";
      goBtn.classList.add("disabled");
      return;
    }

    goBtn.classList.remove("disabled");
    goBtn.href = "/doc/" + sha + "/view?range=" + encodeURIComponent(rangeInput.value);
    estimateEl.textContent = pages.length + "페이지 선택 · 확인 중...";

    fetch("/api/docs/" + sha + "/estimate?range=" + encodeURIComponent(rangeInput.value))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        estimateEl.textContent =
          data.selected + "페이지 선택 · 예상 " + data.eta_text +
          " · 신규 " + data.new_count + " / 재사용 " + data.reused_count;
      })
      .catch(function () {
        estimateEl.textContent = pages.length + "페이지 선택";
      });
  }

  document.querySelectorAll(".thumb").forEach(function (el) {
    el.addEventListener("click", function () {
      addPageToRange(parseInt(el.dataset.page, 10));
    });
  });

  document.querySelectorAll(".outline-item").forEach(function (el) {
    el.addEventListener("click", function () {
      var idx = parseInt(el.dataset.index, 10);
      var entry = outline[idx];
      var endPage = pageCount;
      for (var i = idx + 1; i < outline.length; i++) {
        if (outline[i].level <= entry.level) {
          endPage = outline[i].page - 1;
          break;
        }
      }
      setRangeFromPages(Array.from({ length: endPage - entry.page + 1 }, function (_, i) { return entry.page + i; }));
    });
  });

  rangeInput.addEventListener("input", updateSelectionUI);
  rangeInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !goBtn.classList.contains("disabled")) {
      window.location.href = goBtn.href;
    }
  });
  rangeInput.focus();
  updateSelectionUI();
})();
