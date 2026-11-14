"""Application package marker."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("requirements-api")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
