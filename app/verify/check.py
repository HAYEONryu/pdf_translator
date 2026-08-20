"""규칙 기반 후검증. AI 미사용, 전부 결정적 (SPEC.md §5.4)."""
import re
import unicodedata
from collections import Counter

from app.config import PART_NUMBER_PATTERN, SPEC_NUMBER_PATTERN

_FRACTION_MAP = {
    "½": "0.5", "⅓": "0.333", "⅔": "0.667", "¼": "0.25", "¾": "0.75",
    "⅕": "0.2", "⅖": "0.4", "⅗": "0.6", "⅘": "0.8",
    "⅙": "0.167", "⅚": "0.833", "⅛": "0.125", "⅜": "0.375", "⅝": "0.625", "⅞": "0.875",
}
_HYPHEN_CHARS = "–—−‐‑‒―"
_HYPHEN_RE = re.compile(f"[{_HYPHEN_CHARS}]")
_SPACE_THOUSAND_RE = re.compile(r"(?<=\d) (?=\d{3}\b)")

# ponytail: 휴리스틱 단위 목록. 실제 문서에서 나오는 단위를 보며 확장.
# 끝 경계로 \b 대신 (?![A-Za-z0-9])를 쓴다 — config.py의 SPEC_NUMBER_PATTERN 주석 참고
# (한글 조사가 공백 없이 붙으면 \b가 깨짐: "2.5 mm²여야").
# 소수점은 "\.\d+"로만 매칭한다 — "[\d,.]*"처럼 통으로 넣으면 문장 끝 마침표까지
# 숫자에 먹혀 "2026." 같은 잘못된 토큰이 된다.
_NUMBER_CORE = r"\d[\d,]*(?:\.\d+)?"
_UNIT_ALT = r"mm²|mm2|sq\.?\s?mm|mm|cm|m|kV|kA|mA|A|V|W|kW|°C|℃|degC|kg|g|N|Pa|kPa|MPa|Hz"
UNIT_NUMBER_RE = re.compile(rf"-?{_NUMBER_CORE}\s?(?:{_UNIT_ALT})(?![A-Za-z0-9])")
BARE_NUMBER_RE = re.compile(rf"-?{_NUMBER_CORE}")
NUMERIC_PREFIX_RE = re.compile(rf"-?{_NUMBER_CORE}")
# 콤마 없는 순수 정수(자릿수 무관, 예: "2026")는 그대로 허용하고,
# 콤마가 있으면 천단위 3자리 그룹만 허용한다 ("3,200"은 되고 "71,36"은 안 됨).
ENGLISH_NUMBER_RE = re.compile(r"^-?(\d+|\d{1,3}(,\d{3})+)(\.\d+)?$")

# LLM이 응답 도중 잘리거나(max_completion_tokens 부족) 폭주하면 한국어 번역에 엉뚱한
# 문자 체계가 섞여 나오는 사고가 실제로 있었다 (예: 그루지야 문자 "შესაბამის"가 한글
# 문장 중간에 삽입됨). 번역 결과에 이런 스크립트가 하나라도 있으면 절대 정상일 수 없다.
_UNEXPECTED_SCRIPT_RANGES = (
    (0x0400, 0x04FF),  # Cyrillic
    (0x0530, 0x058F),  # Armenian
    (0x10A0, 0x10FF),  # Georgian
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0E00, 0x0E7F),  # Thai
    (0x0900, 0x097F),  # Devanagari
)
# 원문 대비 번역이 이 비율보다 짧으면 중간에 잘렸을 가능성이 높다고 본다 — ponytail: 휴리스틱
_MIN_TRANSLATION_LENGTH_RATIO = 0.35
_MIN_SOURCE_LEN_FOR_LENGTH_CHECK = 40


def _unexpected_script_chars(text: str) -> set[str]:
    return {c for c in text if any(lo <= ord(c) <= hi for lo, hi in _UNEXPECTED_SCRIPT_RANGES)}


def _corruption_notes(source: str, translated: str | None) -> list[str]:
    """잘리거나 엉뚱한 문자가 섞인 번역을 잡는다. 숫자/규격번호 검증과 별개로 항상 돈다."""
    translated = translated or ""
    notes = []
    bad_chars = _unexpected_script_chars(translated)
    if bad_chars:
        notes.append(f"번역에 예상 밖 문자 포함: {''.join(sorted(bad_chars))}")
    if len(source) >= _MIN_SOURCE_LEN_FOR_LENGTH_CHECK and len(translated) < len(source) * _MIN_TRANSLATION_LENGTH_RATIO:
        notes.append("번역이 원문보다 지나치게 짧음 (중간에 잘렸을 가능성)")
    return notes

_MASK_PLACEHOLDER = ""


