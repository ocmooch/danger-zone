"""ff_pipeline — personal fantasy football data aggregation pipeline.

Phase 1: data foundation. See docs/ for the full design package.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ff-pipeline")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
