"""bbox 블록 추출: 표/figure 후보 검증·마진 확장 포함 (SPEC.md §5.1, ①②③④ 순서 고정)."""
import re
from dataclasses import dataclass

import pdfplumber

from app.config import (
    FIGURE_MERGE_MAX_GAP_PT,
    FORMULA_DIGIT_TOKEN_RATIO,
    FORMULA_EQ_WORD_MAX_RATIO,
    FORMULA_MIN_TOKENS,
    FORMULA_SHORT_TOKEN_RATIO,
    FORMULA_WORD_TOKEN_MAX_RATIO,
    HEADER_FOOTER_MARGIN_RATIO,
    HEADING_MAX_CHARS,
    HEADING_SIZE_RATIO,
    MIN_LONG_CELLS,
    MIN_TABLE_CELLS,
    RENDER_SCALE,
    TABLE_MIN_FILLED_RATIO,
)


@dataclass
class PageMetrics:
    """페이지별 본문 폰트 크기·줄간격 실측치. 문서마다 폰트 크기가 달라 고정 pt
    상수는 어떤 문서에선 너무 크고 어떤 문서에선 너무 작다 — 아래 임계값들을
    이 실측치의 배수로 계산해 문서에 맞춰 스케일한다."""

    body_size: float
    line_gap: float
    page_width: float
    page_height: float

    @property
    def paragraph_gap(self) -> float:
        # 이 값은 줄 bbox 사이 "여백"(top-to-top 줄간격이 아니라 잉크 경계 사이 공백)과
        # 비교된다 — line_gap(baseline 간 pitch)은 스케일이 안 맞아 여기엔 못 쓴다.
        return self.body_size * 0.6

    @property
    def superscript_gap(self) -> float:
        return self.body_size * 0.18

    @property
    def figure_margin(self) -> float:
        return self.body_size * 3.2

    @property
    def min_table_width(self) -> float:
        return self.page_width * 0.26

    @property
    def min_table_height(self) -> float:
        return self.body_size * 3.5


