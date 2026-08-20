// 대조 뷰: 저장된 결과 로드 -> 부족한 페이지만 잡 생성 -> SSE로 실시간 채움 -> 양방향 클릭 대조.
// 순수 vanilla JS, 프레임워크 없음 (SPEC.md §6, §7.3).
(function () {
  "use strict";

  var viewData = JSON.parse(document.getElementById("view-data").textContent);
  var sha = viewData.sha;
  var pageNumbers = viewData.pageNumbers;
  var warnCount = 0;
  var skipCount = 0;
  var doneCount = 0;

  var progressLabel = document.getElementById("progress-label");
  var warnLabel = document.getElementById("warn-count");
  var skipLabel = document.getElementById("skip-count");

  function pad3(n) {
    return String(n).padStart(3, "0");
  }

  function escapeHtml(s) {
    var div = document.createElement("div");
    div.textContent = s == null ? "" : s;
    return div.innerHTML;
  }

  // ---- 블록 -> DOM (Step 3 mock 템플릿과 동일한 구조/클래스를 그대로 재현) ----

  function overlayHtml(pageNo, pageWidth, pageHeight, blocks) {
    return blocks
      .filter(function (b) { return b.type !== "header_footer"; })
      .map(function (b) {
        var isFigure = b.type === "figure";
        var left = (b.bbox[0] / pageWidth) * 100;
        var top = (b.bbox[1] / pageHeight) * 100;
        var width = ((b.bbox[2] - b.bbox[0]) / pageWidth) * 100;
        var height = ((b.bbox[3] - b.bbox[1]) / pageHeight) * 100;
        return (
          '<div class="overlay' + (isFigure ? " is-figure" : "") + '" data-block-id="' + b.id +
          '" data-verify="' + b.verify.status + '"' +
          ' style="left:' + left + '%;top:' + top + '%;width:' + width + '%;height:' + height + '%;"></div>'
        );
      })
      .join("");
  }

  function blockInnerHtml(b, pageNo) {
    if (b.type === "table") {
      var rows = (b.table && b.table.cells_ko) || [];
      return "<table>" + rows.map(function (row) {
        return "<tr>" + row.map(function (c) { return "<td>" + escapeHtml(c) + "</td>"; }).join("") + "</tr>";
      }).join("") + "</table>";
    }
    if (b.type === "figure") {
      // 수식: 텍스트 기반 bbox라 좌표가 정확 → 크롭 이미지가 안전
      if (b.source) {
        return '<img class="formula-img" loading="lazy"' +
              ' src="/doc/' + sha + '/crop/' + pad3(pageNo) + '/' + b.id + '.png"' +
              ' alt="' + escapeHtml(b.source) + '">';
      }
      // 도식: 벡터 클러스터 bbox라 부정확할 수 있음 → 좌측 참조 유지
      return '<div class="figure-ref">[그림 - 좌측 원문 참조]</div>';
    }
    if (b.type === "header_footer") return '<div class="hf-text">' + escapeHtml(b.source) + "</div>";
    return '<div class="block-text">' + escapeHtml(b.ko || b.source) + "</div>";
  }

  function badgeHtml(b) {
    if (b.verify.status === "warn") {
      var missing = (b.verify.missing || []).join(", ");
      return '<span class="badge warn" title="원문의 ' + escapeHtml(missing) + '이(가) 번역에서 확인되지 않습니다">⚠</span>';
    }
    if (b.verify.status === "skipped" && b.type !== "header_footer" && b.type !== "figure") {
      return '<span class="badge skip" title="' + escapeHtml(b.verify.reason) + '">⏭</span>';
    }
    return "";
  }

  function blocksHtml(blocks, pageNo) {
    return blocks
      .map(function (b) {
        var clickAttr = b.type === "header_footer" ? ' data-clickable="false"' : "";
        return (
          '<div class="block block-' + b.type + '" data-block-id="' + b.id + '" data-verify="' +
          b.verify.status + '"' + clickAttr + ">" + blockInnerHtml(b, pageNo) + badgeHtml(b) + "</div>"
        );
      })
      .join("");
  }

  function renderPage(pageNo, pageData) {
    var srcWrap = document.getElementById("src-p" + pad3(pageNo));
    var tgtWrap = document.getElementById("tgt-p" + pad3(pageNo));
    if (!srcWrap || !tgtWrap) return;

    var overlays = overlayHtml(pageNo, pageData.page_width, pageData.page_height, pageData.blocks);
    srcWrap.insertAdjacentHTML("beforeend", overlays);
    tgtWrap.innerHTML = blocksHtml(pageData.blocks, pageNo);

    for (var i = 0; i < pageData.blocks.length; i++) {
      var status = pageData.blocks[i].verify.status;
      var type = pageData.blocks[i].type;
      if (status === "warn") warnCount++;
      else if (status === "skipped" && type !== "header_footer" && type !== "figure") skipCount++;
    }
    warnLabel.textContent = "검증 경고 " + warnCount + "건";
    skipLabel.textContent = "검증 생략 " + skipCount + "건";

    doneCount++;
    progressLabel.textContent = doneCount + " / " + pageNumbers.length + " 페이지";
  }

  // ---- 초기 로드: 이미 캐시된 페이지 먼저 채우기 (새로고침·재접속용, SPEC.md §6) ----

  function loadStoredThenMaybeCreateJob() {
    var rangeStr = pageNumbers.join(",");
    fetch("/api/docs/" + sha + "/pages?range=" + encodeURIComponent(rangeStr))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var stored = data.pages || {};
        var missing = [];
        pageNumbers.forEach(function (p) {
          if (stored[String(p)]) {
            renderPage(p, stored[String(p)]);
          } else {
            missing.push(p);
          }
        });
        if (missing.length > 0) {
          createJobAndStream(missing);
        }
      });
  }

  function createJobAndStream(pages) {
    fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc_sha: sha, doc_title: viewData.docTitle, pages: pages }),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        subscribeToJob(data.job_id);
      });
  }

  function subscribeToJob(jobId) {
    // EventSource가 Last-Event-ID 재연결을 브라우저 표준으로 알아서 처리한다 (SPEC.md §6 ⑥).
    var es = new EventSource("/api/jobs/" + jobId + "/stream");
    es.onmessage = function (evt) {
      var payload = JSON.parse(evt.data);
      if (payload.type === "page_done") {
        renderPage(payload.page, { blocks: payload.blocks, page_width: viewData.pageWidth, page_height: viewData.pageHeight });
      } else if (payload.type === "page_error") {
        var tgtWrap = document.getElementById("tgt-p" + pad3(payload.page));
        if (tgtWrap) tgtWrap.innerHTML = '<div class="page-skeleton error">번역 실패: ' + escapeHtml(payload.reason) + "</div>";
      } else if (payload.type === "job_done") {
        es.close();
      }
    };
    es.onerror = function () {
      // 네트워크가 완전히 끊기면 브라우저가 자동 재연결을 계속 시도한다. 별도 처리 불필요.
    };
  }

  loadStoredThenMaybeCreateJob();

  // ---- 양방향 클릭 대조 ----

  function clearActive() {
    document.querySelectorAll("[data-block-id].active").forEach(function (el) {
      el.classList.remove("active");
    });
  }

  function activate(blockId, scrollTarget) {
    clearActive();
    document.querySelectorAll('[data-block-id="' + blockId + '"]').forEach(function (el) {
      el.classList.add("active");
      if (scrollTarget && el.closest(".pane") !== scrollTarget) {
        el.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
  }

  document.addEventListener("click", function (e) {
    var el = e.target.closest("[data-block-id]");
    if (!el) return;
    if (el.dataset.clickable === "false") return;
    activate(el.dataset.blockId, el.closest(".pane"));
  });
})();
