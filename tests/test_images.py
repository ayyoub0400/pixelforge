"""Thumbnail geometry, format handling and EXIF privacy."""

from __future__ import annotations

import io
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from shared.errors import ImageProcessingError
from shared.images import (
    extension_for_format,
    probe_image,
    render_thumbnails,
)

SIZES = (150, 400, 800)


def test_probe_reports_format_and_dimensions(fixture_bytes: Callable[[str], bytes]) -> None:
    probe = probe_image(fixture_bytes("landscape.jpg"))

    assert probe.format == "JPEG"
    assert (probe.width, probe.height) == (1600, 900)
    assert probe.extension == ".jpg"


def test_probe_rejects_garbage(fixture_bytes: Callable[[str], bytes]) -> None:
    with pytest.raises(ImageProcessingError):
        probe_image(fixture_bytes("corrupt.jpg"))


def test_probe_rejects_text(fixture_bytes: Callable[[str], bytes]) -> None:
    with pytest.raises(ImageProcessingError, match="not a recognised image"):
        probe_image(fixture_bytes("not_an_image.txt"))


def test_probe_rejects_empty_payload() -> None:
    with pytest.raises(ImageProcessingError, match="empty"):
        probe_image(b"")


def test_thumbnails_preserve_aspect_ratio(fixture_bytes: Callable[[str], bytes]) -> None:
    thumbnails, _, width, height, image_format = render_thumbnails(
        fixture_bytes("landscape.jpg"), SIZES
    )

    assert (width, height) == (1600, 900)
    assert image_format == "JPEG"
    assert [thumbnail.size for thumbnail in thumbnails] == list(SIZES)

    source_ratio = 1600 / 900
    for thumbnail in thumbnails:
        assert max(thumbnail.width, thumbnail.height) == thumbnail.size
        assert thumbnail.width / thumbnail.height == pytest.approx(source_ratio, rel=0.02)


def test_portrait_thumbnails_bound_the_long_edge(fixture_bytes: Callable[[str], bytes]) -> None:
    thumbnails, _, _, _, _ = render_thumbnails(fixture_bytes("portrait.png"), SIZES)

    for thumbnail in thumbnails:
        # The source is taller than it is wide, so height is the bounded edge.
        assert thumbnail.height == thumbnail.size
        assert thumbnail.width < thumbnail.height


def test_rendered_thumbnails_are_decodable_jpegs(fixture_bytes: Callable[[str], bytes]) -> None:
    thumbnails, _, _, _, _ = render_thumbnails(fixture_bytes("landscape.jpg"), SIZES)

    for thumbnail in thumbnails:
        with Image.open(io.BytesIO(thumbnail.data)) as rendered:
            assert rendered.format == "JPEG"
            assert rendered.size == (thumbnail.width, thumbnail.height)


def test_small_images_are_never_upscaled(fixture_bytes: Callable[[str], bytes]) -> None:
    thumbnails, _, width, height, _ = render_thumbnails(fixture_bytes("tiny.png"), SIZES)

    assert (width, height) == (120, 80)
    for thumbnail in thumbnails:
        assert (thumbnail.width, thumbnail.height) == (120, 80)


def test_transparency_is_flattened_not_dropped(fixture_bytes: Callable[[str], bytes]) -> None:
    thumbnails, _, _, _, image_format = render_thumbnails(fixture_bytes("portrait.png"), (150,))

    assert image_format == "PNG"
    with Image.open(io.BytesIO(thumbnails[0].data)) as rendered:
        assert rendered.mode == "RGB"


def test_exif_is_extracted(fixture_bytes: Callable[[str], bytes]) -> None:
    _, exif, _, _, _ = render_thumbnails(fixture_bytes("landscape.jpg"), (150,))

    assert exif["Make"] == "PixelForge"
    assert exif["Model"] == "Fixture Camera 1"
    assert exif["DateTime"] == "2024:01:15 10:30:00"


def test_gps_data_never_leaves_the_decoder(fixture_bytes: Callable[[str], bytes]) -> None:
    """The fixture carries GPS tags; none of them may reach the caller."""
    _, exif, _, _, _ = render_thumbnails(fixture_bytes("landscape.jpg"), (150,))

    assert exif, "the fixture does have EXIF, so an empty dict would be a false pass"
    assert not [key for key in exif if key.lower().startswith("gps")]
    assert "GPSInfo" not in exif


def test_exif_values_are_json_and_dynamodb_safe(fixture_bytes: Callable[[str], bytes]) -> None:
    _, exif, _, _, _ = render_thumbnails(fixture_bytes("landscape.jpg"), (150,))

    for key, value in exif.items():
        assert isinstance(key, str)
        assert isinstance(value, (str, int, float, bool, list)), f"{key}={value!r}"


def test_image_without_exif_returns_empty_metadata(fixture_bytes: Callable[[str], bytes]) -> None:
    _, exif, _, _, _ = render_thumbnails(fixture_bytes("tiny.png"), (150,))

    assert exif == {}


def test_truncated_image_raises_processing_error(fixture_bytes: Callable[[str], bytes]) -> None:
    """The header parses but the scan data is missing: the poison case."""
    probe_image(fixture_bytes("truncated.jpg"))  # header alone looks fine

    with pytest.raises(ImageProcessingError):
        render_thumbnails(fixture_bytes("truncated.jpg"), SIZES)


def test_render_accepts_a_path(tmp_path: Path, fixture_bytes: Callable[[str], bytes]) -> None:
    source = tmp_path / "original"
    source.write_bytes(fixture_bytes("landscape.jpg"))

    thumbnails, _, _, _, _ = render_thumbnails(source, (150,))

    assert thumbnails[0].bytes_len == len(thumbnails[0].data) > 0


def test_duplicate_sizes_render_once(fixture_bytes: Callable[[str], bytes]) -> None:
    thumbnails, _, _, _, _ = render_thumbnails(fixture_bytes("landscape.jpg"), (150, 150, 400))

    assert [thumbnail.size for thumbnail in thumbnails] == [150, 400]


@pytest.mark.parametrize(
    ("image_format", "extension"),
    [("JPEG", ".jpg"), ("PNG", ".png"), ("WEBP", ".webp"), ("GIF", ".gif"), (None, ".bin")],
)
def test_extension_mapping(image_format: str | None, extension: str) -> None:
    assert extension_for_format(image_format) == extension
