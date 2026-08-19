"""파일 기반 캐시: 파일 존재 여부가 곧 히트 여부, DB 없음 (SPEC.md §5.5)."""
import hashlib
import json
from pathlib import Path

from app.storage import page_result_path
from app.translate.prompts import PROMPT_VER


def _canonical_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_terms_hash(terms: list[dict]) -> str:
    """그 페이지에 실제 주입된 용어 집합의 해시. 순서 무관하도록 정렬 후 해시한다."""
    sorted_terms = sorted(terms, key=lambda t: t["en"])
    return hashlib.sha256(_canonical_json(sorted_terms).encode("utf-8")).hexdigest()[:12]


def compute_cache_key(terms: list[dict], model_id: str) -> str:
    """terms_hash + PROMPT_VER + model_id — 셋 중 하나라도 바뀌면 다른 키가 나와야 한다."""
    terms_hash = compute_terms_hash(terms)
    raw = f"{terms_hash}|{PROMPT_VER}|{model_id}|ko"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_cached_page(doc_sha: str, page_no: int, terms: list[dict], model_id: str) -> dict | None:
    """캐시 히트 시 페이지 dict, 미스(파일 없음/깨진 JSON) 시 None을 반환한다."""
    path = page_result_path(doc_sha, page_no, compute_cache_key(terms, model_id))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None  # 쓰다 만 파일 등 — 미스로 취급하고 재번역시킨다. 락은 걸지 않는다.


def save_page(doc_sha: str, page_no: int, terms: list[dict], model_id: str, page_data: dict) -> Path:
    """원자적 쓰기 — 강제 종료 중이어도 쓰다 만 JSON이 남지 않는다."""
    path = page_result_path(doc_sha, page_no, compute_cache_key(terms, model_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(page_data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)  # os.replace = 원자적, Windows에서도 동작
    return path


def _demo() -> None:
    import shutil
    import tempfile

    import app.storage as storage_module

    tmp_root = Path(tempfile.mkdtemp())
    orig_data_dir = storage_module.DATA_DIR
    storage_module.DATA_DIR = tmp_root
    try:
        sha = "deadbeef"
        terms_a = [{"en": "conductor", "ko": "도체", "note": ""}]
        terms_b = [{"en": "sheath", "ko": "시스", "note": ""}]
        page_data = {"page_no": 12, "blocks": [{"id": "p012-b00", "ko": "테스트"}]}

        # 미스
        assert load_cached_page(sha, 12, terms_a, "gpt-5.6-luna") is None

        # 저장 후 히트, tmp 파일 안 남음
        path = save_page(sha, 12, terms_a, "gpt-5.6-luna", page_data)
        assert not path.with_suffix(".json.tmp").exists()
        loaded = load_cached_page(sha, 12, terms_a, "gpt-5.6-luna")
        assert loaded == page_data

        # 용어집이 바뀌면 다른 키 -> 이전 캐시는 안 보이고, 독립적으로 미스
        assert load_cached_page(sha, 12, terms_b, "gpt-5.6-luna") is None
        # 원래 키는 여전히 히트 (용어집 바뀐다고 기존 캐시가 사라지지 않음, 새 키로만 안 보일 뿐)
        assert load_cached_page(sha, 12, terms_a, "gpt-5.6-luna") == page_data

        # 모델이 바뀌면 다른 키
        assert load_cached_page(sha, 12, terms_a, "gpt-5.6-terra") is None

        # 깨진 JSON -> 크래시 없이 미스로 취급
        broken_path = page_result_path(sha, 13, compute_cache_key(terms_a, "gpt-5.6-luna"))
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.write_text("{not valid json", encoding="utf-8")
        assert load_cached_page(sha, 13, terms_a, "gpt-5.6-luna") is None

        print("cache.py self-check OK")
    finally:
        storage_module.DATA_DIR = orig_data_dir
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    _demo()
