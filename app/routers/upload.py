"""업로드 화면 + 업로드 API (SPEC.md §7.1)."""
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import MAX_UPLOAD_MB
from app.ingest import ingest_pdf
from app.storage import list_recent_docs

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/")
def upload_page(request: Request):
    return templates.TemplateResponse(request, "upload.html", {"recent_docs": list_recent_docs()})


@router.post("/api/upload")
async def api_upload(file: UploadFile) -> dict:
    if file.content_type != "application/pdf":
        raise HTTPException(400, "PDF 파일만 업로드할 수 있습니다")

    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"{MAX_UPLOAD_MB}MB를 초과하는 파일입니다")

    sha, reused = ingest_pdf(data, file.filename or "문서.pdf")
    return {"sha": sha, "reused": reused}
