"""
Word (.docx) files: check that they're safe, and pull out their text and
pictures for the AI. The file itself is stored and sent back exactly as it
came; it is never opened in Word or converted.

A .docx is a zip of XML files. A hostile one can unpack to gigabytes (a "zip
bomb") or carry XML tricks, so sizes are checked from the zip's own list
before anything is unpacked, and the XML is read with defusedxml.
"""

import re
import zipfile
from io import BytesIO

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from vault.conversions import ConversionError

MAX_ENTRIES = 1000
MAX_UNPACKED_BYTES = 100 * 1024 * 1024
MAX_PART_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
MAX_PICTURES = 3
MIN_PICTURE_BYTES = 5 * 1024  # Smaller ones are logos and icons.

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TEXT_PARTS = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")
PICTURE_PARTS = re.compile(r"^word/media/[^/]+\.(png|jpe?g)$", re.IGNORECASE)

DAMAGED = "This Word file looks damaged, so I can't save it. Save it again as .docx or PDF and send it."
HAS_MACROS = "This Word file has macros, so I won't save it. Save it as a normal .docx or PDF and send it again."


def _archive(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ConversionError(DAMAGED) from exc


def check(data: bytes):
    """Refuse a Word file that unpacks too big or has macros. Raises ConversionError."""
    with _archive(data) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES or sum(e.file_size for e in entries) > MAX_UNPACKED_BYTES:
            raise ConversionError(DAMAGED)
        if any(e.filename.lower().endswith("vbaproject.bin") for e in entries):
            raise ConversionError(HAS_MACROS)
        if b"macroEnabled" in _read(archive, "[Content_Types].xml"):
            raise ConversionError(HAS_MACROS)


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        with archive.open(name) as part:
            data = part.read(MAX_PART_BYTES + 1)
    except (KeyError, zipfile.BadZipFile, OSError, ValueError, EOFError) as exc:
        raise ConversionError(DAMAGED) from exc
    if len(data) > MAX_PART_BYTES:
        raise ConversionError(DAMAGED)
    return data


def _paragraph(element) -> str:
    pieces = []
    for node in element.iter():
        if node.tag == f"{W}t" and node.text:
            pieces.append(node.text)
        elif node.tag == f"{W}tab":
            pieces.append(" ")
        elif node.tag in (f"{W}br", f"{W}cr"):
            pieces.append("\n")
    return "".join(pieces).strip()


def _lines(element):
    """Text in reading order: one line per paragraph, table cells joined with " | "."""
    for child in element:
        if child.tag == f"{W}p":
            yield _paragraph(child)
        elif child.tag == f"{W}tbl":
            for row in child.findall(f"{W}tr"):
                cells = [" ".join(filter(None, map(_paragraph, cell.iter(f"{W}p")))) for cell in row.findall(f"{W}tc")]
                yield " | ".join(cells)
        else:
            yield from _lines(child)


def text(data: bytes) -> str:
    """The words in a Word file (body first, then headers and footers), at most MAX_TEXT_CHARS."""
    with _archive(data) as archive:
        names = sorted((n for n in archive.namelist() if TEXT_PARTS.match(n)), key=lambda n: (n != "word/document.xml", n))
        lines, size = [], 0
        for name in names:
            try:
                root = ElementTree.fromstring(_read(archive, name))
            except (ElementTree.ParseError, DefusedXmlException) as exc:
                raise ConversionError(DAMAGED) from exc
            for line in _lines(root):
                if line.strip(" |"):
                    lines.append(line)
                    size += len(line) + 1
                if size > MAX_TEXT_CHARS:
                    return "\n".join(lines)[:MAX_TEXT_CHARS]
    return "\n".join(lines)


def pictures(data: bytes, limit: int = MAX_PICTURES) -> list[bytes]:
    """The biggest few photos inside (e.g. a scan pasted into the file), biggest first."""
    with _archive(data) as archive:
        found = [e for e in archive.infolist() if PICTURE_PARTS.match(e.filename) and e.file_size >= MIN_PICTURE_BYTES]
        found.sort(key=lambda e: -e.file_size)
        return [_read(archive, e.filename) for e in found[:limit]]
