"""Text extraction from files: PDF, Word, plain text and images. No channel code here."""

import base64
import io
import logging
from dataclasses import dataclass
from pathlib import Path

import llm

log = logging.getLogger("docs")

MAX_BYTES = 20 * 1024 * 1024  # Telegram bot download limit
TEXT_EXTS = {".txt", ".md"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_EXTS = {".pdf", ".docx"} | TEXT_EXTS | IMAGE_EXTS

# A PDF with fewer text characters per page than this is treated as scanned.
SCANNED_CHARS_PER_PAGE = 80
SCANNED_MAX_PAGES = 5

VISION_PROMPT = (
    "Transcribe all text in these images exactly as written. Keep headings, lists "
    "and dates. For tables or schedule grids (for example a class schedule "
    "screenshot), write one line per entry like: Day | Time | Course | Room. "
    "For handwriting or whiteboards, transcribe what you can read. Do not add "
    "anything that is not in the images. Output plain text only."
)


class UnsupportedFile(Exception):
    pass


@dataclass
class Extracted:
    text: str
    pages: int | None = None  # page count for PDFs
    method: str = "text"  # text, docx, vision


def is_supported(file_name: str) -> bool:
    return Path(file_name).suffix.lower() in SUPPORTED_EXTS


async def extract(path: Path) -> Extracted:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return await _pdf(path)
    if ext == ".docx":
        return Extracted(_docx(path), method="docx")
    if ext in TEXT_EXTS:
        return Extracted(path.read_text(encoding="utf-8", errors="replace"))
    if ext in IMAGE_EXTS:
        return Extracted(await _vision([_image_png(path.read_bytes())]), method="vision")
    raise UnsupportedFile(ext)


async def _pdf(path: Path) -> Extracted:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = len(reader.pages)
    text = "\n\n".join((p.extract_text() or "").strip() for p in reader.pages).strip()
    if len(text) >= SCANNED_CHARS_PER_PAGE * max(1, min(pages, SCANNED_MAX_PAGES)):
        return Extracted(text, pages=pages)

    # Scanned PDF: render the first pages and read them with the vision model.
    log.info("%s looks scanned (%d chars, %d pages); using vision", path.name, len(text), pages)
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    images = []
    for i in range(min(len(pdf), SCANNED_MAX_PAGES)):
        pil = pdf[i].render(scale=2).to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        images.append(buf.getvalue())
    vision_text = await _vision(images)
    return Extracted(vision_text, pages=pages, method="vision")


def _docx(path: Path) -> str:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    parts = []
    # Walk the body in order so tables stay next to the text around them.
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = Paragraph(child, d).text.strip()
            if t:
                parts.append(t)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip()
                    if t and (not cells or cells[-1] != t):  # merged cells repeat
                        cells.append(t)
                if cells:
                    parts.append(" | ".join(cells))
    return "\n".join(parts)


def _image_png(data: bytes) -> bytes:
    """Normalize any image to PNG, capped at a size the vision model handles well."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.thumbnail((2048, 2048))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _vision(images: list[bytes]) -> str:
    content = [{"type": "text", "text": VISION_PROMPT}]
    for data in images:
        b64 = base64.b64encode(data).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return (await llm.chat([{"role": "user", "content": content}])).strip()
