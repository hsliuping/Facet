"""Container entrypoint: seed /data/router.json on first run, then exec the gateway."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

TEMPLATE = Path("/opt/planrouter/router.template.json")
DATA = Path("/data")
CONFIG = DATA / "router.json"


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not CONFIG.exists():
        shutil.copyfile(TEMPLATE, CONFIG)
        print(f"[entrypoint] seeded {CONFIG} from template", flush=True)
    # All runtime files (state, usage, subscriptions, notifications, audit log)
    # are derived from the config path, so a single /data volume persists them.
    os.execvp("planrouter", ["planrouter", "--config", str(CONFIG), *sys.argv[1:]])


if __name__ == "__main__":
    main()
