(function () {
  "use strict";
  var dropzone = document.getElementById("dropzone");
  var fileInput = document.getElementById("file-input");
  var status = document.getElementById("status");
  var progressBar = document.getElementById("upload-progress");
  var isUploading = false;

  function setStatus(text, cls) {
    status.textContent = text;
    status.className = "status" + (cls ? " " + cls : "");
  }

  // 업로드(전송 + 서버 분석) 도중에는 dropzone을 비활성화한다 — 진행 중인 업로드가
  // 끝나기 전에 두 번째 파일을 겹쳐 올리면 서로 다른 문서의 진행률/상태 표시가
  // 섞여 보이므로, 한 번에 1개 문서만 받는다.
  function setUploading(value) {
    isUploading = value;
    dropzone.classList.toggle("disabled", value);
    fileInput.disabled = value;
  }

  function upload(file) {
    if (isUploading) return;
    if (file.type !== "application/pdf") {
      setStatus("PDF 파일만 업로드할 수 있습니다.", "error");
      return;
    }
    if (file.size > 100 * 1024 * 1024) {
      setStatus("100MB를 초과하는 파일입니다.", "error");
      return;
    }

    setUploading(true);
    var formData = new FormData();
    formData.append("file", file);

    var xhr = new XMLHttpRequest();
    progressBar.hidden = false;
    progressBar.value = 0;
    setStatus("업로드 중... 0%", "info");

    xhr.upload.addEventListener("progress", function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.round((e.loaded / e.total) * 100);
      progressBar.value = pct;
      setStatus("업로드 중... " + pct + "%", "info");
    });

    xhr.upload.addEventListener("load", function () {
      // 업로드는 끝났지만 서버가 PDF를 스캔(페이지 수·목차·표 탐지)하는 동안은
      // 진행률을 알 수 없으니 부정확한 숫자로 멈춰 있기보다 애니메이션으로 대략만 표시한다.
      progressBar.removeAttribute("value");
      setStatus("문서 분석 중...", "info");
    });

    xhr.addEventListener("load", function () {
      progressBar.hidden = true;
      if (xhr.status < 200 || xhr.status >= 300) {
        setStatus("업로드 실패 (" + xhr.status + ")", "error");
        setUploading(false);
        return;
      }
      var data = JSON.parse(xhr.responseText);
      setStatus(
        data.reused ? "이 문서는 이전에 번역된 적이 있습니다. 결과를 재사용합니다." : "업로드 완료.",
        "info"
      );
      window.location.href = "/doc/" + data.sha + "/select";
    });

    xhr.addEventListener("error", function () {
      progressBar.hidden = true;
      setStatus("네트워크 오류로 업로드에 실패했습니다.", "error");
      setUploading(false);
    });

    xhr.open("POST", "/api/upload");
    xhr.send(formData);
  }

  dropzone.addEventListener("click", function () {
    if (isUploading) return;
    fileInput.click();
  });
  fileInput.addEventListener("change", function () {
    if (fileInput.files[0]) upload(fileInput.files[0]);
  });
  dropzone.addEventListener("dragover", function (e) {
    e.preventDefault();
    if (isUploading) return;
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", function () {
    dropzone.classList.remove("dragover");
  });
  dropzone.addEventListener("drop", function (e) {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    var file = e.dataTransfer.files[0];
    if (file) upload(file);
  });
})();
