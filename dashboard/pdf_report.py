"""
A tiny, dependency-free PDF generator for tabular reports (feature 18).

Renders a title and monospaced text lines into a valid multi-page PDF using the
built-in Helvetica font — enough for trade-history / analytics / backtest
exports without pulling in reportlab.  Not a general PDF library; it only does
left-aligned text with automatic pagination.
"""

from __future__ import annotations

from typing import List

_PAGE_W = 612  # US Letter, points
_PAGE_H = 792
_MARGIN = 54
_LINE_H = 14
_FONT_SIZE = 9
_TITLE_SIZE = 15
_LINES_PER_PAGE = int((_PAGE_H - 2 * _MARGIN) / _LINE_H)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _page_stream(lines: List[str], title: str | None) -> str:
    parts = ["BT", f"/F1 {_FONT_SIZE} Tf", f"{_LINE_H} TL"]
    y = _PAGE_H - _MARGIN
    if title:
        parts = [
            "BT", f"/F2 {_TITLE_SIZE} Tf",
            f"1 0 0 1 {_MARGIN} {y} Tm",
            f"({_escape(title)}) Tj", "ET",
            "BT", f"/F1 {_FONT_SIZE} Tf", f"{_LINE_H} TL",
        ]
        y -= _LINE_H * 2
    parts.append(f"1 0 0 1 {_MARGIN} {y} Tm")
    first = True
    for line in lines:
        if first:
            parts.append(f"({_escape(line)}) Tj")
            first = False
        else:
            parts.append(f"T* ({_escape(line)}) Tj")
    parts.append("ET")
    return "\n".join(parts)


def simple_pdf(title: str, lines: List[str]) -> bytes:
    """Return a PDF document (bytes) rendering *title* then *lines*.

    Long inputs paginate automatically; the title is repeated on page 1 only.
    """
    # Split lines into pages (leaving room for the title on the first page).
    pages: List[List[str]] = []
    first_capacity = _LINES_PER_PAGE - 2
    remaining = list(lines)
    pages.append(remaining[:first_capacity])
    remaining = remaining[first_capacity:]
    while remaining:
        pages.append(remaining[:_LINES_PER_PAGE])
        remaining = remaining[_LINES_PER_PAGE:]

    objects: List[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)  # 1-based object number

    # Fonts (Helvetica + Helvetica-Bold).
    font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    kids_placeholder_index = add(b"")  # Pages node; fill after we know kid ids

    page_ids: List[int] = []
    for i, page_lines in enumerate(pages):
        stream = _page_stream(page_lines, title if i == 0 else None).encode("latin-1", "replace")
        content_id = add(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
        page_id = add(
            (
                f"<< /Type /Page /Parent {kids_placeholder_index} 0 R "
                f"/MediaBox [0 0 {_PAGE_W} {_PAGE_H}] "
                f"/Resources << /Font << /F1 {font_regular} 0 R /F2 {font_bold} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode()
        )
        page_ids.append(page_id)

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[kids_placeholder_index - 1] = (
        f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>".encode()
    )
    catalog_id = add(f"<< /Type /Catalog /Pages {kids_placeholder_index} 0 R >>".encode())

    # Assemble the file with a cross-reference table.
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)
