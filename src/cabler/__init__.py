"""cabler: optimal cable routing from wind farms to substations."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("cabler")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
