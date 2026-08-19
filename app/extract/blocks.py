"""bbox 블록 추출: 표/figure 후보 검증·마진 확장 포함 (SPEC.md §5.1, ①②③④ 순서 고정)."""
import re

import pdfplumber

from app.config import (
    FIGURE_MERGE_MAX_GAP_PT,
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

# PDF 내장 Symbol 폰트는 그리스 문자·연산자를 유니코드 매핑 없이 PUA(U+F000대,
# "원래 코드 + 0xF000")로 인코딩하는 경우가 많다 — 실사용 중 발견: "λ = 0,40W/mK"가
# "  0,40W/mK"로 나와 화면에 빈 네모/미표시 문자로 보임. Symbol 폰트는
# a-z를 QWERTY 순서로 그리스 소문자에 대응시키는 게 40년 넘게 고정된 업계 표준
# 인코딩이라(PostScript Symbol font), 이 규칙으로 역매핑한다. 확신 없는 나머지
# PUA 문자는 지우지 않고 그대로 둔다 — 지우면 그 문자가 문단의 유일한 내용일 때
# (실사용 중 발견, p71: 단독 기호 하나짜리 블록) 텍스트가 통째로 사라진다.
_SYMBOL_FONT_MAP: dict[int, str] = {}
for _lat, _grk in zip("abgdezhqiklmnxoprstufcyw", "αβγδεζηθικλμνξοπρστυφχψω"):
    _SYMBOL_FONT_MAP[0xF000 + ord(_lat)] = _grk
for _lat, _grk in zip("ABGDEZHQIKLMNXOPRSTUFCYW", "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"):
    _SYMBOL_FONT_MAP[0xF000 + ord(_lat)] = _grk
_SYMBOL_FONT_MAP[0xF03D] = "="
_SYMBOL_FONT_MAP[0xF02B] = "+"
_SYMBOL_FONT_MAP[0xF0B7] = "·"
_SYMBOL_FONT_MAP[0xF0D7] = "×"
_PUA_RANGE_RE = re.compile(r"[-]")


def _fix_symbol_font_glyphs(text: str) -> str:
    return _PUA_RANGE_RE.sub(lambda m: _SYMBOL_FONT_MAP.get(ord(m.group()), m.group()), text)


# 블록 확정 후 노이즈 정리 — 구두점·공백 잔여물만 정리한다 (그리스 문자·수학 기호
# 자체는 위에서 정상 유니코드로 복원된 뒤이므로 그대로 둔다).
_NOISE_COMMA_RE = re.compile(r"\s*,\s*,+")
_NOISE_SPACE_RE = re.compile(r"[ \t]{2,}")
_NOISE_EDGE_RE = re.compile(r"^[\s,;·]+|[\s,;·]+$")


def _clean_block_noise(text: str) -> str:
    text = _fix_symbol_font_glyphs(text)
    text = _NOISE_COMMA_RE.sub(",", text)
    text = _NOISE_SPACE_RE.sub(" ", text)
    text = _NOISE_EDGE_RE.sub("", text)
    return text.strip()


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
    diagram_bboxes = _detect_vector_diagram_bboxes(page, [t.bbox for t, _cells in valid_tables])
    return valid_tables, _merge_close_bboxes(figure_bboxes + diagram_bboxes, FIGURE_MERGE_MAX_GAP_PT)


_DIAGRAM_CLUSTER_GAP_PT = 35.0
_DIAGRAM_MIN_PRIMITIVES = 8


def _cluster_primitive_bboxes(items: list, max_gap: float) -> list:
    """items: (bbox, is_image) 목록. (bbox, 원소 개수, 이미지 포함 여부)로 뭉친다.
    개수는 이 클러스터가 "진짜 도식"인지 판단하는 데 쓴다 (선 하나짜리 장식 밑줄과
    구분) — 단, 래스터 이미지가 하나라도 있으면 그 자체로 이미 도식이므로 개수와
    무관하게 인정한다 (실사용 중 발견: 실린더 도면이 이미지 하나뿐이라 rect/line
    개수 기준을 못 넘어 사라짐)."""
    if not items:
        return []
    items = sorted(items, key=lambda it: it[0][1])
    b0, img0 = items[0]
    clusters = [[list(b0), 1, img0]]
    for b, is_image in items[1:]:
        bbox, count, has_image = clusters[-1]
        x_overlap = not (b[2] <= bbox[0] or bbox[2] <= b[0])
        gap = b[1] - bbox[3]
        if x_overlap and gap <= max_gap:
            bbox[0] = min(bbox[0], b[0])
            bbox[1] = min(bbox[1], b[1])
            bbox[2] = max(bbox[2], b[2])
            bbox[3] = max(bbox[3], b[3])
            clusters[-1][1] += 1
            clusters[-1][2] = has_image or is_image
        else:
            clusters.append([list(b), 1, is_image])
    return [(tuple(bbox), count, has_image) for bbox, count, has_image in clusters]


def _detect_vector_diagram_bboxes(page, valid_table_bboxes: list) -> list:
    """격자선이 없는 도식(단면도·사진형 도면 등)은 find_tables()가 후보로도 못 잡아서
    라벨이 본문 텍스트로 새어나간다 (실사용 중 발견: p70 "GOK Mutterboden ..." 단면도,
    실린더 도면 옆 "WGB 0001" 라벨). 벡터 도형(rect/line/curve)과 래스터 이미지가
    뭉친 영역을 도식으로 본다 — curves(해칭·곡선 디테일)와 images(사진형 도면)를
    빼면 그림 본체가 통째로 안 잡히고 라벨 하나만 남은 좁고 납작한 영역이 잡힌다."""
    items = [
        ((p["x0"], p["top"], p["x1"], p["bottom"]), False)
        for p in (*page.rects, *page.lines, *page.curves)
    ]
    items += [((im["x0"], im["top"], im["x1"], im["bottom"]), True) for im in page.images]
    bboxes = []
    for bbox, count, has_image in _cluster_primitive_bboxes(items, _DIAGRAM_CLUSTER_GAP_PT):
        if not has_image and count < _DIAGRAM_MIN_PRIMITIVES:
            continue
        if any(_bbox_overlap(bbox, t) for t in valid_table_bboxes):
            continue  # 실제 데이터 표의 테두리/구분선 — 도식이 아니라 표
        bboxes.append(bbox)
    return bboxes


def _merge_close_bboxes(bboxes: list, max_gap: float) -> list:
    """세로로 가깝고 가로로 겹치는 bbox를 하나로 합친다. 차트 하나가 표 오탐지 후보
    여러 개로 쪼개지면 각각 따로 크롭돼 부분만 확대된 이미지가 나온다 (실사용 중 발견, p19)."""
    if not bboxes:
        return []
    boxes = sorted((list(b) for b in bboxes), key=lambda b: b[1])
    merged = [boxes[0]]
    for b in boxes[1:]:
        last = merged[-1]
        x_overlap = not (b[2] <= last[0] or last[2] <= b[0])
        gap = b[1] - last[3]
        if x_overlap and gap <= max_gap:
            last[0] = min(last[0], b[0])
            last[1] = min(last[1], b[1])
            last[2] = max(last[2], b[2])
            last[3] = max(last[3], b[3])
        else:
            merged.append(b)
    return [tuple(m) for m in merged]


def _word_in_zones(word, zones) -> bool:
    wbox = (word["x0"], word["top"], word["x1"], word["bottom"])
    return any(_bbox_overlap(wbox, z) for z in zones)


_SUPERSCRIPT_SIZE_RATIO = 0.8
_SUPERSCRIPT_VERTICAL_SHIFT_PT = 1.0
_SUPERSCRIPT_GAP_PT = 1.5


def _merge_superscripts(line: list) -> list:
    """상첨자/하첨자는 extract_words()가 폰트 크기·베이스라인이 다르다는 이유로
    같은 줄이어도 별도 단어로 떼어낸다 (실사용 중 발견: "963kg/m" + 상첨자 "3" →
    "963kg/m 3"로 깨짐). 바로 붙어 있고(gap 작음) 폰트가 뚜렷이 작고 수직으로
    어긋난 경우에만 원 단어에 도로 붙인다 — 일반 단어 사이 공백은 건드리지 않는다."""
    if not line:
        return line
    merged = [dict(line[0])]
    for w in line[1:]:
        prev = merged[-1]
        gap = w["x0"] - prev["x1"]
        size_drop = w.get("size", 0) > 0 and w["size"] < prev.get("size", 0) * _SUPERSCRIPT_SIZE_RATIO
        vertical_shift = (
            w["top"] < prev["top"] - _SUPERSCRIPT_VERTICAL_SHIFT_PT
            or w["bottom"] > prev["bottom"] + _SUPERSCRIPT_VERTICAL_SHIFT_PT
        )
        if gap <= _SUPERSCRIPT_GAP_PT and size_drop and vertical_shift:
            prev["text"] += w["text"]
            prev["x1"] = max(prev["x1"], w["x1"])
            prev["top"] = min(prev["top"], w["top"])
            prev["bottom"] = max(prev["bottom"], w["bottom"])
        else:
            merged.append(dict(w))
    return merged


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
        line = _merge_superscripts(line)
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
        text = _clean_block_noise(" ".join(li["text"] for li in para_lines))
        if not text:
            return  # 노이즈 정리 후 남는 게 없으면 블록 자체를 만들지 않는다
        avg_size = sum(li["size"] for li in para_lines) / len(para_lines)
        is_heading = (
            len(para_lines) == 1
            and median_size > 0
            and avg_size >= median_size * HEADING_SIZE_RATIO
            and len(text) < HEADING_MAX_CHARS
        )
        # 단독 라틴 1~2글자(숫자 제외)는 도식에서 새어나온 라벨/기호일 뿐 문장이 아니다.
        is_stray_symbol = len(text) <= 2 and not text.isdigit()
        if bottom <= header_cut or top >= footer_cut:
            btype = "header_footer"
        elif is_stray_symbol or _looks_like_formula(text):
            btype = "figure"  # 수식/도식 잔여 기호 — 번역·검증 대상에서 제외
        elif is_heading:
            btype = "heading"
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

        # 유효한 표는 마진 없이 raw bbox로 제외한다 — figure와 달리 표는 이미지로
        # 크롭되지 않으므로, 마진에 걸린 캡션·직전 문장이 통째로 사라져 어디에도
        # 안 남는다 (실사용 중 발견, p18 "Tab. 4:" 캡션·앞 문장 소실). figure는 마진
        # 영역이 곧 크롭 이미지라 축 라벨·범례가 이미지 안에 시각적으로 남으므로 유지.
        table_zones = [tuple(t.bbox) for t, _cells in valid_tables]
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
        for fb in figure_zones:
            # 크롭 이미지는 마진 확장된 zone 그대로 써야 한다 — 텍스트 제외에는
            # 마진을 적용하면서 크롭은 raw bbox로 하면, 마진 안에 있던 캡션·축
            # 라벨이 텍스트로도 안 남고 이미지에도 안 잡혀 완전히 소실된다
            # (실사용 중 발견, p59 "Abb. 26:" 캡션 소실).
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

    # p1: 표지 로고 이미지 하나 + 나머지는 heading/paragraph — 표는 없어야 함
    p1 = extract_page_blocks(pdf_path, 1)
    assert p1["has_text_layer"] is True
    assert all(b["type"] in ("heading", "paragraph", "header_footer", "figure") for b in p1["blocks"])
    assert sum(1 for b in p1["blocks"] if b["type"] == "figure") == 1, "표지 로고 이미지가 figure로 안 잡힘"

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

    # 상첨자 병합: "963kg/m" + 상첨자 "3"이 공백 없이 붙어야 한다 (실사용 중 발견, p20)
    all_p20_text = " ".join(b["source"] or "" for b in p20["blocks"])
    assert "963kg/m3" in all_p20_text, "상첨자가 원 단어에 안 붙음"
    assert "963kg/m 3" not in all_p20_text, "상첨자가 공백 채로 남음"

    # Symbol 폰트 PUA 글리프 복원: "λ"(lambda), "ρ"(rho)가 PUA가 아닌 실제 유니코드로
    # 나와야 한다 (실사용 중 발견 — 화면에 빈 네모로 보이던 문제)
    assert "λ" in all_p20_text, "람다(λ)가 PUA에서 복원 안 됨"
    assert "ρ" in all_p20_text, "로(ρ)가 PUA에서 복원 안 됨"

    # 격자 없는 벡터 도식(단면도) 라벨 유출 방지: p70 "GOK Mutterboden ..." 라벨이
    # 더는 본문 paragraph로 새지 않고 figure bbox 안에 흡수돼야 한다 (실사용 중 발견)
    p70 = extract_page_blocks(pdf_path, 70)
    p70_paragraph_text = " ".join(
        b["source"] or "" for b in p70["blocks"] if b["type"] in ("paragraph", "heading")
    )
    assert "GOK" not in p70_paragraph_text, "도식 라벨이 본문 블록으로 유출됨"
    assert any(b["type"] == "figure" for b in p70["blocks"]), "벡터 도식이 figure로 안 잡힘"

    print("blocks.py self-check OK")


if __name__ == "__main__":
    _demo()
