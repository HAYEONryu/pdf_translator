"""인메모리 잡 레지스트리 + asyncio 동시성 + SSE 이벤트 로그 (SPEC.md §6).

★ uvicorn --workers 1 고정 필수 — 잡 레지스트리가 프로세스 메모리에만 있어서
워커가 2개 이상이면 SSE 구독이 잡을 시작한 워커와 다른 워커에 연결될 수 있다.

잡 상태는 여기(메모리)에만 있고, 완료된 페이지 결과는 파일(cache.py)에 있다.
새로고침 시 잡을 조회하지 말고 파일을 읽으라는 것이 SPEC.md의 설계 의도이므로,
이 모듈은 "진행 중 스트리밍"만 책임지고 영속화는 전혀 하지 않는다.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field

from app.pipeline import prepare_page, translate_and_verify_page
from app.storage import source_pdf_path

MAX_CONCURRENCY = 6


@dataclass
class Job:
    id: str
    doc_sha: str
    doc_title: str
    page_numbers: list[int]
    events: list[dict] = field(default_factory=list)
    cancelled: bool = False
    done_count: int = 0
    warn_count: int = 0
    skipped_count: int = 0
    created_at: float = field(default_factory=time.time)


_JOBS: dict[str, Job] = {}


def get_job(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def create_job(doc_sha: str, doc_title: str, page_numbers: list[int]) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = Job(id=job_id, doc_sha=doc_sha, doc_title=doc_title, page_numbers=list(page_numbers))
    _JOBS[job_id] = job
    # BackgroundTasks는 요청 종료와 함께 취소되므로 쓰지 않는다 (SPEC.md §6 ④).
    asyncio.create_task(_run_job(job))
    return job_id


def cancel_job(job_id: str) -> bool:
    job = _JOBS.get(job_id)
    if job is None:
        return False
    job.cancelled = True
    return True


def _emit(job: Job, event: dict) -> None:
    job.events.append(event)


def _count_verify(job: Job, blocks: list[dict]) -> None:
    for b in blocks:
        status = b["verify"]["status"]
        if status == "warn":
            job.warn_count += 1
        elif status == "skipped" and b["type"] not in ("header_footer", "figure"):
            job.skipped_count += 1


async def _handle_page(job: Job, pdf_path: str, semaphore: asyncio.Semaphore, page_no: int) -> None:
    if job.cancelled:
        return
    try:
        prep = await asyncio.to_thread(prepare_page, pdf_path, job.doc_sha, page_no, job.doc_title)
        if prep["cache_hit"]:
            page = prep["page"]  # 세마포어 없이 즉시 (SPEC.md §6 ⑤)
        else:
            if job.cancelled:
                return
            async with semaphore:
                if job.cancelled:
                    return
                page = await asyncio.to_thread(
                    translate_and_verify_page, prep, job.doc_sha, page_no, job.doc_title
                )
    except Exception as e:  # noqa: BLE001 — 페이지 하나의 실패가 잡 전체를 죽이면 안 된다
        _emit(job, {"type": "page_error", "page": page_no, "reason": str(e)})
        return

    job.done_count += 1
    _count_verify(job, page["blocks"])
    # ② 검증(+재번역) 완료 후에만 push — 이 시점 이전엔 어떤 이벤트도 내보내지 않는다.
    _emit(job, {
        "type": "page_done",
        "page": page_no,
        "page_width": page["page_width"],
        "page_height": page["page_height"],
        "blocks": page["blocks"],
        "cached": prep["cache_hit"],
    })
    _emit(job, {"type": "progress", "done": job.done_count, "total": len(job.page_numbers)})


async def _run_job(job: Job) -> None:
    pdf_path = str(source_pdf_path(job.doc_sha))
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    await asyncio.gather(*(_handle_page(job, pdf_path, semaphore, p) for p in job.page_numbers))
    _emit(job, {"type": "job_done", "warn_count": job.warn_count, "skipped_count": job.skipped_count})


def _demo() -> None:
    """실제 캐시 히트 경로(비용 없음) + 취소 경로를 asyncio.run으로 직접 검증한다."""
    import hashlib
    from pathlib import Path

    async def _main() -> None:
        pdf_path = "samplePDF.pdf"
        doc_sha = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()

        # Step 5에서 이미 캐시된 실제 페이지(1p)로 히트 경로 검증 — LLM 호출 없음
        job_id = create_job(doc_sha, pdf_path, [1])
        job = get_job(job_id)
        for _ in range(200):
            if job.events and job.events[-1]["type"] == "job_done":
                break
            await asyncio.sleep(0.02)
        types = [e["type"] for e in job.events]
        assert types == ["page_done", "progress", "job_done"], types
        assert job.events[0]["cached"] is True
        assert job.events[1]["done"] == 1 and job.events[1]["total"] == 1

        # 즉시 취소 -> 캐시 히트 페이지도 전혀 처리되지 않아야 함
        job_id2 = create_job(doc_sha, pdf_path, [1])
        job2 = get_job(job_id2)
        job2.cancelled = True
        for _ in range(200):
            if job2.events and job2.events[-1]["type"] == "job_done":
                break
            await asyncio.sleep(0.02)
        assert [e["type"] for e in job2.events] == ["job_done"], job2.events
        assert job2.events[0]["warn_count"] == 0 and job2.events[0]["skipped_count"] == 0

        print("jobs.py self-check OK")

    asyncio.run(_main())


if __name__ == "__main__":
    _demo()
