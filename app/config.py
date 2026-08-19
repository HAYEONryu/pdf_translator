"""전역 상수. 패턴·경로·임계치를 여기 모아두고 하드코딩하지 않는다 (SPEC.md §11)."""
import os
import re
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()

# 표/figure 판별 (SPEC.md §5.1 ①③) — ponytail: 휴리스틱 값, 실제 문서로 튜닝 필요
TABLE_MIN_FILLED_RATIO = 0.3
FIGURE_TABLE_MARGIN_PT = 36.0
# 차트 하나가 표 오탐지 후보 여러 개로 쪼개지면 각각 따로 크롭돼 부분만 확대된 이미지가
# 나온다(실사용 중 발견, p19). 세로로 이 거리 이내면서 가로로 겹치는 figure 후보는 하나로
# 합친다 — ponytail: 보수적으로 잡음(60pt), 문서 전체 재스캔으로 과도하게 합쳐지지
# 않는지 확인 후 값을 정함. 너무 멀리 떨어진 후보(예: 120pt+)까지 합치려면 이 값을 올릴 것.
FIGURE_MERGE_MAX_GAP_PT = 60.0

# 블록 그룹핑 (SPEC.md §5.1)
# ★ 5%(0.05)는 실제 샘플 문서의 풋터를 못 잡았다 — 풋터가 페이지 높이의 94.0%(0.940)에
# 있는데 본문 마지막 줄은 89.5% 이하에서 끝나서, 8%(0.08)로 넉넉히 잡아도 본문은 안 걸린다.
HEADER_FOOTER_MARGIN_RATIO = 0.08
SAME_LINE_Y_TOLERANCE_PT = 2.5
PARAGRAPH_GAP_PT = 6.0
HEADING_SIZE_RATIO = 1.2
HEADING_MAX_CHARS = 80

# 수식 오탐지 방지 — 수식 이미지/벡터 안에 흩어진 기호를 pdfplumber가 단어로 주워담으면
# "x 4 8 f y s ; x 2 10 7 k (2.2) s 192 0,8 x 4 s R s s DC" 처럼 토큰 대부분이 1~2글자인
# 문단이 되거나, "0,2857W/mK c2300Ws/kgK 930kg/m 3 PE, , ," 처럼 숫자+단위가 붙어 토큰은
# 길지만 진짜 단어(알파벳만으로 된 3글자 이상)가 거의 없는 문단이 된다. 둘 중 하나라도
# 걸리면 수식으로 보고 figure 취급(번역·검증 제외)한다.
# ponytail: 휴리스틱, 실제 문서로 튜닝 필요 — 너무 낮추면 진짜 짧은 문장을 수식으로 오인함.
FORMULA_SHORT_TOKEN_RATIO = 0.6
FORMULA_MIN_TOKENS = 4
FORMULA_DIGIT_TOKEN_RATIO = 0.4
FORMULA_WORD_TOKEN_MAX_RATIO = 0.2
# 수식 번호 "(2.12)"가 있어도 진짜 문장("Die Gleichungen (2.16) und (2.17) ergeben:")과
# 구분하려면 순수 알파벳 단어 비율을 봐야 한다 — 기호가 하나라도 섞이면 단어로 안 침.
FORMULA_EQ_WORD_MAX_RATIO = 0.3

RENDER_SCALE = 2.0
THUMBNAIL_SCALE = 0.25  # 썸네일 그리드 전용 — 실측 결과, 원본 해상도로 수십 장을 한 번에 로드하면 브라우저 탭이 멈춘다

MAX_UPLOAD_MB = 100

# 예상 소요시간 계산용 (SPEC.md §7.2, §9) — ponytail: 실측 기반 대략치, 정밀 측정 아님
PAGE_TRANSLATE_ETA_SEC = 15

# 번역 모델 (SPEC.md §5.3) — luna만 Step 0에서 실제 API 호출로 검증됨.
# terra/sol은 착수 시 재확인 필요.
MODEL_TEXT_ONLY = "gpt-5.6-luna"
MODEL_WITH_TABLE_OR_FIGURE = "gpt-5.6-terra"
MODEL_SCANNED = "gpt-5.6-terra"

# ★ 프롬프트 텍스트뿐 아니라 추출/분류 로직(수식 판별, 헤더·풋터 마진 등)이 바뀔 때도
# 반드시 올린다 — 캐시 키가 이 값+용어집+모델만 보므로, 안 올리면 로직을 고쳐도 옛날
# 캐시가 그대로 나온다 (실사용 중 발견: 지금은 figure로 잡힐 블록이 예전 캐시엔
# paragraph로 번역된 채 남아있었음).
PROMPT_VER = "v3"

# 규격번호/품번 마스킹 패턴 (SPEC.md §5.4) — ponytail: 휴리스틱, 실제 문서로 튜닝 필요
#
# 끝 경계로 \b 대신 (?![A-Za-z0-9])를 쓴다: 번역문에서는 한국어 조사가 숫자/영문 뒤에
# 공백 없이 바로 붙는다 ("IEC 60502-2에 따라"). 한글 음절도 정규식 \w에 포함되므로
# \b는 "2"와 "에" 사이에서 경계로 인식되지 않아 매칭이 깨진다.
SPEC_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:IEC|ASTM|EN|KS|ISO|DIN|VDE|IEEE)\s?[A-Z]{0,2}\s?\d{3,6}(?:[-/]\d+)*(?![A-Za-z0-9])"
)
PART_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:P/N|Dwg\.?\s?No\.?|Teil-?Nr\.?)\s?[:.]?\s?[A-Z0-9][A-Z0-9-]{3,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
