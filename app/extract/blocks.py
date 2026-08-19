"""bbox 블록 추출: 표/figure 후보 검증·마진 확장 포함 (SPEC.md §5.1, ①②③④ 순서 고정)."""
import re

import pdfplumber

from app.config import (
    FIGURE_TABLE_MARGIN_PT,
    FORMULA_DIGIT_TOKEN_RATIO,
    FORMULA_EQ_WORD_MAX_RATIO,
    FORMULA_MIN_TOKENS,
    FORMULA_SHORT_TOKEN_RATIO,
    FORMULA_WORD_TOKEN_MAX_RATIO,
    HEADER_FOOTER_MARGIN_RATIO,
    HEADING_MAX_CHARS,
    HEADING_SIZE_RATIO,
    PARAGRAPH_GAP_PT,
    RENDER_SCALE,
    SAME_LINE_Y_TOLERANCE_PT,
    TABLE_MIN_FILLED_RATIO,
)


# 공학 문서의 수식 번호 표기. "(2.12)", "(2.18a)"처럼 정상 문장에는 거의 안 나오는
# 고유 패턴이라, 아주 짧은 수식 조각("Ra ⋅Gr . (2.12)")도 토큰 수와 무관하게 잡아낸다.
_EQUATION_NUMBER_RE = re.compile(r"\(\d{1,2}\.\d{1,3}[a-z]?\)")
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)]+$")


def _is_real_word(token: str) -> bool:
    """수식 기호가 하나라도 섞이면 탈락하는 엄격한 판정 — ③에서 "Die Gleichungen (2.16) und
    (2.17) ergeben:" 같은 진짜 문장과 "Ra ⋅Gr . (2.12)" 같은 수식 조각을 가르는 데 쓴다."""
    stripped = _TRAILING_PUNCT_RE.sub("", token)
    return len(stripped) >= 3 and stripped.isalpha()


def _looks_like_formula(text: str) -> bool:
    """수식 이미지/벡터에서 흩어진 기호를 주워담으면 세 가지 패턴으로 깨진다.
    ① 토큰 대부분이 1~2글자 ("x 4 8 f y s ; x 2 10 7 k (2.2) s 192 0,8 x 4 s R s s DC")
    ② 숫자+단위가 붙어 토큰은 길지만 진짜 단어가 거의 없음
       ("0,2857W/mK c2300Ws/kgK 930kg/m 3 PE, , ,")
    ③ 수식 번호가 붙어 있고 진짜 단어가 거의 없음 ("Ra ⋅Gr . (2.12)") — 조각이 너무 짧아
       ①②를 못 넘는 경우 대비. 문장이 수식 번호를 그냥 언급하는 경우
       ("Die Gleichungen (2.16) und (2.17) ergeben:")와는 단어 비율로 구분한다."""
    # 목차/캡션의 점선 리더("......")처럼 순수 기호로만 된 토큰은 판정에서 제외한다 —
    # 안 그러면 "Abb. 3: ... [Nex2001] ................." 같은 정상 캡션도 걸린다.
    tokens = [t for t in text.split() if any(c.isalnum() for c in t)]
    n = len(tokens)

    if n and _EQUATION_NUMBER_RE.search(text):
        # 종류(distinct) 기준으로 센다 — 수식은 "Kabel Rohr"처럼 같은 첨자 단어를
        # 대여섯 번씩 반복하는데, 등장 횟수로 세면 이게 비율을 왜곡해 진짜 수식을
        # 놓친다 (실사용 중 발견: (2.18a), (2.19) 수식). 진짜 문장은 같은 단어를
        # 그렇게 반복하지 않으므로 종류 수로 세는 편이 더 정확하다.
        distinct_tokens = set(tokens)
        word_ratio = sum(1 for t in distinct_tokens if _is_real_word(t)) / len(distinct_tokens)
        if word_ratio <= FORMULA_EQ_WORD_MAX_RATIO:
            return True

    if n < FORMULA_MIN_TOKENS:
        return False
    # "짧은 토큰"에서 2글자짜리 순수 알파벳 단어("um", "cm", "an")는 뺀다 — 독일어에는 그런
    # 정상 단어가 흔해서, 안 빼면 "für eine um 10 cm" 같은 진짜 문장이 수식으로 오탐된다.
    # 1글자 토큰은 늘 카운트한다 — 수식 변수명(x, y, k, s)은 언어와 무관하게 항상 1글자다.
    short_ratio = sum(1 for t in tokens if len(t) == 1 or (len(t) == 2 and not t.isalpha())) / n
    if short_ratio >= FORMULA_SHORT_TOKEN_RATIO:
        return True
    digit_ratio = sum(1 for t in tokens if any(c.isdigit() for c in t)) / n
    # "단어"는 숫자가 없고 3글자 이상인 토큰으로 판단한다. isalpha()를 쓰면 하이픈 붙은
    # 독일어 복합어("Einleiter-Hochspannungskabel")가 단어로 안 잡혀 참고문헌·캡션이
    # 수식으로 오탐되므로, 하이픈·따옴표가 섞여도 숫자만 없으면 단어로 인정한다.
    word_ratio = sum(1 for t in tokens if len(t) >= 3 and not any(c.isdigit() for c in t)) / n
    return digit_ratio >= FORMULA_DIGIT_TOKEN_RATIO and word_ratio <= FORMULA_WORD_TOKEN_MAX_RATIO


