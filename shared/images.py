"""Image decoding, validation, thumbnailing and EXIF extraction.

Shared because the API needs the *validation* half (reject a payload that is
not a decodable image before it ever reaches the queue) and the worker needs
the *rendering* half.

Two safety rails apply to every decode:

* a decompression-bomb ceiling, so a 200-byte file cannot allocate gigabytes;
* GPS EXIF tags are dropped during extraction, so location data is never
  persisted, returned or logged.

Every decode failure is normalised to :class:`~shared.errors.ImageProcessingError`
so callers can treat "bad input" uniformly and never see a raw Pillow error.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from PIL import ExifTags, Image, ImageFile, ImageOps, UnidentifiedImageError

from shared.errors import ImageProcessingError

__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_PIXELS",
    "ImageProbe",
    "RenderedThumbnail",
    "extension_for_format",
    "extract_exif",
    "probe_image",
    "render_thumbnails",
]

#: Content types the API accepts on upload. Anything else is a ``415``.
ALLOWED_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/bmp",
        "image/tiff",
    }
)

#: Pillow format name -> canonical file extension. The extension of the stored
#: original comes from the *detected* format, never from the client's filename,
#: so a crafted name cannot influence the S3 key.
_FORMAT_EXTENSIONS: Final[dict[str, str]] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "MPO": ".jpg",
}

#: Refuse anything that would decode to more than ~80 megapixels.
MAX_PIXELS: Final[int] = 80_000_000

#: Quality/subsampling for rendered thumbnails.
_JPEG_QUALITY: Final[int] = 85

#: EXIF tag ids that carry location data and are dropped on sight.
_GPS_IFD_TAG: Final[int] = 0x8825
_EXIF_IFD_TAG: Final[int] = 0x8769

#: Longest string kept from a single EXIF value.
_MAX_EXIF_VALUE_CHARS: Final[int] = 256

#: EXIF tags that are large binary blobs with no diagnostic value.
_EXIF_BLOCKLIST: Final[frozenset[str]] = frozenset(
    {"MakerNote", "UserComment", "PrintImageMatching", "ImageResources", "XMLPacket"}
)

# A truncated file must raise rather than silently render a grey band: that is
# the difference between a job that fails loudly and a customer receiving a
# corrupt thumbnail.
ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


@dataclass(frozen=True, slots=True)
class ImageProbe:
    """What a cheap header-only inspection learned about some bytes."""

    format: str
    width: int
    height: int
    extension: str


@dataclass(frozen=True, slots=True)
class RenderedThumbnail:
    """One rendered thumbnail, ready to be uploaded."""

    size: int
    data: bytes
    width: int
    height: int

    @property
    def bytes_len(self) -> int:
        """Length of the encoded JPEG."""
        return len(self.data)


def extension_for_format(image_format: str | None) -> str:
    """Map a Pillow format name to a canonical extension.

    Args:
        image_format: Pillow's detected format, e.g. ``"JPEG"``.

    Returns:
        A dotted extension; ``".bin"`` for an unrecognised format.
    """
    if not image_format:
        return ".bin"
    return _FORMAT_EXTENSIONS.get(image_format.upper(), ".bin")


def probe_image(data: bytes) -> ImageProbe:
    """Validate that ``data`` is a decodable image and report its shape.

    This is the API's upload gate. It parses the header and fully verifies the
    structure without keeping the decoded raster, so it is cheap enough to run
    inline on the request path.

    Args:
        data: Raw uploaded bytes.

    Returns:
        An :class:`ImageProbe` describing the image.

    Raises:
        ImageProcessingError: The bytes are not a supported, intact image.
    """
    if not data:
        raise ImageProcessingError("uploaded file is empty")

    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format or ""
            width, height = image.size
            # verify() walks the file structure and catches truncation that
            # open() alone would not notice.
            image.verify()
    except UnidentifiedImageError as exc:
        raise ImageProcessingError("file is not a recognised image format") from exc
    except Image.DecompressionBombError as exc:
        raise ImageProcessingError("image exceeds the maximum allowed pixel count") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImageProcessingError(f"image could not be decoded: {exc}") from exc

    extension = extension_for_format(image_format)
    if extension == ".bin":
        raise ImageProcessingError(f"unsupported image format: {image_format or 'unknown'}")
    if width <= 0 or height <= 0:
        raise ImageProcessingError("image has zero width or height")

    return ImageProbe(format=image_format.upper(), width=width, height=height, extension=extension)


def _normalise_exif_value(value: Any) -> Any:
    """Coerce a Pillow EXIF value into a JSON- and DynamoDB-safe primitive.

    Returns ``None`` for values that should be dropped entirely.
    """
    if isinstance(value, bytes):
        return None
    if isinstance(value, str):
        cleaned = value.replace("\x00", "").strip()
        return cleaned[:_MAX_EXIF_VALUE_CHARS] or None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(float(value), 6)
    # IFDRational and friends expose numerator/denominator.
    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is not None and denominator is not None:
        if denominator == 0:
            return None
        return round(float(numerator) / float(denominator), 6)
    if isinstance(value, (tuple, list)):
        items = [_normalise_exif_value(item) for item in value[:16]]
        kept = [item for item in items if item is not None]
        return kept or None
    return None


def extract_exif(image: Image.Image) -> dict[str, Any]:
    """Extract EXIF metadata, excluding all location data.

    GPS tags are dropped here rather than at the logging boundary so that
    location data never enters the system at all: it is not persisted to
    DynamoDB, not returned by the API, and therefore cannot leak into a log.

    Args:
        image: An open Pillow image.

    Returns:
        A flat mapping of tag name to a JSON-safe scalar. Empty when the image
        carries no EXIF.
    """
    result: dict[str, Any] = {}

    try:
        raw = image.getexif()
    except Exception:  # pragma: no cover - malformed EXIF block
        return result
    if not raw:
        return result

    def absorb(source: Any) -> None:
        for tag_id, value in source.items():
            if tag_id in (_GPS_IFD_TAG, _EXIF_IFD_TAG):
                continue
            name = ExifTags.TAGS.get(tag_id) or ExifTags.GPSTAGS.get(tag_id) or f"Tag{tag_id}"
            if name in _EXIF_BLOCKLIST or name.lower().startswith("gps"):
                continue
            normalised = _normalise_exif_value(value)
            if normalised is not None:
                result[name] = normalised

    absorb(raw)
    try:
        exif_ifd = raw.get_ifd(_EXIF_IFD_TAG)
    except Exception:  # pragma: no cover - malformed sub-IFD
        exif_ifd = None
    if exif_ifd:
        absorb(exif_ifd)

    return result


def render_thumbnails(
    source: Path | bytes, sizes: Sequence[int]
) -> tuple[list[RenderedThumbnail], dict[str, Any], int, int, str]:
    """Decode an image and render one thumbnail per requested size.

    Aspect ratio is preserved: each ``size`` is the longest edge of a bounding
    box the image is fitted into, so a 1600x900 source at size 400 becomes
    400x225. Images smaller than the box are not upscaled, which is Pillow's
    ``thumbnail`` semantics and avoids shipping a blurry enlargement.

    EXIF orientation is applied before rendering so a phone photo is not
    delivered sideways.

    Args:
        source: Path to the downloaded original, or the raw bytes.
        sizes: Bounding-box edges in pixels.

    Returns:
        ``(thumbnails, exif, source_width, source_height, source_format)``.

    Raises:
        ImageProcessingError: The image could not be decoded or rendered.
    """
    payload = source.read_bytes() if isinstance(source, Path) else source

    try:
        with Image.open(io.BytesIO(payload)) as opened:
            source_format = (opened.format or "UNKNOWN").upper()
            opened.load()
            exif = extract_exif(opened)
            oriented = ImageOps.exif_transpose(opened) or opened
            source_width, source_height = oriented.size
            renderable = _to_rgb(oriented)
            thumbnails = [_render_one(renderable, size) for size in sorted(set(sizes))]
    except ImageProcessingError:
        raise
    except UnidentifiedImageError as exc:
        raise ImageProcessingError("file is not a recognised image format") from exc
    except Image.DecompressionBombError as exc:
        raise ImageProcessingError("image exceeds the maximum allowed pixel count") from exc
    except (OSError, ValueError, SyntaxError, MemoryError) as exc:
        raise ImageProcessingError(f"image could not be processed: {exc}") from exc

    return thumbnails, exif, source_width, source_height, source_format


def _to_rgb(image: Image.Image) -> Image.Image:
    """Flatten to RGB so every output can be encoded as JPEG.

    Transparency is composited onto white rather than dropped, which keeps PNG
    logos legible instead of rendering them as black-on-black.
    """
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        converted = image.convert("RGBA")
        background = Image.new("RGBA", converted.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, converted).convert("RGB")
    return image.convert("RGB")


def _render_one(image: Image.Image, size: int) -> RenderedThumbnail:
    """Fit a copy of ``image`` into a ``size`` x ``size`` box and encode it."""
    copy = image.copy()
    copy.thumbnail((size, size), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    copy.save(buffer, format="JPEG", quality=_JPEG_QUALITY, optimize=True, progressive=True)
    width, height = copy.size
    copy.close()
    return RenderedThumbnail(size=size, data=buffer.getvalue(), width=width, height=height)
