"""facet-models: a thin fact client for the Facet model registry.

Facts only — load the table, resolve names, filter and rank. Decisions stay
with the caller. Zero runtime dependencies (stdlib only).
"""

from ._find import check, find, rank
from ._identity import build_lookup, identity, resolve
from ._table import BUNDLED_TABLE, DEFAULT_TABLE_URL, load

try:  # written by tools/build_package.py; absent in a raw source tree
    from ._version import __version__
except ImportError:  # pragma: no cover
    __version__ = "0.0.0.dev0"

__all__ = [
    "BUNDLED_TABLE",
    "DEFAULT_TABLE_URL",
    "__version__",
    "build_lookup",
    "check",
    "find",
    "identity",
    "load",
    "rank",
    "resolve",
]
