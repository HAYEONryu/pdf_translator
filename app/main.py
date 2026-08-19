"""FastAPI 엔트리 (SPEC.md §8)."""
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

load_dotenv()  # OPENAI_API_KEY 등 — translate/client.py의 OpenAI() 생성보다 먼저 로드돼야 한다

from app.routers.docs import router as docs_router  # noqa: E402
from app.routers.jobs import router as jobs_router  # noqa: E402
from app.routers.upload import router as upload_router  # noqa: E402

app = FastAPI(title="사내 기술문서 번역 도구")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(upload_router)
app.include_router(docs_router)
app.include_router(jobs_router)