def normalize_locale_agnostic(text: str) -> str:
    """언어 무관 정규화 — 전각→반각, 공백 천단위 제거, 유니코드 분수, 하이픈 통일 (SPEC.md §5.4 0단계).

    분수 치환을 NFKC보다 먼저 한다: NFKC 자체가 "½"를 호환 분해로 "1⁄2"(숫자+분수슬래시+숫자)
    로 바꿔버려서, NFKC 이후에 "½" 키로 찾으면 이미 사라지고 없다.
    """
    for frac, dec in _FRACTION_MAP.items():
        text = text.replace(frac, dec)
    text = unicodedata.normalize("NFKC", text)
    text = _SPACE_THOUSAND_RE.sub("", text)
    text = _HYPHEN_RE.sub("-", text)
    return text


def _mask_pattern(text: str, pattern: re.Pattern) -> tuple[str, list]:
    masked_values = []

    def _repl(m: re.Match) -> str:
        masked_values.append(m.group(0))
        return _MASK_PLACEHOLDER

    return pattern.sub(_repl, text), masked_values


def extract_verification_tokens(text: str) -> dict:
    """0단계 정규화 후 규격번호→품번→단위동반수치→순수수치 순서로 마스킹 추출 (SPEC.md §5.4, 순서 고정)."""
    text = normalize_locale_agnostic(text)
    text, spec_numbers = _mask_pattern(text, SPEC_NUMBER_PATTERN)
    text, part_numbers = _mask_pattern(text, PART_NUMBER_PATTERN)
    text, unit_numbers = _mask_pattern(text, UNIT_NUMBER_RE)
    text, bare_numbers = _mask_pattern(text, BARE_NUMBER_RE)
    return {
        "spec_numbers": spec_numbers,
        "part_numbers": part_numbers,
        "unit_numbers": unit_numbers,
        "bare_numbers": bare_numbers,
    }


def _numeric_prefix(s: str) -> str:
    m = NUMERIC_PREFIX_RE.match(s.strip())
    return m.group(0) if m else s.strip()


def _is_english_numeral(s: str) -> bool:
    return bool(ENGLISH_NUMBER_RE.match(_numeric_prefix(s)))


def _normalize_numeric_token(s: str) -> str:
    """콤마 제거 후 비교용 정규화. 영어 표기 범위 검사를 통과한 토큰에만 적용 (SPEC.md §5.4)."""
    prefix = _numeric_prefix(s)
    rest = s.strip()[len(prefix):].strip()
    normalized_prefix = prefix.replace(",", "")
    return f"{normalized_prefix} {rest}" if rest else normalized_prefix


def check_text_block(source: str | None, translated: str | None) -> dict:
    if not source:
        return {"status": "ok", "missing": [], "reason": None}

    corruption = _corruption_notes(source, translated)
    if corruption:
        return {"status": "warn", "missing": corruption, "reason": None}

    src_tokens = extract_verification_tokens(source)
    numeric_tokens = src_tokens["unit_numbers"] + src_tokens["bare_numbers"]
    if any(not _is_english_numeral(t) for t in numeric_tokens):
        return {
            "status": "skipped",
            "missing": [],
            "reason": "unsupported number format (non-English decimal/thousand notation)",
        }

    tgt_tokens = extract_verification_tokens(translated or "")

    missing = []
    for key in ("spec_numbers", "part_numbers"):
        src_c = Counter(src_tokens[key])
        tgt_c = Counter(tgt_tokens[key])
        for val, cnt in src_c.items():
            if tgt_c.get(val, 0) < cnt:
                missing.append(val)

    for key in ("unit_numbers", "bare_numbers"):
        src_c = Counter(_normalize_numeric_token(t) for t in src_tokens[key])
        tgt_c = Counter(_normalize_numeric_token(t) for t in tgt_tokens[key])
        for val, cnt in src_c.items():
            if tgt_c.get(val, 0) < cnt:
                missing.append(val)

    # 시스템 프롬프트 8번 조항(줄바꿈·불릿 보존)이 지켜졌는지 검사 — LLM이 목록 항목을
    # 한 줄로 합쳐버리면 줄 수가 달라진다.
    if source.count("\n") != (translated or "").count("\n"):
        missing.append("줄바꿈 구조 불일치")

    return {"status": "warn" if missing else "ok", "missing": missing, "reason": None}


