"""Scratch space for the worker.

Instances hold no local state: every temporary file lives under the system
temp directory (``TMPDIR``, which both container images pin to ``/tmp``) and is
removed in a ``finally`` block even when processing raises. Nothing is ever
written to the working directory.
"""

from __future__ import annotations

import contextlib
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import structlog

__all__ = ["TEMP_PREFIX", "temp_workspace"]

TEMP_PREFIX: Final[str] = "pixelforge-"

_LOG = structlog.get_logger(__name__)


@contextlib.contextmanager
def temp_workspace(prefix: str = TEMP_PREFIX) -> Iterator[Path]:
    """Yield a private temporary directory and delete it on exit.

    The directory is removed whether the body completes, raises, or the process
    is unwinding, which is the property that keeps a restarted pod from
    inheriting half-written files.

    Args:
        prefix: Directory name prefix, useful when grepping ``/tmp``.

    Yields:
        Path to a directory that exists for the duration of the block.
    """
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        try:
            shutil.rmtree(path, ignore_errors=False)
        except OSError as exc:  # pragma: no cover - only on a broken filesystem
            _LOG.warning("temp_workspace_cleanup_failed", path=str(path), error=str(exc))
            shutil.rmtree(path, ignore_errors=True)
