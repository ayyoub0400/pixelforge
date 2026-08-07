"""Shared building blocks for the pixelforge API and worker services.

Nothing in this package may import from :mod:`api` or :mod:`worker`; the
dependency arrow points one way so that both services can be packaged
independently.
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
