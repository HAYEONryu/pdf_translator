"""페이지 범위 문자열 파싱. routers/jobs.py, routers/docs.py가 공유한다 (SPEC.md §7.2 입력 문법)."""


def parse_range(range_str: str) -> list[int]:
    """"12-18, 25, 40-47" -> [12, 13, ..., 18, 25, 40, ..., 47] (오름차순, 중복 제거)."""
    pages: set[int] = set()
    for part in range_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            pages.update(range(int(start), int(end) + 1))
        else:
            pages.add(int(part))
    return sorted(pages)
