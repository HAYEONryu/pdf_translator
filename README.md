# 사내 기술문서 번역 도구

외국어 기술문서(PDF)를 링크 하나로 업로드해 번역하고, 원문과 좌우 대조로 읽는 내부용 웹 도구.
전체 설계는 [SPEC.md](SPEC.md) 참고 (1차 개발 범위, 구현 순서, 하지 말아야 할 것 포함).

## 요구사항

- Python 3.11+ (개발 환경은 3.14로 검증됨)
- OpenAI API 키

## 설치

```bat
python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

`.env`를 열어 `OPENAI_API_KEY`를 채워 넣는다.

## 실행

```bat
run.bat
```

또는 수동으로:

```bat
call .venv\Scripts\activate
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

브라우저에서 `http://127.0.0.1:8000` 접속 → PDF 업로드 → 페이지 범위 선택 → 대조 뷰.

**`--workers 1`을 반드시 지킬 것.** 잡 상태가 프로세스 메모리에만 있어서 워커가 2개 이상이면
SSE 스트리밍이 깨진다 (SPEC.md §6).

## 용어집 편집

`data/glossary.csv`를 텍스트 에디터나 엑셀로 직접 수정한다. 서버 재시작 불필요 —
파일 변경 시각(mtime)을 감지해 자동으로 다시 읽는다.

```csv
en,ko,note
conductor,도체,
sheath,시스,외피 아님 - 사내 표준
```

## 프로젝트 구조

```
app/
├── main.py, config.py, storage.py, cache.py, glossary.py, ingest.py, export.py
├── pipeline.py         # 페이지 단위 추출→번역→검증 (cli.py, jobs.py가 공유)
├── jobs.py             # 인메모리 잡 레지스트리 + asyncio 동시성 + SSE
├── cli.py              # 단일 페이지 디버그용: python -m app.cli --pdf x.pdf --page 12
├── extract/            # bbox 블록 추출, PDF 렌더, 목차
├── translate/          # OpenAI 라우팅·프롬프트·클라이언트
├── verify/             # 규칙 기반 후검증
├── routers/            # upload / docs(선택·뷰·다운로드) / jobs(SSE)
├── templates/, static/ # Jinja2 + vanilla JS
data/
├── glossary.csv         # 팀 공용 용어집 (git 추적)
└── docs/{sha}/           # 업로드 문서 + 번역 캐시 (git 미추적, 로컬 데이터)
```

## 테스트

프레임워크 없이 각 모듈에 assert 기반 자체 점검이 내장되어 있다 (외부 API를 호출하는
`translate/`·`jobs.py` 일부를 빼면 전부 오프라인/무료로 돈다):

```bat
python -m app.storage
python -m app.cache
python -m app.glossary
python -m app.extract.blocks
python -m app.translate.engine
python -m app.verify.check
python -m app.ingest
python -m app.jobs
```

## 알려진 제한 (1차 범위 밖 / 의도적으로 생략)

- 로그인·DB·보안 심사 없음 (SPEC.md §11)
- 다국어 숫자 표기 검증은 영어식(콤마 천단위/마침표 소수점)만 지원 — 그 외 표기는 번역은
  하되 검증만 건너뛰고 화면에 "검증 생략"으로 표시
- 대조 뷰에서 스크롤 중 미번역 페이지를 즉석으로 추가하는 버튼은 없음 — 범위 선택 화면에서
  다시 선택해서 열면 된다
- 용어 등록 UI 없음 — `data/glossary.csv` 직접 편집

## 2차(사내 서버 배포) 전환 시 손댈 곳

1. `run.bat`/실행 커맨드의 `--host 127.0.0.1` → 사내 접근 가능한 주소
2. `DATA_DIR` 환경변수 → 공유 스토리지 경로
3. `OPENAI_API_KEY` 주입 위치

그 외 코드 변경은 필요 없다 (SPEC.md §1).