def check_table_block(cells_src: list | None, cells_ko: list | None) -> dict:
    """행·열 개수 + 숫자 셀 원형 보존 검사. 셀 단위로 언어 범위 밖은 건너뛴다 (표 전체를 죽이지 않음)."""
    if not cells_src:
        return {"status": "ok", "missing": [], "reason": None}
    if not cells_ko:
        return {"status": "warn", "missing": ["table not translated"], "reason": None}

    rows_src, rows_ko = len(cells_src), len(cells_ko)
    cols_src = len(cells_src[0]) if cells_src else 0
    cols_ko = len(cells_ko[0]) if cells_ko else 0
    if rows_src != rows_ko or cols_src != cols_ko:
        return {
            "status": "warn",
            "missing": [f"shape mismatch: {rows_src}x{cols_src} (원문) vs {rows_ko}x{cols_ko} (번역)"],
            "reason": None,
        }

    missing = []
    for r_idx, (srow, orow) in enumerate(zip(cells_src, cells_ko)):
        for c_idx, (s, o) in enumerate(zip(srow, orow)):
            if s is None:
                continue
            s_norm = normalize_locale_agnostic(s.strip())
            if not BARE_NUMBER_RE.fullmatch(s_norm):
                continue
            if not _is_english_numeral(s_norm):
                continue  # 이 셀은 언어 범위 밖 — 표 전체가 아니라 이 셀만 건너뜀
            if (o or "").strip() != s.strip():
                missing.append(f"[{r_idx},{c_idx}] {s!r} -> {o!r}")

    return {"status": "warn" if missing else "ok", "missing": missing, "reason": None}


def verify_block(block: dict) -> dict:
    if block["type"] in ("header_footer", "figure"):
        return {"status": "skipped", "missing": [], "reason": f"{block['type']} - not verified"}
    if block["type"] == "table":
        return check_table_block(block["table"]["cells_src"], block["table"]["cells_ko"])
    return check_text_block(block["source"], block["ko"])


def _demo() -> None:
    # 영어 표기, 완전 일치 -> ok
    r = check_text_block("The conductor shall be 2.5 mm² per IEC 60502-2.",
                          "도체(conductor)는 IEC 60502-2에 따라 2.5 mm²여야 한다.")
    assert r["status"] == "ok", r

    # 영어 표기, 수치 누락 -> warn
    r = check_text_block("Rated current is 3,200 A.", "정격 전류이다.")
    assert r["status"] == "warn" and any("3,200" in m or "3200" in m for m in r["missing"]), r

    # 실사용 중 발견된 사고 재현: 번역에 그루지야 문자가 섞임 -> warn
    long_source = "Der Leiterradius und die erforderliche Anzahl an Segmenten wurden iterativ so bestimmt."
    r = check_text_block(long_source, "도체 반경 및 필요한 분할 수는 შესაბამის 반복적으로 결정하였다.")
    assert r["status"] == "warn" and any("예상 밖 문자" in m for m in r["missing"]), r

    # 번역이 원문보다 지나치게 짧음 (중간에 잘림) -> warn
    r = check_text_block(long_source, "도체 반경 및")
    assert r["status"] == "warn" and any("잘렸을 가능성" in m for m in r["missing"]), r

    # 독일식 콤마-소수점 -> skipped (Step 0에서 실측한 71,36 케이스)
    r = check_text_block("Wert beträgt 71,36 mm.", "값은 71,36 mm이다.")
    assert r["status"] == "skipped", r
    assert r["reason"] is not None

    # 콤마 없는 4자리 이상 순수 정수(예: 연도)는 영어 표기로 인정해야 함
    r = check_text_block("Published in February 2026.", "2026년 2월에 발행됨.")
    assert r["status"] == "ok", r

    # 목록 줄바꿈이 유지되면 -> ok, LLM이 목록 항목을 한 줄로 합치면 -> warn
    bulleted = "• Item one\n• Item two\n• Item three"
    r = check_text_block(bulleted, "• 항목 하나\n• 항목 둘\n• 항목 셋")
    assert r["status"] == "ok", r
    r = check_text_block(bulleted, "항목 하나, 항목 둘, 항목 셋")
    assert r["status"] == "warn" and "줄바꿈 구조 불일치" in r["missing"], r

    # 공백 천단위(SI) + 유니코드 분수 + 하이픈 변형은 언어 무관 정규화로 흡수
    normalized = normalize_locale_agnostic("Range: 30–50 ℃, load ½ of 1 234 N")
    assert "1234" in normalized
    assert "0.5" in normalized
    assert "30-50" in normalized

    # 표: shape 일치 + 숫자 셀 보존 -> ok
    r = check_table_block([["Size", "1.5"], ["Weight", "32"]], [["단면적", "1.5"], ["중량", "32"]])
    assert r["status"] == "ok", r

    # 표: shape 불일치 -> warn
    r = check_table_block([["A", "B"], ["1", "2"]], [["가", "나", "다"]])
    assert r["status"] == "warn", r

    # 표: 숫자 셀 손상 -> warn
    r = check_table_block([["Size", "1.5"]], [["단면적", "1.6"]])
    assert r["status"] == "warn", r

    print("check.py self-check OK")


if __name__ == "__main__":
    _demo()
