"""단일 페이지 처리 파이프라인: 추출→캐시조회→번역→검증→캐시저장.

cli.py(Step 2)와 jobs.py(Step 6)가 공유한다. LLM 호출 전(prepare_page)과 후
(translate_and_verify_page)를 나눈 이유는 SPEC.md §6 ⑤ 때문 — 캐시 히트 페이지는
세마포어를 소모하지 않고 즉시 push해야 하므로, 잡 오케스트레이션 쪽에서 캐시 히트
여부를 먼저 알아야 세마포어 진입 여부를 결정할 수 있다.
"""
from app.cache import load_cached_page, save_page
from app.extract.blocks import extract_page_blocks
from app.extract.render import render_page_png
from app.glossary import match_terms
from app.translate.engine import choose_model, retranslate_block, translate_page
from app.verify.check import verify_block


def _page_text_for_glossary(page: dict) -> str:
    """용어 매칭용 페이지 텍스트 — 실제로 번역기에 보내는 블록의 텍스트만 모은다."""
    parts = []
    for b in page["blocks"]:
        if b["type"] in ("header_footer", "figure"):
            continue
        if b["type"] == "table":
            for row in b["table"]["cells_src"] or []:
                parts.extend(cell for cell in row if cell)
        elif b["source"]:
            parts.append(b["source"])
    return " ".join(parts)


def prepare_page(pdf_path: str, doc_sha: str, page_no: int, doc_title: str) -> dict:
    """추출 + 캐시 조회까지. LLM 호출 전 단계 — 세마포어가 필요 없다."""
    page = extract_page_blocks(pdf_path, page_no)
    terms = match_terms(_page_text_for_glossary(page)) if page["has_text_layer"] else []
    model_id = choose_model(page)

    cached = load_cached_page(doc_sha, page_no, terms, model_id)
    if cached is not None:
        return {"cache_hit": True, "page": cached}

    needs_image = (not page["has_text_layer"]) or any(
        b["type"] in ("table", "figure") for b in page["blocks"]
    )
    image_png = render_page_png(pdf_path, page_no) if needs_image else None
    return {
        "cache_hit": False,
        "page": page,
        "terms": terms,
        "model_id": model_id,
        "image_png": image_png,
    }


def translate_and_verify_page(prep: dict, doc_sha: str, page_no: int, doc_title: str) -> dict:
    """LLM 호출 + 검증(+실패 시 재번역) + 캐시 저장. 세마포어로 감싸는 부분."""
    page = prep["page"]
    terms = prep["terms"]

    page = translate_page(page, doc_title, terms, prep["image_png"])

    if page["has_text_layer"]:
        for block in page["blocks"]:
            if block["type"] in ("header_footer", "figure"):
                continue
            result = verify_block(block)
            block["verify"] = result
            if result["status"] == "warn":
                retranslate_block(block, result["missing"], doc_title, terms)
                block["verify"] = verify_block(block)

    save_page(doc_sha, page_no, terms, prep["model_id"], page)
    return page


def process_page(pdf_path: str, doc_sha: str, page_no: int, doc_title: str) -> tuple[dict, bool]:
    """CLI용 동기 원샷 헬퍼. 반환: (page_data, cache_hit 여부)."""
    prep = prepare_page(pdf_path, doc_sha, page_no, doc_title)
    if prep["cache_hit"]:
        return prep["page"], True
    return translate_and_verify_page(prep, doc_sha, page_no, doc_title), False
