"""팀 공용 용어집: CSV 로드 + mtime 자동 리로드 + 규칙 기반 매칭. LLM 미사용, 결정적 (SPEC.md §5.2).

캐시 키(§5.5)에 들어가므로 매칭 결과가 매번 같은 입력에 같은 출력을 내야 한다.
"""
import csv
import re
from pathlib import Path

from app.storage import glossary_path

_cache: dict = {"path": None, "mtime": None, "entries": []}


def _normalize_for_match(s: str) -> str:
    """매칭 키 생성 전용 — 소문자화 + 하이픈/공백 변형 통일. 원문 표시에는 쓰지 않는다."""
    s = s.lower().strip()
    return re.sub(r"[-\s]+", " ", s)


def _plural_variants(term: str) -> set[str]:
    variants = {term}
    if term.endswith("y") and len(term) > 1 and term[-2] not in "aeiou":
        variants.add(term[:-1] + "ies")
    elif term.endswith(("s", "x", "z", "ch", "sh")):
        variants.add(term + "es")
    else:
        variants.add(term + "s")
    return variants


def _build_pattern(term: str) -> re.Pattern:
    normalized = _normalize_for_match(term)
    variants = _plural_variants(normalized)
    # 정규화 과정에서 하이픈/공백을 전부 단일 공백으로 합쳤으므로,
    # 매칭 시에는 그 공백 자리에 하이픈이든 공백이든 아무거나 오도록 되돌려 허용한다.
    alt = "|".join(re.escape(v).replace(r"\ ", r"[-\s]") for v in sorted(variants, key=len, reverse=True))
    # \b 대신 (?<![A-Za-z0-9])/(?![A-Za-z0-9])를 쓴다 — verify/check.py와 동일한 이유
    # (원문 뒤에 비-ASCII 문자가 붙는 경우에도 안전하게 경계를 잡기 위함).
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", re.IGNORECASE)


def load_glossary(path: Path | None = None) -> list[dict]:
    """path의 mtime이 바뀌었을 때만 다시 읽는다 — 재시작 없이 CSV 수정이 반영된다."""
    path = path or glossary_path()
    if not path.exists():
        return []
    mtime = path.stat().st_mtime
    if _cache["path"] == path and _cache["mtime"] == mtime:
        return _cache["entries"]

    entries = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            en = (row.get("en") or "").strip()
            ko = (row.get("ko") or "").strip()
            if not en or not ko:
                continue
            entries.append({"en": en, "ko": ko, "note": (row.get("note") or "").strip()})

    _cache["path"] = path
    _cache["mtime"] = mtime
    _cache["entries"] = entries
    return entries


def match_terms(text: str, glossary: list[dict] | None = None) -> list[dict]:
    """페이지 텍스트에 실제 등장하는 용어집 항목만 반환한다.

    긴 용어를 우선한다 ("copper conductor"가 있으면 그 구간에서 "conductor"는
    따로 매칭하지 않는다) — 겹치는 구간은 가장 긴 매칭만 채택하는 그리디 방식.
    """
    glossary = glossary if glossary is not None else load_glossary()
    if not text or not glossary:
        return []

    candidates = []  # (start, end, entry)
    for entry in glossary:
        for m in _build_pattern(entry["en"]).finditer(text):
            candidates.append((m.start(), m.end(), entry))

    candidates.sort(key=lambda c: (c[0] - c[1], c[0]))  # 길이 내림차순(음수 우선) -> 시작 위치

    selected_spans: list[tuple[int, int]] = []
    matched: list[dict] = []
    seen_terms: set[str] = set()
    for start, end, entry in candidates:
        if any(start < s_end and end > s_start for s_start, s_end in selected_spans):
            continue
        selected_spans.append((start, end))
        if entry["en"] not in seen_terms:
            seen_terms.add(entry["en"])
            matched.append(entry)

    return matched


def _demo() -> None:
    import shutil
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp())
    csv_path = tmp_dir / "glossary.csv"
    try:
        csv_path.write_text(
            "en,ko,note\nconductor,도체,\ncopper conductor,동도체,\nsheath,시스,외피 아님\nlay-up,연합,\n",
            encoding="utf-8",
        )
        glossary = load_glossary(csv_path)
        assert len(glossary) == 4

        # mtime 자동 리로드
        import time
        time.sleep(0.3)  # mtime 해상도가 거친 파일시스템에서도 변경이 감지되도록 여유를 둔다
        csv_path.write_text("en,ko,note\nconductor,도체,\n", encoding="utf-8")
        reloaded = load_glossary(csv_path)
        assert len(reloaded) == 1, "CSV를 고쳤는데 캐시가 안 갱신됨"

        # 원상복구 후 매칭 테스트 — mtime 리로드 자체는 위에서 이미 검증했으므로,
        # 여기서는 타이밍에 기대지 않고 캐시를 강제로 무효화해 결정론적으로 새로 읽는다.
        csv_path.write_text(
            "en,ko,note\nconductor,도체,\ncopper conductor,동도체,\nsheath,시스,외피 아님\nlay-up,연합,\n",
            encoding="utf-8",
        )
        _cache["mtime"] = None
        glossary = load_glossary(csv_path)
        assert len(glossary) == 4

        # 복수형 매칭
        m = match_terms("The conductors shall comply with IEC 60228.", glossary)
        assert {e["en"] for e in m} == {"conductor"}, m

        # 긴 용어 우선 — "conductor"가 "copper conductor"에 흡수되어야 함
        m = match_terms("The copper conductor and its sheath were tested.", glossary)
        terms = {e["en"] for e in m}
        assert terms == {"copper conductor", "sheath"}, terms

        # 하이픈/공백 변형 통일
        m = match_terms("Cable lay up and lay-up and layup are the same.", glossary)
        assert {e["en"] for e in m} == {"lay-up"}, m

        # 매칭 안 되는 텍스트 -> 빈 리스트
        assert match_terms("No relevant terms here.", glossary) == []

        print("glossary.py self-check OK")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    _demo()
