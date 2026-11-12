#!/usr/bin/env python3
"""Gateway entry inside the Windows bundle: `facet serve` without a console script."""
from __future__ import annotations

import sys

from planrouter.cli import main

if __name__ == "__main__":
    sys.exit(main())