def measure_page(words: list, page_width: float, page_height: float) -> PageMetrics:
    """words는 extract_page_blocks가 이미 뽑아둔 page.extract_words() 결과를 그대로
    받는다 — pdfplumber의 단어 추출은 페이지당 한 번이면 충분해 다시 부르지 않는다."""
    sizes = sorted(w["size"] for w in words if w.get("size", 0) > 0)
    body_size = sizes[len(sizes) // 2] if sizes else 10.0

    # 줄간격: 인접한 서로 다른 baseline 사이 거리의 중앙값
    tops = sorted({round(w["top"], 1) for w in words})
    gaps = [b - a for a, b in zip(tops, tops[1:]) if 0 < b - a < body_size * 3]
    line_gap = sorted(gaps)[len(gaps) // 2] if gaps else body_size * 1.2

    return PageMetrics(body_size, line_gap, page_width, page_height)


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
_SYMBOL_FONT_MAP[0xF0B7] = "•"
_SYMBOL_FONT_MAP[0xF0D7] = "×"
_SYMBOL_FONT_MAP.update({
    0xF04A: "ϑ",   # J -> vartheta  (this document's temperature symbol)
    0xF06A: "φ",   # j -> phi variant
    0xF02D: "−",   # - -> minus ("10^-6"'s minus sign)
    0xF02F: "/",
    0xF0A3: "≤", 0xF0B3: "≥", 0xF0B9: "≠", 0xF0BB: "≈",
    0xF0B1: "±", 0xF0A5: "∞", 0xF0D6: "√",
    0xF0E5: "∑", 0xF0F2: "∫", 0xF0B6: "∂", 0xF0D1: "∇",
    0xF0AE: "→", 0xF0B0: "°",
})
# Symbol fonts keep digits/brackets/punctuation at the same ASCII code point.
for _c in "0123456789()[]{}.,;:!?<>*":
    _SYMBOL_FONT_MAP.setdefault(0xF000 + ord(_c), _c)

_PUA_RANGE_RE = re.compile(r"[-]")


# 복원 후에도 남는 PUA(매핑표에 없는 글리프) — 이게 2개 이상이면 이 블록은 폰트
# 없이는 못 읽는 진짜 깨진 텍스트로 보고 번역을 시도하지 않는다 (아래 flush()에서 사용).
_PUA_LEFT_RE = re.compile("[-]")


def _fix_symbol_font_glyphs(text: str) -> str:
    return _PUA_RANGE_RE.sub(lambda m: _SYMBOL_FONT_MAP.get(ord(m.group()), m.group()), text)


# 블록 확정 후 노이즈 정리 — 구두점·공백 잔여물만 정리한다 (그리스 문자·수학 기호
# 자체는 위에서 정상 유니코드로 복원된 뒤이므로 그대로 둔다).
_NOISE_COMMA_RE = re.compile(r"\s*,\s*,+")
_NOISE_SPACE_RE = re.compile(r"[ \t]{2,}")     # \s 쓰면 \n이 죽음
_NOISE_EDGE_RE  = re.compile(r"^[ \t,;]+|[ \t,;·]+$")   # \s → 공백/탭으로


def _clean_block_noise(text: str) -> str:
    text = _fix_symbol_font_glyphs(text)
    text = _NOISE_COMMA_RE.sub(",", text)
    text = _NOISE_SPACE_RE.sub(" ", text)
    text = _NOISE_EDGE_RE.sub("", text)
    return text.strip()


_UNIT_SUP_RE = re.compile(r"\b(mm|cm|m|kg|km)\s*([23])\b")


def _clean_cells(cells: list) -> list:
    """t.extract()로 나온 표 셀 문자열은 본문 단어 경로(_clean_block_noise 등)를 안
    거쳐 PUA 글리프·상첨자 분리가 그대로 남는다 (실사용 중 발견: "3200 mm2" — 원문은
    "3200 mm²". t.extract()는 word 좌표 없이 셀을 평문화해서, 상첨자가 공백 있는
    "mm 2"와 붙어 있는 "mm2" 둘 다로 나온다). PUA 복원·노이즈 정리는 그대로
    재사용하고, 상첨자는 위치 기반 복원이 불가능하므로 "단위 뒤 고아 숫자 2/3"
    패턴만 차선책으로 위첨자화한다."""
    def clean(c):
        if not c:
            return c
        text = _clean_block_noise(c)
        return _UNIT_SUP_RE.sub(lambda m: m.group(1) + ("²" if m.group(2) == "2" else "³"), text)
    return [[clean(c) for c in row] for row in cells]


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


def _is_real_table(t, cells, metrics: PageMetrics) -> bool:
    if _table_filled_ratio(cells) < TABLE_MIN_FILLED_RATIO:
        return False
    rows, cols = len(cells), (len(cells[0]) if cells else 0)
    if rows < 2 or cols < 2 or rows * cols < MIN_TABLE_CELLS:
        return False
    x0, top, x1, bottom = t.bbox
    if (x1 - x0) < metrics.min_table_width or (bottom - top) < metrics.min_table_height:
        return False  # ★ 라벨 크기 박스는 여기서 걸림
    long_cells = sum(1 for row in cells for c in row if c and len(c.strip()) >= 5)
    return long_cells >= MIN_LONG_CELLS


def _classify_table_candidates(page, metrics: PageMetrics):
    """① 표 후보 유효성 검사 → ② 무효 후보는 figure 후보로 재분류."""
    valid_tables = []
    figure_bboxes = []
    for t in page.find_tables():
        cells = t.extract()
        if _is_real_table(t, cells, metrics):
            valid_tables.append((t, _clean_cells(cells)))
        else:
            figure_bboxes.append(t.bbox)
    diagram_bboxes = _detect_vector_diagram_bboxes(page, [t.bbox for t, _cells in valid_tables])
    return valid_tables, _merge_close_bboxes(figure_bboxes + diagram_bboxes, FIGURE_MERGE_MAX_GAP_PT)


_DIAGRAM_CLUSTER_GAP_PT = 35.0
_DIAGRAM_MIN_PRIMITIVES = 8


def _bboxes_near(a, b, gap: float) -> bool:
    """x/y 갭을 각각 독립적으로 본다 — x가 안 겹쳐도 가까우면 인접으로 친다
    (실사용 중 발견: 3D 플롯의 그래프 본체와 축 눈금은 x 겹침 없이 옆으로 떨어져
    있는데 같은 도식이다). 이전엔 x 겹침을 요구해서 이런 경우가 안 붙었다."""
    near_x = not (b[0] - a[2] > gap or a[0] - b[2] > gap)
    near_y = not (b[1] - a[3] > gap or a[1] - b[3] > gap)
    return near_x and near_y


def _cluster_primitive_bboxes(items: list, max_gap: float) -> list:
    """items: (bbox, is_image) 목록. (bbox, 원소 개수, 이미지 포함 여부)로 뭉친다.
    개수는 이 클러스터가 "진짜 도식"인지 판단하는 데 쓴다 (선 하나짜리 장식 밑줄과
    구분) — 단, 래스터 이미지가 하나라도 있으면 그 자체로 이미 도식이므로 개수와
    무관하게 인정한다 (실사용 중 발견: 실린더 도면이 이미지 하나뿐이라 rect/line
    개수 기준을 못 넘어 사라짐).

    2단계로 병합한다 — 원소가 페이지당 수만 개(curves 해칭 디테일)까지 나와서,
    A-B-C 체인을 전부 서로 비교하는 전이적 병합을 처음부터 하면 O(n²)이라 너무
    느리다 (실측: 한 페이지 14000+ curves).
    ① y 정렬 후 단일 패스로 빠르게 큰 덩어리로 줄인다 (O(n log n)).
    ② ①로 크게 줄어든 소수의 후보만 전이적으로 한 번 더 붙인다 — 정렬 순서상
       안 붙는 A-B-C 체인(가운데 B가 다리 역할)을 여기서 마저 붙인다. 변화가
       없을 때까지 반복한다. 후보 수가 적어(보통 수십 개 이하) O(m²) 반복이어도
       안전하다."""
    if not items:
        return []

    # ① 빠른 1차 병합
    sorted_items = sorted(items, key=lambda it: it[0][1])
    b0, img0 = sorted_items[0]
    merged = [[list(b0), 1, img0]]
    for b, is_image in sorted_items[1:]:
        last = merged[-1]
        if _bboxes_near(last[0], b, max_gap):
            last[0][0] = min(last[0][0], b[0])
            last[0][1] = min(last[0][1], b[1])
            last[0][2] = max(last[0][2], b[2])
            last[0][3] = max(last[0][3], b[3])
            last[1] += 1
            last[2] = last[2] or is_image
        else:
            merged.append([list(b), 1, is_image])

    # ② 전이적 2차 병합 — ①이 만든 소수의 후보에만 적용.
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            if merged[i] is None:
                continue
            for j in range(i + 1, len(merged)):
                if merged[j] is None:
                    continue
                a, b = merged[i], merged[j]
                if _bboxes_near(a[0], b[0], max_gap):
                    a[0][0] = min(a[0][0], b[0][0])
                    a[0][1] = min(a[0][1], b[0][1])
                    a[0][2] = max(a[0][2], b[0][2])
                    a[0][3] = max(a[0][3], b[0][3])
                    a[1] += b[1]
                    a[2] = a[2] or b[2]
                    merged[j] = None
                    changed = True
        merged = [m for m in merged if m is not None]

    return [(tuple(bbox), count, has_image) for bbox, count, has_image in merged]


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


def _merge_superscripts(line: list, metrics: PageMetrics) -> list:
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
        if gap <= metrics.superscript_gap and size_drop and vertical_shift:
            prev["text"] += w["text"]
            prev["x1"] = max(prev["x1"], w["x1"])
            prev["top"] = min(prev["top"], w["top"])
            prev["bottom"] = max(prev["bottom"], w["bottom"])
        else:
            merged.append(dict(w))
    return merged


def _same_line(w, cur_top, cur_bottom):
    """폰트 크기가 달라도(첨자) 세로로 겹치면 같은 줄로 본다."""
    overlap = min(w["bottom"], cur_bottom) - max(w["top"], cur_top)
    h = min(w["bottom"] - w["top"], cur_bottom - cur_top)
    return h > 0 and overlap / h >= 0.5


def _build_line_info(words, metrics: PageMetrics) -> list:
    """단어를 같은 y좌표(줄)로 묶고 상첨자를 병합해, 줄 단위 텍스트+bbox 목록을 만든다.
    문단 그룹핑(_group_words_to_blocks)과 도식 근처 라벨 흡수(_absorb_orphan_labels)가
    같은 줄 단위 표현을 공유한다."""
    words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    lines, cur_line, cur_top, cur_bottom = [], [], None, None
    for w in words:
        if cur_top is None or _same_line(w, cur_top, cur_bottom):
            cur_line.append(w)
            cur_top = w["top"] if cur_top is None else min(cur_top, w["top"])
            cur_bottom = w["bottom"] if cur_bottom is None else max(cur_bottom, w["bottom"])
        else:
            lines.append(cur_line)
            cur_line, cur_top, cur_bottom = [w], w["top"], w["bottom"]
    if cur_line:
        lines.append(cur_line)

    line_info = []
    for line in lines:
        line = sorted(line, key=lambda w: w["x0"])
        line = _merge_superscripts(line, metrics)
        # Symbol 폰트 PUA 글리프를 여기서 미리 복원해야 한다 — _join_lines()의 불릿
        # 판정(_BULLET_RE)이 이 줄 텍스트를 보는데, PUA를 나중에(_clean_block_noise)
        # 복원하면 그때는 이미 문단 전체가 공백으로 합쳐진 뒤라 불릿 줄바꿈을 놓친다
        # (실사용 중 발견: Symbol 폰트 "•"가 개행 없이 문장 중간에 이어 붙음).
        text = _fix_symbol_font_glyphs(" ".join(w["text"] for w in line))
        x0 = min(w["x0"] for w in line)
        x1 = max(w["x1"] for w in line)
        top = min(w["top"] for w in line)
        bottom = max(w["bottom"] for w in line)
        size = sum(w.get("size", 0) for w in line) / len(line)
        line_info.append({"text": text, "bbox": (x0, top, x1, bottom), "size": size})
    return line_info


_AXIS_LABEL_RE = re.compile(r"^[-+]?[\d.,]+(?:[eE][-+]?\d+)?$")
_ORPHAN_LABEL_REACH_PT = 45.0


def _absorb_orphan_labels(figure_zones: list, lines: list, reach: float = _ORPHAN_LABEL_REACH_PT) -> list:
    """도식 근처의 "축 눈금처럼 생긴 줄"을 zone에 흡수한다 — find_tables()/벡터
    클러스터링 둘 다 못 잡는 경우가 있다: 축 눈금 숫자만 딱 떨어져 있으면 rect/line/
    curve가 없어 도식 후보에도 안 걸리고, 본문 words 제외에도 안 걸려 paragraph로
    새어나간다(실사용 중 발견). 줄 전체가 숫자형이거나(눈금) 아주 짧으면(범례 토막)
    도식 라벨로 보고, zone과 가까우면(reach pt 이내) zone을 그 줄까지 확장한다."""
    zones = [list(z) for z in figure_zones]
    for li in lines:
        toks = li["text"].split()
        if not toks:
            continue
        # 전부 숫자형이거나, 평균 토큰 길이가 아주 짧으면 축 라벨/범례로 본다
        numeric = sum(1 for t in toks if _AXIS_LABEL_RE.match(t)) / len(toks)
        if numeric < 0.7 and len(li["text"]) > 25:
            continue
        x0, top, x1, bot = li["bbox"]
        for z in zones:
            if (x0 < z[2] + reach and x1 > z[0] - reach
                    and top < z[3] + reach and bot > z[1] - reach):
                z[0], z[1] = min(z[0], x0), min(z[1], top)
                z[2], z[3] = max(z[2], x1), max(z[3], bot)
                break
    return [tuple(z) for z in zones]

_BULLET_RE = re.compile(r"^\s*(?:[•▪◦‣∙]\s*|[·\-–—]\s+)")

def _join_lines(lines):
    out = ""
    for li in lines:
        t = li["text"]
        if _BULLET_RE.match(t):
            out = (out + "\n" + t) if out else t     # 불릿 줄은 개행 유지
        elif out.endswith("-") and t[:1].islower():
            out = out[:-1] + t
        else:
            out = (out + " " + t) if out else t
    return out


def _group_words_to_blocks(words, page_height, metrics: PageMetrics):
    header_cut = page_height * HEADER_FOOTER_MARGIN_RATIO
    footer_cut = page_height * (1 - HEADER_FOOTER_MARGIN_RATIO)

    line_info = _build_line_info(words, metrics)

    sizes = [li["size"] for li in line_info if li["size"] > 0]
    median_size = sorted(sizes)[len(sizes) // 2] if sizes else 0
    # 캡션처럼 줄간격이 본문보다 넓은 텍스트는 고정 6pt 기준으로는 쪼개진다 —
    # 문서 실측 본문 크기·줄간격 배수로 계산해 문서마다 스케일한다 (실사용 중 발견).
    paragraph_gap = metrics.paragraph_gap

    blocks = []
    para_lines = []

    def flush():
        if not para_lines:
            return
        x0 = min(li["bbox"][0] for li in para_lines)
        top = min(li["bbox"][1] for li in para_lines)
        x1 = max(li["bbox"][2] for li in para_lines)
        bottom = max(li["bbox"][3] for li in para_lines)
        text = _clean_block_noise(_join_lines(para_lines))
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
        # 매핑표에 없는 Symbol 폰트 PUA 글리프가 2개 이상 남으면 원래 폰트 없이는
        # 못 읽는 문자라, 번역을 시도하는 대신 원본 그대로 보존하는 figure로 뺀다.
        has_unreliable_pua = len(_PUA_LEFT_RE.findall(text)) >= 2
        if bottom <= header_cut or top >= footer_cut:
            btype = "header_footer"
        elif is_stray_symbol or has_unreliable_pua or _looks_like_formula(text):
            btype = "figure"  # 수식/도식 잔여 기호 — 번역·검증 대상에서 제외
        elif is_heading:
            btype = "heading"
        else:
            btype = "paragraph"
        blocks.append({"type": btype, "bbox": [x0, top, x1, bottom], "source": text})

    prev_bottom = None
    for li in line_info:
        top = li["bbox"][1]
        if prev_bottom is not None and (top - prev_bottom) > paragraph_gap:
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

        metrics = measure_page(all_words, page_width, page_height)

        valid_tables, figure_bboxes = _classify_table_candidates(page, metrics)  # ①②

        # 유효한 표는 마진 없이 raw bbox로 제외한다 — figure와 달리 표는 이미지로
        # 크롭되지 않으므로, 마진에 걸린 캡션·직전 문장이 통째로 사라져 어디에도
        # 안 남는다 (실사용 중 발견, p18 "Tab. 4:" 캡션·앞 문장 소실). figure는 마진
        # 영역이 곧 크롭 이미지라 축 라벨·범례가 이미지 안에 시각적으로 남으므로 유지.
        table_zones = [tuple(t.bbox) for t, _cells in valid_tables]
        figure_zones = [
            _expand_bbox(b, metrics.figure_margin, page_width, page_height)
            for b in figure_bboxes
        ]  # ③
        # 축 눈금 숫자만 딱 떨어져 있으면 rect/line/curve가 없어 도식 후보에도
        # 안 걸리고 본문 제외에도 안 걸려 paragraph로 새어나간다 — 근처 zone으로 흡수.
        figure_zones = _absorb_orphan_labels(figure_zones, _build_line_info(all_words, metrics))
        exclusion_zones = table_zones + figure_zones

        remaining_words = [w for w in all_words if not _word_in_zones(w, exclusion_zones)]  # ④
        text_blocks = _group_words_to_blocks(remaining_words, page_height, metrics)

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

    # Symbol 폰트 PUA 확장 매핑: "ϑ"(vartheta, 이 문서의 온도 기호)가 복원되고,
    # 매핑 안 된 PUA 잔여 문자가 하나도 없어야 한다 (실사용 중 발견)
    assert "ϑ" in all_p20_text, "vartheta가 PUA에서 복원 안 됨"
    assert not re.search(r"[-]", all_p20_text), "PUA 잔여 문자 있음"

    # 같은 줄 판정을 top거리 대신 세로 겹침 비율로 바꾼 회귀 방지: 첨자(ϑ0)가
    # 다른 폰트 크기 때문에 별도 줄로 분리되면 안 됨 (실사용 중 발견)
    assert "ϑ0" in all_p20_text or "ϑ₀" in all_p20_text, "아래첨자가 분리됨"

    # 도식 라벨/축 눈금 유출 방지 (실사용 중 발견: 전류 밀도 라벨 중복, 축 눈금 숫자 유출)
    p19 = extract_page_blocks(pdf_path, 19)
    para19 = " ".join(
        b["source"] or "" for b in p19["blocks"] if b["type"] in ("paragraph", "heading")
    )
    assert "Stromdichte" not in para19, "도식 라벨이 본문으로 유출됨"
    assert "1.5e+06" not in para19, "축 눈금이 본문으로 유출됨"

    print("blocks.py self-check OK")


if __name__ == "__main__":
    _demo()
