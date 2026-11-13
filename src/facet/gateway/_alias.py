"""Deprecated ``planrouter`` command: announce the rename, then serve anyway.

Kept as a transitional alias so existing scripts keep working; the canonical
entry point is now ``facet serve`` (same wheel, same argv semantics).
"""

from __future__ import annotations

import sys

from .cli import main as _serve


def main(argv=None) -> int:
    print(
        "planrouter command is deprecated; use 'facet serve' instead "
        "(this alias still works).",
        file=sys.stderr,
    )
    return _serve(argv)
