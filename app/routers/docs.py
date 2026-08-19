"""페이지 선택 화면 + 렌더 서빙 + 대조 뷰 화면 + 다운로드 (SPEC.md §7.2, §7.3)."""
import math

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import PAGE_TRANSLATE_ETA_SEC, THUMBNAIL_SCALE
from app.export import export_docx, export_html
from app.extract.render import render_crop_png, render_page_png
from app.rangeutil import parse_range
from app.storage import (
    doc_exists,
    get_stored_pages,
    list_cached_page_numbers,
    load_meta,
    render_path,
    source_pdf_path,
    thumb_path,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _require_meta(sha: str) -> dict:
    if not doc_exists(sha):
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    return load_meta(sha)


def _format_eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}초"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}분 {secs}초" if secs else f"{minutes}분"


@router.get("/doc/{sha}/select")
def select_page(sha: str, request: Request):
    meta = _require_meta(sha)
    return templates.TemplateResponse(
        request, "select.html", {"meta": meta, "cached_pages": list_cached_page_numbers(sha)}
    )


@router.get("/doc/{sha}/render/{page}.png")
def get_render(sha: str, page: int):
    _require_meta(sha)
    path = render_path(sha, page)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_page_png(str(source_pdf_path(sha)), page))
    return Response(content=path.read_bytes(), media_type="image/png")


@router.get("/doc/{sha}/thumb/{page}.png")
def get_thumb(sha: str, page: int):
    """저해상도 썸네일 전용. 수십~수백 장을 그리드로 한 번에 로드하므로 원본 해상도는 쓰지 않는다
    (실측: scale=2.0으로 85장을 한꺼번에 로드하니 브라우저 탭이 멈췄다)."""
    _require_meta(sha)
    path = thumb_path(sha, page)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_page_png(str(source_pdf_path(sha)), page, scale=THUMBNAIL_SCALE))
    return Response(content=path.read_bytes(), media_type="image/png")


@router.get("/doc/{sha}/crop/{page}.png")
def get_crop(sha: str, page: int, x0: float, top: float, x1: float, bottom: float):
    """수식 블록 등 bbox 영역만 이미지로 잘라 서빙한다. PUA 폰트로 깨지는 텍스트 대신
    원본 그대로 보여주기 위함 (§7.3, 사용자 피드백)."""
    _require_meta(sha)
    png = render_crop_png(str(source_pdf_path(sha)), page, (x0, top, x1, bottom))
    return Response(content=png, media_type="image/png")


@router.get("/api/docs/{sha}/estimate")
def api_estimate(sha: str, range: str) -> dict:  # noqa: A002
    _require_meta(sha)
    pages = parse_range(range)
    cached = list_cached_page_numbers(sha)
    reused_count = sum(1 for p in pages if p in cached)
    new_count = len(pages) - reused_count
    eta_sec = math.ceil(new_count / 6) * PAGE_TRANSLATE_ETA_SEC
    return {
        "selected": len(pages),
        "new_count": new_count,
        "reused_count": reused_count,
        "eta_sec": eta_sec,
        "eta_text": _format_eta(eta_sec),
    }


@router.get("/doc/{sha}/view")
def view_page(sha: str, request: Request, range: str):  # noqa: A002
    meta = _require_meta(sha)
    page_numbers = parse_range(range)
    if not page_numbers:
        raise HTTPException(400, "range가 비어 있습니다")
    return templates.TemplateResponse(
        request,
        "view.html",
        {
            "sha": sha,
            "doc_title": meta["filename"],
            "page_numbers": page_numbers,
            "page_width": meta["page_width"],
            "page_height": meta["page_height"],
            "range_str": range,
        },
    )


@router.get("/doc/{sha}/export.html")
def download_html(sha: str, range: str):  # noqa: A002
    meta = _require_meta(sha)
    page_numbers = parse_range(range)
    pages = get_stored_pages(sha, page_numbers)
    ordered = [pages[p] for p in page_numbers if p in pages]
    html = export_html(sha, meta["filename"], ordered)
    return HTMLResponse(content=html, headers={"Content-Disposition": f'attachment; filename="{meta["filename"]}.html"'})


@router.get("/doc/{sha}/export.docx")
def download_docx(sha: str, range: str):  # noqa: A002
    meta = _require_meta(sha)
    page_numbers = parse_range(range)
    pages = get_stored_pages(sha, page_numbers)
    ordered = [pages[p] for p in page_numbers if p in pages]
    data = export_docx(sha, meta["filename"], ordered)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{meta["filename"]}.docx"'},
    )
