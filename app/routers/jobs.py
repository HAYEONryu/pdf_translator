"""잡 생성/스트리밍/취소 + 저장된 페이지 조회 엔드포인트 (SPEC.md §6)."""
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.jobs import cancel_job, create_job, get_job
from app.rangeutil import parse_range
from app.storage import get_stored_pages

router = APIRouter(prefix="/api")


class CreateJobRequest(BaseModel):
    doc_sha: str
    doc_title: str
    pages: list[int]


@router.post("/jobs")
async def api_create_job(body: CreateJobRequest) -> dict:
    # ★ async def 필수 — sync 핸들러는 FastAPI가 워커 스레드에서 돌리는데, 그 스레드에는
    # 실행 중인 이벤트 루프가 없어 create_job() 내부의 asyncio.create_task()가 죽는다.
    if not body.pages:
        raise HTTPException(400, "pages는 비어 있을 수 없습니다")
    job_id = create_job(body.doc_sha, body.doc_title, body.pages)
    return {"job_id": job_id}


@router.post("/jobs/{job_id}/cancel")
def api_cancel_job(job_id: str) -> dict:
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(404, "job not found")
    return {"cancelled": True}


def _sse_line(event_id: int, data: dict) -> str:
    return f"id: {event_id}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_events(job_id: str, start_after: int):
    import asyncio

    cursor = start_after + 1
    while True:
        job = get_job(job_id)
        if job is None:
            yield _sse_line(-1, {"type": "page_error", "page": 0, "reason": "job not found"})
            return
        while cursor < len(job.events):
            event = job.events[cursor]
            yield _sse_line(cursor, event)
            if event["type"] == "job_done":
                return
            cursor += 1
        await asyncio.sleep(0.3)


@router.get("/jobs/{job_id}/stream")
async def api_stream_job(job_id: str, request: Request) -> StreamingResponse:
    last_event_id = request.headers.get("Last-Event-ID")
    start_after = int(last_event_id) if last_event_id is not None else -1
    return StreamingResponse(_stream_events(job_id, start_after), media_type="text/event-stream")


@router.get("/docs/{sha}/pages")
def api_get_stored_pages(sha: str, range: str) -> dict:  # noqa: A002 (SPEC.md의 쿼리 파라미터명 그대로)
    """새로고침·재접속용. 잡 상태가 아니라 파일에서 직접 읽는다 (SPEC.md §6)."""
    pages = get_stored_pages(sha, parse_range(range))
    return {"pages": {str(k): v for k, v in pages.items()}}
