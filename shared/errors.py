"""Exception hierarchy shared by both services.

The distinction that matters operationally is between a *permanent* failure
(the input is bad and will never succeed, so the job must be marked FAILED and
the SQS message removed) and a *transient* failure (a dependency is briefly
unavailable, so the message must be left on the queue for redelivery).
"""

from __future__ import annotations


class PixelforgeError(Exception):
    """Base class for every error raised by pixelforge code."""


class ConfigError(PixelforgeError):
    """Raised when required configuration is missing or invalid.

    Always fatal: the process must exit rather than start with a guessed
    value.
    """


class TransientDependencyError(PixelforgeError):
    """An AWS dependency was unreachable or throttled.

    Retrying later is expected to succeed, so the caller must *not* treat the
    job as failed and must *not* delete the SQS message.
    """

    def __init__(self, message: str, *, operation: str | None = None) -> None:
        super().__init__(message)
        self.operation = operation


class ImageProcessingError(PixelforgeError):
    """The uploaded bytes could not be decoded or processed.

    This is a poison message: no amount of retrying will help, so the job is
    marked FAILED and the message is deleted.
    """


class MessageFormatError(PixelforgeError):
    """An SQS message body did not match the documented job schema."""
