"""단일 페이지 파이프라인: 추출→번역→검증 (SPEC.md §10 Step 2).

사용법: python -m app.cli --pdf x.pdf --page 12
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.pipeline import process_page


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(pdf_path: str, page_no: int) -> dict:
    doc_sha = _file_sha256(pdf_path)
    page, cache_hit = process_page(pdf_path, doc_sha, page_no, doc_title=pdf_path)
    if cache_hit:
        print(f"[cache HIT] p{page_no:03d}", file=sys.stderr)
    return page


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="단일 페이지 추출→번역→검증 파이프라인")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--page", required=True, type=int)
    args = parser.parse_args()

    page = run(args.pdf, args.page)
    print(json.dumps(page, ensure_ascii=False, indent=2))

    warn = sum(1 for b in page["blocks"] if b["verify"]["status"] == "warn")
    skipped = sum(1 for b in page["blocks"] if b["verify"]["status"] == "skipped")
    print(f"\n--- 검증 경고 {warn}건 · 검증 생략 {skipped}건 ---", file=sys.stderr)


if __name__ == "__main__":
    main()