def _bbox_overlap(a, b) -> bool:
    ax0, atop, ax1, abot = a
    bx0, btop, bx1, bbot = b
    return not (ax1 <= bx0 or bx1 <= ax0 or abot <= btop or bbot <= atop)


def _expand_bbox(bbox, margin, page_width, page_height):
    x0, top, x1, bottom = bbox
    return (
        max(0.0, x0 - margin),
        max(0.0, top - margin),
        min(page_width, x1 + margin),
        min(page_height, bottom + margin),
    )


def _table_filled_ratio(cells) -> float:
    flat = [c for row in cells for c in row]
    if not flat:
        return 0.0
    non_empty = sum(1 for c in flat if c not in (None, ""))
    return non_empty / len(flat)


def _classify_table_candidates(page):
    """① 표 후보 유효성 검사 → ② 무효 후보는 figure 후보로 재분류."""
    valid_tables = []
    figure_bboxes = []
    for t in page.find_tables():
        cells = t.extract()
        if _table_filled_ratio(cells) >= TABLE_MIN_FILLED_RATIO:
            valid_tables.append((t, cells))
        else:
            figure_bboxes.append(t.bbox)
    return valid_tables, figure_bboxes


def _word_in_zones(word, zones) -> bool:
    wbox = (word["x0"], word["top"], word["x1"], word["bottom"])
    return any(_bbox_overlap(wbox, z) for z in zones)


