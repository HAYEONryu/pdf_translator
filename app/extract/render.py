"""PyMuPDF 페이지 PNG 렌더링 (SPEC.md §5, render_scale=2.0 고정)."""
import fitz

from app.config import RENDER_SCALE


def render_page_png(pdf_path, page_no: int, scale: float = RENDER_SCALE) -> bytes:
    """1-indexed page_no. PNG bytes를 반환한다."""
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_no - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        return pix.tobytes("png")
    finally:
        doc.close()


def render_crop_png(pdf_path, page_no: int, bbox, scale: float = RENDER_SCALE, margin: float = 2.0) -> bytes:
    """bbox(PDF pt, [x0, top, x1, bottom]) 영역만 잘라 PNG로 반환한다.

    수식은 PDF 내장 Symbol/수식 폰트의 Private Use Area 코드로 저장된 경우가 많아,
    텍스트로 추출하면 원래 폰트 없이는 읽을 수 없는 글자가 된다 (예: \\uf06c, \\uf03d).
    그래서 수식 블록은 텍스트가 아니라 원본 그대로 보이는 이미지로 잘라서 보여준다.
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_no - 1]
        x0, top, x1, bottom = bbox
        clip = fitz.Rect(x0 - margin, top - margin, x1 + margin, bottom + margin) & page.rect
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip)
        return pix.tobytes("png")
    finally:
        doc.close()
