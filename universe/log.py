"""Small logging helper with colour that degrades gracefully.

ANSI codes are emitted only when stdout is a terminal or when GitHub Actions is
rendering the log, so piping output to a file keeps it clean.
"""

from __future__ import annotations

import os
import sys
from typing import Final

_FORCE = os.environ.get("FORCE_COLOR", "").lower() in {"1", "true", "yes"}
_DISABLED = bool(os.environ.get("NO_COLOR")) or os.environ.get(
    "TERM", ""
) == "dumb"
_ON_CI = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

_USE_COLOR: Final[bool] = _FORCE or (
    not _DISABLED and (_ON_CI or sys.stdout.isatty())
)

_CODES = {
    "green": "\033[92m",
    "yellow": "\033[93m",
    "red": "\033[91m",
    "blue": "\033[94m",
    "grey": "\033[90m",
}
_RESET = "\033[0m"


def _emit(color: str, message: str, *, stream=None) -> None:
    text = f"{_CODES[color]}{message}{_RESET}" if _USE_COLOR else message
    print(text, file=stream or sys.stdout, flush=True)


def info(message: str) -> None:
    _emit("blue", message)


def success(message: str) -> None:
    _emit("green", message)


def warn(message: str) -> None:
    _emit("yellow", message)


def error(message: str) -> None:
    _emit("red", message, stream=sys.stderr)


def detail(message: str) -> None:
    _emit("grey", message)
