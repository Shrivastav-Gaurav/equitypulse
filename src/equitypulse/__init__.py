"""EquityPulse: personal Indian-equity trade planner."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is pyproject.toml; read it from installed metadata.
    __version__ = version("equitypulse")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
