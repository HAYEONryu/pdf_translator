"""data/ 경로 규약 + meta.json 입출력. DB 대신 파일시스템 (SPEC.md §2)."""
import json
from pathlib import Path

from app.config import DATA_DIR


def doc_dir(sha256: str) -> Path:
    return DATA_DIR / "docs" / sha256


def source_pdf_path(sha256: str) -> Path:
    return doc_dir(sha256) / "source.pdf"


def meta_path(sha256: str) -> Path:
    return doc_dir(sha256) / "meta.json"


def render_path(sha256: str, page_no: int) -> Path:
    return doc_dir(sha256) / "renders" / f"p{page_no:03d}.png"


def thumb_path(sha256: str, page_no: int) -> Path:
    return doc_dir(sha256) / "renders" / f"p{page_no:03d}_thumb.png"


def page_result_path(sha256: str, page_no: int, cache_key: str) -> Path:
    return doc_dir(sha256) / "pages" / f"p{page_no:03d}__{cache_key}.json"


def glossary_path() -> Path:
    return DATA_DIR / "glossary.csv"


def doc_exists(sha256: str) -> bool:
    return meta_path(sha256).exists()


def ensure_doc_dirs(sha256: str) -> None:
    doc_dir(sha256).mkdir(parents=True, exist_ok=True)
    (doc_dir(sha256) / "renders").mkdir(exist_ok=True)
    (doc_dir(sha256) / "pages").mkdir(exist_ok=True)


def load_meta(sha256: str) -> dict:
    return json.loads(meta_path(sha256).read_text(encoding="utf-8"))


def save_meta(sha256: str, meta: dict) -> None:
    """원자적 쓰기 — 강제 종료 중 파일이 남아도 파싱 에러가 나지 않는다 (SPEC.md §5.5와 동일 패턴)."""
    path = meta_path(sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_cached_page_numbers(sha256: str) -> set[int]:
    """번역 결과가 하나라도 있는 페이지 번호 집합 — 썸네일 그리드 ✓ 배지용 (SPEC.md §7.2)."""
    pages_dir = doc_dir(sha256) / "pages"
    if not pages_dir.exists():
        return set()
    return {int(f.name.split("__")[0][1:]) for f in pages_dir.glob("p*__*.json")}


def get_stored_pages(sha256: str, page_numbers: list[int]) -> dict[int, dict]:
    """pages/에서 요청한 페이지들의 최신 결과를 읽는다. 잡 상태가 아니라 파일이 진실이다 (SPEC.md §6)."""
    pages_dir = doc_dir(sha256) / "pages"
    result: dict[int, dict] = {}
    if not pages_dir.exists():
        return result
    for page_no in page_numbers:
        candidates = sorted(pages_dir.glob(f"p{page_no:03d}__*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            continue
        try:
            result[page_no] = json.loads(candidates[-1].read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue  # 깨진 캐시 파일은 "미완료"로 취급
    return result


def list_recent_docs(limit: int = 10) -> list[dict]:
    """meta.json mtime 기준 최근 문서 목록. 각 항목에 번역된 페이지 수를 덧붙인다 (SPEC.md §7.1)."""
    docs_root = DATA_DIR / "docs"
    if not docs_root.exists():
        return []
    meta_files = sorted(
        docs_root.glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    results = []
    for mp in meta_files[:limit]:
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        pages_dir = mp.parent / "pages"
        page_nos = {f.name.split("__")[0] for f in pages_dir.glob("p*__*.json")} if pages_dir.exists() else set()
        meta["translated_page_count"] = len(page_nos)
        results.append(meta)
    return results


def _demo() -> None:
    """python -m app.storage로 실행하면 __main__이 app.storage와 다른 모듈 객체가 되므로,
    app.storage를 별도로 import해 그 DATA_DIR를 패치하는 방식은 __main__ 쪽 함수에 반영되지
    않는다 (실제로 이 버그로 진짜 ./data에 테스트 파일이 생성된 적이 있음). 그 대신 현재
    실행 중인 모듈 자신의 globals()를 직접 패치한다 — doc_dir() 등도 같은 globals를 보므로
    __main__으로 실행하든 app.storage로 import하든 항상 올바르게 적용된다.
    """
    import shutil
    import tempfile

    tmp_root = Path(tempfile.mkdtemp())
    orig_data_dir = globals()["DATA_DIR"]
    globals()["DATA_DIR"] = tmp_root
    try:
        sha = "deadbeef"
        assert not doc_exists(sha)
        ensure_doc_dirs(sha)
        assert doc_dir(sha).exists()
        assert (doc_dir(sha) / "renders").exists()
        assert (doc_dir(sha) / "pages").exists()

        meta = {"sha256": sha, "filename": "테스트 문서.pdf", "page_count": 3}
        save_meta(sha, meta)
        assert doc_exists(sha)
        assert not meta_path(sha).with_suffix(".json.tmp").exists(), "tmp 파일이 남으면 안 됨"
        loaded = load_meta(sha)
        assert loaded["filename"] == "테스트 문서.pdf"

        assert render_path(sha, 12).name == "p012.png"
        assert page_result_path(sha, 12, "abc123").name == "p012__abc123.json"

        pages_dir = doc_dir(sha) / "pages"
        (pages_dir / "p001__key1.json").write_text("{}", encoding="utf-8")
        (pages_dir / "p001__key2.json").write_text("{}", encoding="utf-8")  # 같은 페이지, 다른 캐시 키
        (pages_dir / "p002__key1.json").write_text("{}", encoding="utf-8")

        recent = list_recent_docs()
        assert len(recent) == 1
        assert recent[0]["translated_page_count"] == 2, "중복 캐시 키가 있어도 페이지 수는 dedupe되어야 함"

        print("storage.py self-check OK")
    finally:
        globals()["DATA_DIR"] = orig_data_dir
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    _demo()
