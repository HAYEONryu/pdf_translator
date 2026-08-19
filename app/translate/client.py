"""OpenAI 래퍼: Structured Outputs 호출 + rate-limit 재시도 (SPEC.md §5.3)."""
import json

import openai
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


class TranslationTruncatedError(Exception):
    """max_completion_tokens에 걸려 응답이 중간에 잘렸다. 부분 JSON을 그대로 쓰면
    번역이 문장 중간에서 끊기거나(사용자 실사용 중 발견) 이상한 글자가 섞여 나올 수 있어,
    조용히 넘기지 않고 에러로 취급해 페이지 단위 재시도로 이어지게 한다."""


@retry(
    retry=retry_if_exception_type(openai.RateLimitError),
    wait=wait_random_exponential(min=1, max=30),
    stop=stop_after_attempt(5),
)
def call_structured(
    model: str,
    messages: list,
    schema: dict,
    schema_name: str = "translation",
    temperature: float | None = None,
    max_completion_tokens: int | None = None,
) -> dict:
    """Structured Outputs로 호출하고 파싱된 JSON dict를 반환한다."""
    kwargs = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_completion_tokens is not None:
        kwargs["max_completion_tokens"] = max_completion_tokens
    resp = get_client().chat.completions.create(
        model=model,
        messages=messages,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        },
        **kwargs,
    )
    choice = resp.choices[0]
    if choice.finish_reason == "length":
        raise TranslationTruncatedError(
            f"응답이 max_completion_tokens({max_completion_tokens})에서 잘렸습니다"
        )
    return json.loads(choice.message.content)
