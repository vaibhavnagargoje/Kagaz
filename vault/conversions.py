"""
File conversions people need for online forms: shrink to a size limit,
image <-> PDF, merge several files into one PDF, and resize photos or
signatures to exact pixel sizes.

Everything works on bytes in memory so it is easy to test. The limits below
keep one huge or hostile file (a "file bomb") from using up the server's
memory: sizes are checked from a file's header before anything is decoded.
"""

import threading
import time
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

import img2pdf
import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

register_heif_opener()  # iPhone photos (HEIC).

PDF = "application/pdf"
JPEG = "image/jpeg"
HEIC = "image/heic"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
# Pillow's names for the photo formats we open. Nothing else is ever decoded.
IMAGE_FORMATS = {"JPEG": JPEG, "PNG": "image/png", "WEBP": "image/webp", "HEIF": HEIC}
# What can be saved. HEIC is turned into JPEG when it's saved.
SAVEABLE_TYPES = {PDF, HEIC, DOCX} | IMAGE_TYPES
# Brands in an HEIC file's header ("ftyp" box).
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}

A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
MAX_PAGES_TO_RASTERIZE = 40
MAX_JPG_PAGES = 5
MAX_PDF_PAGES = 200
MAX_MERGE_FILES = 20
MAX_MERGE_PAGES = 200
# 60 megapixels: more than any 50 MP phone camera.
MAX_IMAGE_PIXELS = 60_000_000
# One PDF page drawn as a picture, and all the pages of one PDF together.
MAX_RENDER_PIXELS = 25_000_000
MAX_PDF_RENDER_PIXELS = 50_000_000
COMPRESS_SECONDS = 60

# What people can ask for. Outside this, that part of the request is skipped
# and they're told why.
MIN_SIDE_PX, MAX_SIDE_PX = 16, 5000
MIN_TARGET_KB, MAX_TARGET_KB = 5, 100_000

# Converting can take a few hundred MB of memory for a moment, so each
# process does at most this many at once; the rest wait their turn. Take it
# only around a whole job, never inside one (they would wait on each other).
HEAVY_WORK = threading.BoundedSemaphore(2)

TOO_BIG_PHOTO = (
    f"This photo is too big for me. I can take photos up to {MAX_IMAGE_PIXELS // 1_000_000} megapixels, "
    "so please send a smaller copy."
)


class ConversionError(Exception):
    """A reason, safe to show the user, why a file couldn't be converted."""


@dataclass
class FileData:
    data: bytes
    mime: str
    name: str

    @property
    def size_kb(self) -> int:
        return max(1, round(len(self.data) / 1024))