def _group_words_to_blocks(words, page_height):
    header_cut = page_height * HEADER_FOOTER_MARGIN_RATIO
    footer_cut = page_height * (1 - HEADER_FOOTER_MARGIN_RATIO)

    words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines, cur_line, cur_top = [], [], None
    for w in words:
        if cur_top is None or abs(w["top"] - cur_top) <= SAME_LINE_Y_TOLERANCE_PT:
            cur_line.append(w)
            cur_top = w["top"] if cur_top is None else cur_top
        else:
            lines.append(cur_line)
            cur_line, cur_top = [w], w["top"]
    if cur_line:
        lines.append(cur_line)

    line_info = []
    for line in lines:
        line = sorted(line, key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in line)
        x0 = min(w["x0"] for w in line)
        x1 = max(w["x1"] for w in line)
        top = min(w["top"] for w in line)
        bottom = max(w["bottom"] for w in line)
        size = sum(w.get("size", 0) for w in line) / len(line)
        line_info.append({"text": text, "bbox": (x0, top, x1, bottom), "size": size})

    sizes = [li["size"] for li in line_info if li["size"] > 0]
    median_size = sorted(sizes)[len(sizes) // 2] if sizes else 0

    blocks = []
    para_lines = []

    def flush():
        if not para_lines:
            return
        x0 = min(li["bbox"][0] for li in para_lines)
        top = min(li["bbox"][1] for li in para_lines)
        x1 = max(li["bbox"][2] for li in para_lines)
        bottom = max(li["bbox"][3] for li in para_lines)
        text = " ".join(li["text"] for li in para_lines)
        avg_size = sum(li["size"] for li in para_lines) / len(para_lines)
        is_heading = (
            len(para_lines) == 1
            and median_size > 0
            and avg_size >= median_size * HEADING_SIZE_RATIO
            and len(text) < HEADING_MAX_CHARS
        )
        if bottom <= header_cut or top >= footer_cut:
            btype = "header_footer"
        elif is_heading:
            btype = "heading"
        elif _looks_like_formula(text):
            btype = "figure"  # 수식 오탐지 — 번역·검증 대상에서 제외 (figure와 동일 취급)
        else:
            btype = "paragraph"
        blocks.append({"type": btype, "bbox": [x0, top, x1, bottom], "source": text})

    prev_bottom = None
    for li in line_info:
        top = li["bbox"][1]
        if prev_bottom is not None and (top - prev_bottom) > PARAGRAPH_GAP_PT:
            flush()
            para_lines = []
        para_lines.append(li)
        prev_bottom = li["bbox"][3]
    flush()

    return blocks


def _scanned_page_result(page_no, page_width, page_height) -> dict:
    return {
        "page_no": page_no,
        "page_width": page_width,
        "page_height": page_height,
        "render_scale": RENDER_SCALE,
        "has_text_layer": False,
        "blocks": [
            {
                "id": f"p{page_no:03d}-b00",
                "type": "paragraph",
                "bbox": [0, 0, page_width, page_height],
                "source": None,
                "table": None,
                "ko": None,
                "verify": {
                    "status": "skipped",
                    "missing": [],
                    "reason": "scanned page - whole-page markdown, block-level verify not applicable",
                },
            }
        ],
    }


def extract_page_blocks(pdf_path, page_no: int) -> dict:
    """1-indexed page_no. §4 블록 스키마 dict를 반환한다."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_no - 1]
        page_width, page_height = page.width, page.height
        all_words = page.extract_words(extra_attrs=["size"])

        if not all_words:
            return _scanned_page_result(page_no, page_width, page_height)

        valid_tables, figure_bboxes = _classify_table_candidates(page)  # ①②

        table_zones = [
            _expand_bbox(t.bbox, FIGURE_TABLE_MARGIN_PT, page_width, page_height)
            for t, _cells in valid_tables
        ]
        figure_zones = [
            _expand_bbox(b, FIGURE_TABLE_MARGIN_PT, page_width, page_height)
            for b in figure_bboxes
        ]  # ③
        exclusion_zones = table_zones + figure_zones

        remaining_words = [w for w in all_words if not _word_in_zones(w, exclusion_zones)]  # ④
        text_blocks = _group_words_to_blocks(remaining_words, page_height)

        blocks = []
        for t, cells in valid_tables:
            blocks.append({
                "type": "table",
                "bbox": list(t.bbox),
                "source": None,
                "table": {
                    "rows": len(cells),
                    "cols": len(cells[0]) if cells else 0,
                    "cells_src": cells,
                    "cells_ko": None,
                },
                "ko": None,
                "verify": {"status": "ok", "missing": [], "reason": None},
            })
        for fb in figure_bboxes:
            blocks.append({
                "type": "figure",
                "bbox": list(fb),
                "source": None,
                "table": None,
                "ko": None,
                "verify": {"status": "skipped", "missing": [], "reason": "figure - not translated/verified"},
            })
        for tb in text_blocks:
            if tb["type"] == "header_footer":
                verify = {"status": "skipped", "missing": [], "reason": "header_footer - not translated/verified"}
            elif tb["type"] == "figure":  # 수식 오탐지로 재분류된 경우
                verify = {"status": "skipped", "missing": [], "reason": "formula - not translated/verified"}
            else:
                verify = {"status": "ok", "missing": [], "reason": None}
            blocks.append({
                "type": tb["type"],
                "bbox": tb["bbox"],
                # 수식으로 재분류된 경우에도 원문은 보존한다 — 번역은 안 하지만 화면에서
                # 원본 그대로 복사할 수 있어야 한다 (표/차트 오탐지 figure는 애초에 텍스트가 없음).
                "source": tb["source"],
                "table": None,
                "ko": None,
                "verify": verify,
            })

        blocks.sort(key=lambda b: (b["bbox"][1], b["bbox"][0]))
        for idx, b in enumerate(blocks):
            b["id"] = f"p{page_no:03d}-b{idx:02d}"

        return {
            "page_no": page_no,
            "page_width": page_width,
            "page_height": page_height,
            "render_scale": RENDER_SCALE,
            "has_text_layer": True,
            "blocks": blocks,
        }


def _demo() -> None:
    """Step 0 스파이크에서 실측한 samplePDF.pdf 특성을 그대로 회귀 검증한다."""
    pdf_path = "samplePDF.pdf"

    # p27: 막대그래프가 find_tables()에 표로 잡히지만 유효성 검사에서 걸러져 figure가 돼야 함
    p27 = extract_page_blocks(pdf_path, 27)
    types27 = {b["type"] for b in p27["blocks"]}
    assert "table" not in types27, "차트가 진짜 표로 오인되면 안 됨"
    assert "figure" in types27, "오탐지된 표 후보는 figure로 재분류되어야 함"
    figure_block = next(b for b in p27["blocks"] if b["type"] == "figure")
    assert figure_block["verify"]["status"] == "skipped"

    # p44: 46행x5열 실제 데이터 표 — 표 제외 로직이 정상 동작해야 함
    p44 = extract_page_blocks(pdf_path, 44)
    table_block = next(b for b in p44["blocks"] if b["type"] == "table")
    assert table_block["table"]["rows"] == 46
    assert table_block["table"]["cols"] == 5
    # 표 안 숫자가 별도 paragraph로 새어나가면 안 됨 (극소수의 각주만 남아야 함)
    paragraph_blocks = [b for b in p44["blocks"] if b["type"] == "paragraph"]
    assert len(paragraph_blocks) <= 2, f"표 내용이 텍스트 블록으로 새어나감: {paragraph_blocks}"

    # ID 형식
    for b in p44["blocks"]:
        assert b["id"].startswith("p044-b")

    # p1: 순수 텍스트 페이지 — 표/figure 없이 heading/paragraph만 나와야 함
    p1 = extract_page_blocks(pdf_path, 1)
    assert p1["has_text_layer"] is True
    assert all(b["type"] in ("heading", "paragraph", "header_footer") for b in p1["blocks"])

    # 수식 오탐지: 사용자가 실제로 겪은 깨진 문단 예시 (① 짧은 토큰 다수)
    assert _looks_like_formula("x 4 8 f y s ; x 2 10 7 k (2.2) s 192 0,8 x 4 s R s s DC")
    # ② 숫자+단위가 붙어 토큰은 길지만 진짜 단어가 거의 없는 경우
    assert _looks_like_formula(" 0,2857W/mK c2300Ws/kgK  930kg/m 3 PE, , ,")
    # 진짜 짧은 문장은 수식으로 오인하면 안 됨 (원문은 항상 외국어 — 번역 대상 소스 언어)
    assert not _looks_like_formula("The conductor shall comply with IEC 60228 Class 2.")
    assert not _looks_like_formula("Der Wert ist sehr wichtig für die Berechnung.")
    # 회귀 방지: 85페이지 전체 스캔에서 실제로 오탐났던 케이스들
    # (목차/그림 캡션의 점선 리더, 참고문헌 항목) — 번역 대상에서 빠지면 안 됨
    assert not _looks_like_formula(
        "Abb. 3: Einleiter-Hochspannungskabel [Nex2001] ................................."
    )
    assert not _looks_like_formula(
        '[Nex2001] Nexans: „HTC 2753-3T", Kabeldatenblatt Nexans 380 kV 2500 mm2.pdf, 01.'
    )
    assert not _looks_like_formula("für eine um 10 cm")
    # 회귀 방지: 짧은 수식 조각(단일 알파벳 변수명 다수)이 위 완화 이후에도 잡혀야 함
    assert _looks_like_formula("k =0,35 , k =0,2 s p")
    assert _looks_like_formula("Ra =Pr·Gr . (2.12)")

    # 수식으로 재분류돼도 원문은 보존되어야 한다 (화면에서 복사할 수 있도록)
    p20 = extract_page_blocks(pdf_path, 20)
    formula_blocks = [b for b in p20["blocks"] if b["verify"]["reason"] == "formula - not translated/verified"]
    assert len(formula_blocks) == 3
    assert all(b["source"] for b in formula_blocks), "수식 블록의 원문이 비어있으면 안 됨"

    # 풋터가 페이지 높이의 94.0%에 있어 기존 5% 기준으로는 안 잡혔다 (실사용 중 발견) —
    # header_footer로 잡혀 번역·검증에서 제외돼야 형식이 고정된다.
    footer_hits = 0
    for pno in (17, 20, 21):
        pg = extract_page_blocks(pdf_path, pno)
        for b in pg["blocks"]:
            if b["type"] == "header_footer" and "Seite" in (b["source"] or ""):
                footer_hits += 1
    assert footer_hits == 3, f"풋터가 header_footer로 안 잡힘: {footer_hits}/3"

    print("blocks.py self-check OK")


if __name__ == "__main__":
    _demo()
