"""번역 라우팅·요청 구성·ID 검증·재번역 (SPEC.md §5.3)."""
import base64
import json

from app.config import MODEL_SCANNED, MODEL_TEXT_ONLY, MODEL_WITH_TABLE_OR_FIGURE
from app.translate.client import call_structured
from app.translate.prompts import SCANNED_SYSTEM_PROMPT, SYSTEM_PROMPT

_EXCLUDED_TYPES = ("header_footer", "figure")


def choose_model(page: dict) -> str:
    if not page["has_text_layer"]:
        return MODEL_SCANNED
    has_table_or_figure = any(b["type"] in ("table", "figure") for b in page["blocks"])
    return MODEL_WITH_TABLE_OR_FIGURE if has_table_or_figure else MODEL_TEXT_ONLY


def _translatable_blocks(page: dict) -> list:
    return [b for b in page["blocks"] if b["type"] not in _EXCLUDED_TYPES]


def _build_schema(block_ids: list) -> dict:
    return {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": block_ids},
                        "ko": {"type": ["string", "null"]},
                        "cells_ko": {
                            "type": ["array", "null"],
                            "items": {"type": "array", "items": {"type": ["string", "null"]}},
                        },
                    },
                    "required": ["id", "ko", "cells_ko"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["blocks"],
        "additionalProperties": False,
    }


def _build_user_payload(doc_title: str, terms: list, blocks: list) -> dict:
    payload_blocks = []
    for b in blocks:
        if b["type"] == "table":
            payload_blocks.append({"id": b["id"], "type": "table", "cells_src": b["table"]["cells_src"]})
        else:
            payload_blocks.append({"id": b["id"], "type": b["type"], "source": b["source"]})
    return {"doc_title": doc_title, "terms": terms, "blocks": payload_blocks}


def _estimate_max_tokens(payload: dict) -> int:
    """넉넉히 잡은 출력 토큰 상한. max_completion_tokens를 안 정하면 API 기본값에 걸려
    응답이 문장 중간에서 잘리고(실사용 중 발견), 그 상태로 JSON을 억지로 닫으려다 이상한
    글자가 섞여 나올 수 있다. 소스 글자 수 기반으로 여유 있게(원문 병기·JSON 오버헤드 포함)
    잡는다."""
    total_chars = 0
    for b in payload["blocks"]:
        total_chars += len(b.get("source") or "")
        for row in b.get("cells_src") or []:
            for cell in row:
                total_chars += len(cell or "")
    return max(3000, min(32000, total_chars * 4))


def _image_content(image_png: bytes) -> dict:
    b64 = base64.b64encode(image_png).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}}


def _apply_translation(blocks: list, result_blocks: list) -> None:
    by_id = {b["id"]: b for b in blocks}
    for rb in result_blocks:
        b = by_id.get(rb["id"])
        if b is None:
            continue
        if b["type"] == "table":
            b["table"]["cells_ko"] = rb.get("cells_ko")
        else:
            b["ko"] = rb.get("ko")


def translate_page(page: dict, doc_title: str, terms: list, image_png: bytes | None) -> dict:
    """page(§4 스키마)의 번역 가능 블록을 채워서 반환한다. header_footer/figure는 건드리지 않는다."""
    if not page["has_text_layer"]:
        return _translate_scanned_page(page, doc_title, image_png)

    blocks = _translatable_blocks(page)
    if not blocks:
        return page

    model = choose_model(page)
    block_ids = [b["id"] for b in blocks]
    schema = _build_schema(block_ids)
    payload = _build_user_payload(doc_title, terms, blocks)

    content = [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    if image_png is not None and model != MODEL_TEXT_ONLY:
        content.append(_image_content(image_png))

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]
    max_tokens = _estimate_max_tokens(payload)

    result = call_structured(model, messages, schema, max_completion_tokens=max_tokens)
    _apply_translation(blocks, result["blocks"])

    returned_ids = {b["id"] for b in result["blocks"]}
    if set(block_ids) != returned_ids:
        # ID 불일치 → 1회 재시도 (SPEC.md §5.3)
        result = call_structured(model, messages, schema, max_completion_tokens=max_tokens)
        _apply_translation(blocks, result["blocks"])
        returned_ids = {b["id"] for b in result["blocks"]}
        missing_ids = set(block_ids) - returned_ids
        for b in blocks:
            if b["id"] in missing_ids:
                b["ko"] = None
                if b["type"] == "table":
                    b["table"]["cells_ko"] = None
                b["verify"] = {"status": "warn", "missing": ["block missing from LLM response"], "reason": None}

    return page


def retranslate_block(block: dict, missing_items: list, doc_title: str, terms: list) -> None:
    """검증 실패 블록 1개만 temperature=0으로 재번역 (SPEC.md §5.4 실패 처리)."""
    model = MODEL_WITH_TABLE_OR_FIGURE if block["type"] == "table" else MODEL_TEXT_ONLY
    schema = _build_schema([block["id"]])
    payload = _build_user_payload(doc_title, terms, [block])
    note = (
        f"The following items were missing from your previous translation of this block: {missing_items}. "
        "You MUST include them verbatim in the retranslation."
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False) + "\n\n" + note},
    ]
    result = call_structured(model, messages, schema, temperature=0, max_completion_tokens=_estimate_max_tokens(payload))
    _apply_translation([block], result["blocks"])


def _translate_scanned_page(page: dict, doc_title: str, image_png: bytes | None) -> dict:
    if image_png is None:
        raise ValueError("scanned page translation requires image_png")

    block = page["blocks"][0]
    schema = {
        "type": "object",
        "properties": {
            "source_md": {"type": "string"},
            "ko_md": {"type": "string"},
        },
        "required": ["source_md", "ko_md"],
        "additionalProperties": False,
    }
    messages = [
        {"role": "system", "content": SCANNED_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": json.dumps({"doc_title": doc_title}, ensure_ascii=False)},
                _image_content(image_png),
            ],
        },
    ]
    # 스캔본은 블록이 없어 글자 수로 예산을 못 잡으므로, 전체 페이지 Markdown 두 벌
    # (원문+번역) 분량으로 넉넉히 고정값을 쓴다.
    result = call_structured(
        MODEL_SCANNED, messages, schema, schema_name="scanned_translation", max_completion_tokens=16000
    )
    block["source"] = result["source_md"]
    block["ko"] = result["ko_md"]
    return page
