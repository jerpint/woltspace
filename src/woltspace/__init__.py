"""Woltspace native control plane."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("woltspace")
except PackageNotFoundError:  # source tree without an installed distribution
    __version__ = "0.5.0rc4"

__all__ = ["__version__"]
