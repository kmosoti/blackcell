from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files

_MAX_HTML_BYTES = 64 * 1024
_MAX_CSS_BYTES = 128 * 1024
_MAX_JAVASCRIPT_BYTES = 256 * 1024


class WebAssetFailureCode(StrEnum):
    INVALID_ASSET = "execution-web-invalid-asset"


class WebAssetError(RuntimeError):
    def __init__(self) -> None:
        self.code = WebAssetFailureCode.INVALID_ASSET
        super().__init__(self.code.value)


@dataclass(frozen=True, slots=True)
class WebAssets:
    html: bytes = field(repr=False)
    css: bytes = field(repr=False)
    javascript: bytes = field(repr=False)


@lru_cache(maxsize=1)
def load_web_assets() -> WebAssets:
    try:
        root = files("blackcell.interfaces.http").joinpath("assets", "ui")
        return WebAssets(
            html=_asset(root.joinpath("index.html").read_bytes(), _MAX_HTML_BYTES),
            css=_asset(root.joinpath("app.css").read_bytes(), _MAX_CSS_BYTES),
            javascript=_asset(root.joinpath("app.js").read_bytes(), _MAX_JAVASCRIPT_BYTES),
        )
    except WebAssetError:
        raise
    except (OSError, TypeError) as error:
        raise WebAssetError from error


def _asset(content: bytes, maximum: int) -> bytes:
    if not isinstance(content, bytes) or not 1 <= len(content) <= maximum or b"\x00" in content:
        raise WebAssetError
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WebAssetError from error
    return content


__all__ = [
    "WebAssetError",
    "WebAssetFailureCode",
    "WebAssets",
    "load_web_assets",
]