def _whole(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


@dataclass(frozen=True)
class SendOptions:
    target_kb: int = 0
    fmt: str = "original"  # "original", "pdf" or "jpg"
    width: int = 0
    height: int = 0
    merge: bool = False
    # Why part of the request was skipped (e.g. a size too big to make), safe to show.
    notes: tuple = field(default=(), compare=False)

    def __post_init__(self):
        """Keep sizes within what can be made, and note why anything was dropped."""
        notes = list(self.notes)
        width, height, target_kb = _whole(self.width), _whole(self.height), _whole(self.target_kb)
        problem = ""
        if (width or height) and not (width and height):
            problem = "To resize, tell me both the width and the height, like 200x230 px."
        elif width and max(width, height) > MAX_SIDE_PX:
            problem = f"The biggest I can make is {MAX_SIDE_PX} × {MAX_SIDE_PX} px, so I didn't resize it."
        elif width and min(width, height) < MIN_SIDE_PX:
            problem = f"The smallest I can make is {MIN_SIDE_PX} × {MIN_SIDE_PX} px, so I didn't resize it."
        if problem:
            notes.append(problem)
            width = height = 0
        if 0 < target_kb < MIN_TARGET_KB:
            notes.append(f"The smallest size I can aim for is {MIN_TARGET_KB} KB, so I didn't shrink it.")
            target_kb = 0
        elif target_kb > MAX_TARGET_KB:
            target_kb = 0  # Every file is already smaller than that.
        object.__setattr__(self, "target_kb", target_kb)
        object.__setattr__(self, "fmt", self.fmt if self.fmt in ("pdf", "jpg") else "original")
        object.__setattr__(self, "width", width)
        object.__setattr__(self, "height", height)
        object.__setattr__(self, "merge", bool(self.merge))
        object.__setattr__(self, "notes", tuple(dict.fromkeys(notes)))

    @property
    def converts(self) -> bool:
        return bool(self.target_kb or self.fmt != "original" or (self.width and self.height) or self.merge)


@dataclass
class Prepared:
    files: list[FileData]
    operations: list[str] = field(default_factory=list)
    met_target: bool = True
    notes: list[str] = field(default_factory=list)


def is_pdf(mime: str) -> bool:
    return mime == PDF


def is_image(mime: str) -> bool:
    return mime in IMAGE_TYPES


def is_word(mime: str) -> bool:
    return mime == DOCX


def _is_docx(data: bytes) -> bool:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return "word/document.xml" in archive.NameToInfo
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def detect_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    """The real type of a file, from its first bytes (what browsers and WhatsApp say can't be trusted)."""
    if data[:5] == b"%PDF-":
        return PDF
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp" and data[8:12] in HEIF_BRANDS:
        return HEIC
    if data[:4] == b"PK\x03\x04" and _is_docx(data):
        return DOCX
    return fallback


def with_extension(name: str, extension: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}{extension}"


# --- Images -----------------------------------------------------------------


def check_image(data: bytes) -> Image.Image:
    """Open a photo without decoding it, refusing one that's too big to handle."""
    try:
        img = Image.open(BytesIO(data), formats=list(IMAGE_FORMATS))
    except Image.DecompressionBombError as exc:
        raise ConversionError(TOO_BIG_PHOTO) from exc
    except (UnidentifiedImageError, OSError) as exc:
        raise ConversionError("I couldn't open this image.") from exc
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise ConversionError(TOO_BIG_PHOTO)
    return img


def open_image(data: bytes, max_side: int = 0) -> Image.Image:
    """Open an image upright (phone photos carry EXIF rotation) as RGB.

    With `max_side`, a big JPEG is decoded straight at a smaller size (at
    least `max_side` on each side), which needs far less memory.
    """
    img = check_image(data)
    try:
        if max_side and img.format in ("JPEG", "MPO"):  # MPO: a phone JPEG with extra pictures inside.
            img.draft("RGB", (max_side, max_side))
        img = ImageOps.exif_transpose(img)
        if max_side and min(img.size) >= 2 * max_side and img.mode != "P":
            img = img.reduce(min(img.size) // max_side)  # Shrink before making copies of it.
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ConversionError("I couldn't open this image.") from exc
    if img.mode in ("RGBA", "LA", "P"):
        img = img if img.mode == "RGBA" else img.convert("RGBA")
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.getchannel("A"))
        return background
    return img.convert("RGB")


def encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    img.save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def heic_to_jpeg(data: bytes) -> bytes:
    """iPhone photos are saved as JPEG: no form accepts HEIC."""
    return encode_jpeg(open_image(data), 92)


def _scaled(img: Image.Image, factor: float) -> Image.Image:
    width, height = img.size
    size = (max(1, round(width * factor)), max(1, round(height * factor)))
    return img.resize(size, Image.Resampling.LANCZOS)


def _best_jpeg_under(img: Image.Image, limit: int, min_quality: int) -> bytes | None:
    """Highest-quality JPEG of `img` that fits in `limit` bytes, or None."""
    low, high, best = min_quality, 92, None
    while low <= high:
        quality = (low + high) // 2
        encoded = encode_jpeg(img, quality)
        if len(encoded) <= limit:
            best, low = encoded, quality + 1
        else:
            high = quality - 1
    return best


def compress_image(data: bytes, target_bytes: int) -> tuple[bytes, bool]:
    """Shrink an image to a JPEG of at most `target_bytes`.

    Lowers quality first, then size, so text stays readable as long as possible.
    Returns the result and whether it fit.
    """
    img = open_image(data, max_side=2000)
    if max(img.size) > 2000:
        img = _scaled(img, 2000 / max(img.size))
    while True:
        encoded = _best_jpeg_under(img, target_bytes, min_quality=35)
        if encoded:
            return encoded, True
        if min(img.size) <= 150:
            break
        img = _scaled(img, 0.8)
    last_try = encode_jpeg(img, 15)
    return last_try, len(last_try) <= target_bytes


def image_to_jpeg(data: bytes) -> bytes:
    return encode_jpeg(open_image(data), 92)


def resize_image(data: bytes, width: int, height: int) -> bytes:
    """Crop-to-fill to exactly width x height pixels (what exam forms ask for)."""
    img = ImageOps.fit(open_image(data, max_side=max(width, height)), (width, height), Image.Resampling.LANCZOS)
    return encode_jpeg(img, 92)


def image_to_pdf(data: bytes) -> bytes:
    img = open_image(data, max_side=3000)  # Still over 250 dpi on A4.
    page = A4 if img.height >= img.width else (A4[1], A4[0])
    layout = img2pdf.get_layout_fun(page, fit=img2pdf.FitMode.shrink)
    return img2pdf.convert(encode_jpeg(img, 90), layout_fun=layout)


# --- PDFs -------------------------------------------------------------------


def _open_pdfium(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ConversionError("This PDF is password-protected. Send an unlocked copy and I can convert it.") from exc
        raise ConversionError("I couldn't open this PDF.") from exc


def pdf_page_count(data: bytes) -> int | None:
    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            return None
        return len(reader.pages)
    except (PdfReadError, ValueError, OSError):
        return None


def pdf_pick_pages(data: bytes, first: int, last: int = 0) -> bytes:
    """A PDF of just its first `first` pages and its last `last` pages (the whole PDF if it's short)."""
    reader = PdfReader(BytesIO(data))
    total = len(reader.pages)
    if total <= first + last:
        return data
    writer = PdfWriter()
    for page in [*reader.pages[:first], *reader.pages[total - last:]]:
        writer.add_page(page)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _pdf_lossless(data: bytes) -> bytes:
    """Recompress content streams and drop duplicate objects. Keeps text sharp."""
    try:
        writer = PdfWriter(clone_from=BytesIO(data))
        for page in writer.pages:
            page.compress_content_streams()
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        buffer = BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception:  # Any pypdf failure just means "no lossless gain".
        return data


def _render(page: pdfium.PdfPage, dpi: int, max_pixels: int = MAX_RENDER_PIXELS) -> Image.Image:
    """Draw one PDF page as an RGB picture, at a lower resolution if the page is huge."""
    width, height = page.get_size()  # In points (1/72 inch), so a crafted page can be enormous.
    scale = dpi / 72
    pixels = max(1.0, width * scale) * max(1.0, height * scale)
    if pixels > max_pixels:
        scale *= (max_pixels / pixels) ** 0.5
    try:
        return page.render(scale=scale).to_pil().convert("RGB")
    except pdfium.PdfiumError as exc:
        raise ConversionError("I couldn't read a page of this PDF.") from exc


def _render_pages(pdf: pdfium.PdfDocument, dpi: int) -> list[Image.Image]:
    max_pixels = min(MAX_RENDER_PIXELS, MAX_PDF_RENDER_PIXELS // max(1, len(pdf)))
    images = []
    for index in range(len(pdf)):
        page = pdf[index]
        try:
            images.append(_render(page, dpi, max_pixels))
        finally:
            page.close()  # Frees the pictures pdfium decoded for this page.
    return images


def _jpegs_to_pdf(jpegs: list[bytes], dpi: int) -> bytes:
    return img2pdf.convert(jpegs, layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)))


def _too_many_pages(count: int) -> ConversionError:
    return ConversionError(f"This PDF has {count} pages, which is too many to shrink.")


def compress_pdf(data: bytes, target_bytes: int) -> tuple[bytes, bool]:
    """Shrink a PDF to at most `target_bytes`.

    Tries a lossless rewrite first. If that isn't enough, renders each page to
    a JPEG at falling resolutions, which is how scanned documents get small.
    Gives up after COMPRESS_SECONDS with the smallest result so far.
    """
    if len(data) <= target_bytes:
        return data, True
    pdf = _open_pdfium(data)
    try:
        if len(pdf) > MAX_PDF_PAGES:
            raise _too_many_pages(len(pdf))
        lossless = _pdf_lossless(data)
        if len(lossless) <= target_bytes:
            return lossless, True
        if len(pdf) > MAX_PAGES_TO_RASTERIZE:
            raise _too_many_pages(len(pdf))

        deadline = time.monotonic() + COMPRESS_SECONDS
        smallest = lossless
        for dpi in (150, 120, 100, 85, 72, 60, 50):
            if time.monotonic() > deadline:
                break
            pages = _render_pages(pdf, dpi)
            low, high, best = 30, 85, None
            while low <= high and time.monotonic() <= deadline:
                quality = (low + high) // 2
                candidate = _jpegs_to_pdf([encode_jpeg(p, quality) for p in pages], dpi)
                if len(candidate) <= target_bytes:
                    best, low = candidate, quality + 1
                else:
                    high = quality - 1
                    if len(candidate) < len(smallest):
                        smallest = candidate
            if best:
                return best, True
            del pages  # Before drawing them again, smaller.
        return smallest, False
    finally:
        pdf.close()


def pdf_to_jpegs(data: bytes, dpi: int = 150) -> tuple[list[bytes], int]:
    """JPEGs of the first MAX_JPG_PAGES pages, and how many pages the PDF has."""
    pdf = _open_pdfium(data)
    try:
        pages = [pdf[i] for i in range(min(len(pdf), MAX_JPG_PAGES))]
        return [encode_jpeg(_render(page, dpi), 90) for page in pages], len(pdf)
    finally:
        pdf.close()


def _first_page_image(item: FileData, dpi: int, max_side: int) -> Image.Image:
    if is_pdf(item.mime):
        pdf = _open_pdfium(item.data)
        try:
            if len(pdf) == 0:
                raise ConversionError(f"{item.name} has no pages.")
            return _render(pdf[0], dpi)
        finally:
            pdf.close()
    if is_image(item.mime):
        return open_image(item.data, max_side=max_side)
    raise ConversionError(f"I can only show PDFs and photos, not {item.name}.")


def preview_jpeg(data: bytes, mime: str, max_side: int = 1200) -> bytes:
    """A JPEG of the first page, for showing on the website.

    We always serve our own JPEG instead of the uploaded file, so a crafted
    file can never run code in the browser.
    """
    img = _first_page_image(FileData(data, mime, "file"), dpi=100, max_side=max_side)
    img.thumbnail((max_side, max_side))
    return encode_jpeg(img, 80)


SHEET_DPI = 150


def card_sheet(front: FileData, back: FileData) -> bytes:
    """Both sides of a card on one A4 page, front above back, like a photocopy.

    Most forms want "Aadhaar (both sides)" as a single file.
    """
    width, height = round(210 / 25.4 * SHEET_DPI), round(297 / 25.4 * SHEET_DPI)
    margin, gap = 90, 60
    box = (width - 2 * margin, (height - 2 * margin - gap) // 2)
    page = Image.new("RGB", (width, height), "white")
    top = margin
    for item in (front, back):
        side = _first_page_image(item, dpi=SHEET_DPI, max_side=max(box))
        side.thumbnail(box, Image.Resampling.LANCZOS)
        page.paste(side, ((width - side.width) // 2, top))
        top += box[1] + gap
    return img2pdf.convert(encode_jpeg(page, 88), layout_fun=img2pdf.get_fixed_dpi_layout_fun((SHEET_DPI, SHEET_DPI)))


def merge_to_pdf(files: list[FileData]) -> bytes:
    if len(files) > MAX_MERGE_FILES:
        raise ConversionError(f"I can combine up to {MAX_MERGE_FILES} files at once.")
    writer = PdfWriter()
    for item in files:
        if is_pdf(item.mime):
            pdf_bytes = item.data
        elif is_image(item.mime):
            pdf_bytes = image_to_pdf(item.data)
        else:
            raise ConversionError(f"I can only combine PDFs and photos, not {item.name}.")
        try:
            reader = PdfReader(BytesIO(pdf_bytes))
            if reader.is_encrypted:
                raise ConversionError(f"{item.name} is password-protected, so I can't combine it.")
            if len(writer.pages) + len(reader.pages) > MAX_MERGE_PAGES:
                raise ConversionError(f"Together these have more than {MAX_MERGE_PAGES} pages, too many for one PDF.")
            writer.append(reader)
        except PdfReadError as exc:
            raise ConversionError(f"I couldn't read {item.name}.") from exc
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- Putting it together ----------------------------------------------------


def word_as_is(name: str) -> str:
    return (
        f"{name} is a Word file, so I sent it as it is. To get a PDF, open it in Word or Google Docs, "
        "then File → Save as (or Download) → PDF."
    )


def _change_format(item: FileData, fmt: str) -> tuple[list[FileData], str]:
    """The file in the new format, and a note if pages were left out."""
    if fmt == "pdf" and is_image(item.mime):
        return [FileData(image_to_pdf(item.data), PDF, with_extension(item.name, ".pdf"))], ""
    if fmt == "jpg" and is_pdf(item.mime):
        pages, total = pdf_to_jpegs(item.data)
        note = f"{item.name} has {total} pages; I sent the first {len(pages)} as JPG." if total > len(pages) else ""
        if len(pages) == 1:
            return [FileData(pages[0], JPEG, with_extension(item.name, ".jpg"))], note
        return [
            FileData(page, JPEG, with_extension(item.name, f"_page{number}.jpg"))
            for number, page in enumerate(pages, 1)
        ], note
    if fmt == "jpg" and is_image(item.mime) and item.mime != JPEG:
        return [FileData(image_to_jpeg(item.data), JPEG, with_extension(item.name, ".jpg"))], ""
    return [item], ""


def prepare(files: list[FileData], options: SendOptions) -> Prepared:
    """Apply merge -> format -> resize -> compress, in that order.

    Word files are always sent as they are.
    """
    result = Prepared(files=list(files), notes=list(options.notes))
    if options.converts:
        result.notes.extend(word_as_is(item.name) for item in result.files if is_word(item.mime))

    if options.merge and len(result.files) > 1:
        words = [item for item in result.files if is_word(item.mime)]
        others = [item for item in result.files if not is_word(item.mime)]
        if len(others) > 1:
            others = [FileData(merge_to_pdf(others), PDF, "Kagaz_combined.pdf")]
            result.operations.append("merge")
        result.files = others + words

    if options.fmt != "original":
        changed = []
        for item in result.files:
            converted, note = _change_format(item, options.fmt)
            if converted != [item]:
                result.operations.append(f"to_{options.fmt}")
            if note:
                result.notes.append(note)
            changed.extend(converted)
        result.files = changed

    if options.width and options.height:
        resized = []
        for item in result.files:
            if is_image(item.mime):
                data = resize_image(item.data, options.width, options.height)
                resized.append(FileData(data, JPEG, with_extension(item.name, ".jpg")))
                result.operations.append("resize")
            else:
                resized.append(item)
                if is_pdf(item.mime):
                    result.notes.append(f"{item.name} is a PDF, so I didn't resize it. Ask for it as JPG to resize.")
        result.files = resized

    if options.target_kb:
        target_bytes = options.target_kb * 1024
        compressed = []
        for item in result.files:
            if len(item.data) <= target_bytes or is_word(item.mime):
                compressed.append(item)
                continue
            if is_pdf(item.mime):
                data, fits = compress_pdf(item.data, target_bytes)
                new_item = FileData(data, PDF, item.name)
            elif is_image(item.mime):
                data, fits = compress_image(item.data, target_bytes)
                new_item = FileData(data, JPEG, with_extension(item.name, ".jpg"))
            else:
                raise ConversionError("I can only shrink PDFs and photos.")
            result.operations.append("compress")
            if not fits:
                result.met_target = False
                result.notes.append(
                    f"The smallest I could make {new_item.name} is {new_item.size_kb} KB "
                    f"(you asked for {options.target_kb} KB)."
                )
            compressed.append(new_item)
        result.files = compressed

    result.operations = list(dict.fromkeys(result.operations))
    return result
