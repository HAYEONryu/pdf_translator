"""업로드된 PDF에서 meta.json을 만들고 저장한다 (SPEC.md §3 S1, §7.1). SHA가 곧 캐시/조회 키다."""
import hashlib
from datetime import datetime, timezone

import fitz
import pdfplumber

from app.config import TABLE_MIN_FILLED_RATIO
from app.storage import doc_exists, ensure_doc_dirs, load_meta, save_meta, source_pdf_path


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _table_filled_ratio(cells: list) -> float:
    flat = [c for row in cells for c in row]
    if not flat:
        return 0.0
    return sum(1 for c in flat if c not in (None, "")) / len(flat)


def _scan_pdf(pdf_path: str) -> dict:
    doc = fitz.open(pdf_path)
    outline = [{"level": lvl, "title": title, "page": page} for lvl, title, page in doc.get_toc()]
    doc.close()

    table_pages: list[int] = []
    has_text_layer = False
    page_width = page_height = 0.0
    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if i == 0:
                page_width, page_height = page.width, page.height
            if page.extract_words():
                has_text_layer = True  # 한 페이지라도 텍스트가 있으면 True — 대략적인 문서 단위 지표
            for t in page.find_tables():
                if _table_filled_ratio(t.extract()) >= TABLE_MIN_FILLED_RATIO:
                    table_pages.append(i + 1)
                    break

    return {
        "page_count": page_count,
        "has_text_layer": has_text_layer,
        "page_width": page_width,
        "page_height": page_height,
        "outline": outline,
        "table_pages": table_pages,
    }


def ingest_pdf(data: bytes, filename: str) -> tuple[str, bool]:
    """SHA 계산 -> 신규면 저장+스캔, 기존이면 재사용. (sha256, 재사용 여부)를 반환한다."""
    sha = file_sha256(data)
    if doc_exists(sha):
        return sha, True

    ensure_doc_dirs(sha)
    source_pdf_path(sha).write_bytes(data)

    meta = {
        "sha256": sha,
        "filename": filename,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **_scan_pdf(str(source_pdf_path(sha))),
    }
    save_meta(sha, meta)
    return sha, False


def _demo() -> None:
    import shutil
    from pathlib import Path

    import app.storage as storage_module

    tmp_root = Path(__file__).resolve().parent.parent / ".tmp_ingest_demo"
    orig_data_dir = storage_module.DATA_DIR
    storage_module.DATA_DIR = tmp_root
    try:
        data = Path("samplePDF.pdf").read_bytes()
        sha1, reused1 = ingest_pdf(data, "샘플 문서.pdf")
        assert reused1 is False
        meta = load_meta(sha1)
        assert meta["page_count"] == 85
        assert meta["has_text_layer"] is True
        # p44는 실제 데이터 표라 포함되고, p27은 차트 오탐이라 제외되어야 함 (Step 0 실측)
        assert 44 in meta["table_pages"]
        assert 27 not in meta["table_pages"]

        # 같은 내용 재업로드 -> 재사용, 다시 스캔하지 않음
        sha2, reused2 = ingest_pdf(data, "다른이름.pdf")
        assert sha1 == sha2
        assert reused2 is True
        assert load_meta(sha2)["filename"] == "샘플 문서.pdf"  # 재사용 시 원래 메타 유지

        print("ingest.py self-check OK")
    finally:
        storage_module.DATA_DIR = orig_data_dir
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    _demo()
