"""
File conversions people need for online forms: shrink to a size limit,
image <-> PDF, merge several files into one PDF, and resize photos or
signatures to exact pixel sizes.

Everything works on bytes in memory so it is easy to test.
"""

from dataclasses import dataclass, field
from io import BytesIO

import img2pdf
import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

PDF = "application/pdf"
JPEG = "image/jpeg"
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

A4 = (img2pdf.mm_to_pt(210), img2pdf.mm_to_pt(297))
MAX_PAGES_TO_RASTERIZE = 40
MAX_JPG_PAGES = 5


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


@dataclass(frozen=True)
class SendOptions:
    target_kb: int = 0
    fmt: str = "original"  # "original", "pdf" or "jpg"
    width: int = 0
    height: int = 0
    merge: bool = False

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


def detect_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    """The real type of uploaded bytes (browsers' claims about file types can't be trusted)."""
    if data[:5] == b"%PDF-":
        return PDF
    try:
        image_format = Image.open(BytesIO(data)).format
    except (UnidentifiedImageError, OSError):
        return fallback
    return {"JPEG": JPEG, "PNG": "image/png", "WEBP": "image/webp"}.get(image_format, fallback)


def with_extension(name: str, extension: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return f"{stem}{extension}"


# --- Images -----------------------------------------------------------------


def open_image(data: bytes) -> Image.Image:
    """Open an image upright (phone photos carry EXIF rotation) as RGB."""
    try:
        img = Image.open(BytesIO(data))
        img = ImageOps.exif_transpose(img)
    except (UnidentifiedImageError, OSError) as exc:
        raise ConversionError("I couldn't open this image.") from exc
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, "white")
        background.paste(img, mask=img.getchannel("A"))
        return background
    return img.convert("RGB")


def encode_jpeg(img: Image.Image, quality: int) -> bytes:
    buffer = BytesIO()
    img.save(buffer, "JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


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
    img = open_image(data)
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
    img = ImageOps.fit(open_image(data), (width, height), Image.Resampling.LANCZOS)
    return encode_jpeg(img, 92)


def image_to_pdf(data: bytes) -> bytes:
    img = open_image(data)
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


def pdf_first_pages(data: bytes, max_pages: int) -> bytes:
    reader = PdfReader(BytesIO(data))
    if len(reader.pages) <= max_pages:
        return data
    writer = PdfWriter()
    for page in reader.pages[:max_pages]:
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


def _render_pages(pdf: pdfium.PdfDocument, dpi: int) -> list[Image.Image]:
    return [page.render(scale=dpi / 72).to_pil().convert("RGB") for page in pdf]


def _jpegs_to_pdf(jpegs: list[bytes], dpi: int) -> bytes:
    return img2pdf.convert(jpegs, layout_fun=img2pdf.get_fixed_dpi_layout_fun((dpi, dpi)))


def compress_pdf(data: bytes, target_bytes: int) -> tuple[bytes, bool]:
    """Shrink a PDF to at most `target_bytes`.

    Tries a lossless rewrite first. If that isn't enough, renders each page to
    a JPEG at falling resolutions, which is how scanned documents get small.
    """
    if len(data) <= target_bytes:
        return data, True
    lossless = _pdf_lossless(data)
    if len(lossless) <= target_bytes:
        return lossless, True

    pdf = _open_pdfium(data)
    try:
        if len(pdf) > MAX_PAGES_TO_RASTERIZE:
            raise ConversionError(f"This PDF has {len(pdf)} pages, which is too many to shrink.")
        smallest = lossless
        for dpi in (150, 120, 100, 85, 72, 60, 50):
            pages = _render_pages(pdf, dpi)
            low, high, best = 30, 85, None
            while low <= high:
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
        return smallest, False
    finally:
        pdf.close()


def pdf_to_jpegs(data: bytes, dpi: int = 150) -> list[bytes]:
    pdf = _open_pdfium(data)
    try:
        pages = [pdf[i] for i in range(min(len(pdf), MAX_JPG_PAGES))]
        return [encode_jpeg(page.render(scale=dpi / 72).to_pil().convert("RGB"), 90) for page in pages]
    finally:
        pdf.close()


def _first_page_image(item: FileData, dpi: int) -> Image.Image:
    if is_pdf(item.mime):
        pdf = _open_pdfium(item.data)
        try:
            if len(pdf) == 0:
                raise ConversionError(f"{item.name} has no pages.")
            return pdf[0].render(scale=dpi / 72).to_pil().convert("RGB")
        finally:
            pdf.close()
    if is_image(item.mime):
        return open_image(item.data)
    raise ConversionError(f"I can only show PDFs and photos, not {item.name}.")


def preview_jpeg(data: bytes, mime: str, max_side: int = 1200) -> bytes:
    """A JPEG of the first page, for showing on the website.

    We always serve our own JPEG instead of the uploaded file, so a crafted
    file can never run code in the browser.
    """
    img = _first_page_image(FileData(data, mime, "file"), dpi=100)
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
        side = _first_page_image(item, dpi=SHEET_DPI)
        side.thumbnail(box, Image.Resampling.LANCZOS)
        page.paste(side, ((width - side.width) // 2, top))
        top += box[1] + gap
    return img2pdf.convert(encode_jpeg(page, 88), layout_fun=img2pdf.get_fixed_dpi_layout_fun((SHEET_DPI, SHEET_DPI)))


def merge_to_pdf(files: list[FileData]) -> bytes:
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
            writer.append(reader)
        except PdfReadError as exc:
            raise ConversionError(f"I couldn't read {item.name}.") from exc
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --- Putting it together ----------------------------------------------------


def _change_format(item: FileData, fmt: str) -> list[FileData]:
    if fmt == "pdf" and is_image(item.mime):
        return [FileData(image_to_pdf(item.data), PDF, with_extension(item.name, ".pdf"))]
    if fmt == "jpg" and is_pdf(item.mime):
        pages = pdf_to_jpegs(item.data)
        if len(pages) == 1:
            return [FileData(pages[0], JPEG, with_extension(item.name, ".jpg"))]
        return [
            FileData(page, JPEG, with_extension(item.name, f"_page{number}.jpg"))
            for number, page in enumerate(pages, 1)
        ]
    if fmt == "jpg" and is_image(item.mime) and item.mime != JPEG:
        return [FileData(image_to_jpeg(item.data), JPEG, with_extension(item.name, ".jpg"))]
    return [item]


def prepare(files: list[FileData], options: SendOptions) -> Prepared:
    """Apply merge -> format -> resize -> compress, in that order."""
    result = Prepared(files=list(files))

    if options.merge and len(result.files) > 1:
        result.files = [FileData(merge_to_pdf(result.files), PDF, "Kagaz_combined.pdf")]
        result.operations.append("merge")

    if options.fmt != "original":
        changed = []
        for item in result.files:
            converted = _change_format(item, options.fmt)
            if converted != [item]:
                result.operations.append(f"to_{options.fmt}")
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
                result.notes.append(f"{item.name} is a PDF, so I didn't resize it. Ask for it as JPG to resize.")
        result.files = resized

    if options.target_kb:
        target_bytes = options.target_kb * 1024
        compressed = []
        for item in result.files:
            if len(item.data) <= target_bytes:
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
