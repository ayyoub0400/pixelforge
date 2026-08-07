"""Regenerate the test fixtures in this directory.

Run with ``make fixtures`` (or ``python fixtures/generate_fixtures.py``). The
generated files are committed so that the test-suite runs offline and produces
identical results on every machine; this script exists so they can be recreated
or extended deliberately rather than being opaque binaries.

Fixtures produced:

``landscape.jpg``
    1600x900 JPEG carrying EXIF, including GPS tags. The GPS tags are the
    point: they prove the pipeline strips location data instead of persisting
    or logging it.
``portrait.png``
    800x1200 RGBA PNG with transparency, to exercise the alpha-flattening path.
``tiny.png``
    120x80 PNG, smaller than the smallest thumbnail, so the "never upscale"
    behaviour has something to assert against.
``corrupt.jpg``
    JPEG magic bytes followed by garbage. Rejected at upload.
``truncated.jpg``
    A valid JPEG cut off mid-scan. The header parses, the decode does not:
    this is the poison message the worker must survive.
``not_an_image.txt``
    Plain text sent with an image content type.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image

FIXTURES_DIR = Path(__file__).resolve().parent

#: Fixed seed so regenerating produces byte-identical files.
SEED = 20240115


def _noise(width: int, height: int, *, seed: int, mode: str = "RGB") -> Image.Image:
    """Build a blocky noise image, upscaled from 1/8 scale for speed."""
    rng = random.Random(seed)
    channels = len(mode)
    small_width, small_height = max(1, width // 8), max(1, height // 8)
    raw = rng.randbytes(small_width * small_height * channels)
    small = Image.frombytes(mode, (small_width, small_height), raw)
    return small.resize((width, height), Image.Resampling.NEAREST)


def _exif_with_gps() -> Image.Exif:
    """Build an EXIF block containing camera details and GPS coordinates."""
    exif = Image.Exif()
    exif[0x010F] = "PixelForge"  # Make
    exif[0x0110] = "Fixture Camera 1"  # Model
    exif[0x0131] = "pixelforge-fixtures/1.0"  # Software
    exif[0x0112] = 1  # Orientation: normal
    exif[0x011A] = 72.0  # XResolution
    exif[0x011B] = 72.0  # YResolution
    exif[0x0132] = "2024:01:15 10:30:00"  # DateTime

    # Somewhere in London. If any of this survives into a job record or a log
    # line, the privacy tests fail - which is exactly what they are for.
    exif[0x8825] = {
        1: "N",
        2: (51.0, 30.0, 26.0),
        3: "W",
        4: (0.0, 7.0, 39.0),
        5: 0,
        6: 11.0,
    }
    return exif


def write_landscape() -> Path:
    """Write ``landscape.jpg``: 1600x900 with EXIF including GPS."""
    path = FIXTURES_DIR / "landscape.jpg"
    image = _noise(1600, 900, seed=SEED)
    image.save(path, format="JPEG", quality=90, exif=_exif_with_gps())
    return path


def write_portrait() -> Path:
    """Write ``portrait.png``: 800x1200 RGBA with a transparent corner."""
    path = FIXTURES_DIR / "portrait.png"
    image = _noise(800, 1200, seed=SEED + 1).convert("RGBA")
    # Punch a fully transparent block so alpha compositing has to do something.
    overlay = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    image.paste(overlay, (0, 0), overlay)
    image.putalpha(
        Image.linear_gradient("L").resize((800, 1200), Image.Resampling.BILINEAR)
    )
    image.save(path, format="PNG", optimize=True)
    return path


def write_tiny() -> Path:
    """Write ``tiny.png``: smaller than the smallest configured thumbnail."""
    path = FIXTURES_DIR / "tiny.png"
    _noise(120, 80, seed=SEED + 2).save(path, format="PNG", optimize=True)
    return path


def write_corrupt() -> Path:
    """Write ``corrupt.jpg``: JPEG magic bytes followed by garbage."""
    path = FIXTURES_DIR / "corrupt.jpg"
    rng = random.Random(SEED + 3)
    path.write_bytes(b"\xff\xd8\xff\xe0" + rng.randbytes(4096))
    return path


def write_truncated() -> Path:
    """Write ``truncated.jpg``: a real JPEG cut off part-way through."""
    path = FIXTURES_DIR / "truncated.jpg"
    source = (FIXTURES_DIR / "landscape.jpg").read_bytes()
    path.write_bytes(source[: len(source) // 3])
    return path


def write_not_an_image() -> Path:
    """Write ``not_an_image.txt``: plain text pretending to be an image."""
    path = FIXTURES_DIR / "not_an_image.txt"
    path.write_text(
        "This is not an image. It is uploaded with an image content type so the\n"
        "API has to decide on the bytes rather than the declared type.\n",
        encoding="utf-8",
    )
    return path


def main() -> int:
    """Regenerate every fixture."""
    written = [
        write_landscape(),
        write_portrait(),
        write_tiny(),
        write_truncated(),
        write_corrupt(),
        write_not_an_image(),
    ]
    for path in written:
        print(f"{path.name:24} {path.stat().st_size:>9,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
