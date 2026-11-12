#!/usr/bin/env python3
"""Combined entrypoint: runs either PlanRouter gateway or Computer-Use Agent."""

import os
import sys
import subprocess
import signal

def run_gateway():
    """Run PlanRouter gateway (original entrypoint)."""
    os.execv(sys.executable, [sys.executable, "/opt/planrouter/entrypoint.py"] + sys.argv[1:])

def run_agent():
    """Run Computer-Use Agent."""
    # Switch to appuser for agent (non-root)
    if os.getuid() == 0:
        # Drop privileges
        os.setgid(1000)
        os.setuid(1000)
    os.execv(sys.executable, [sys.executable, "/app/computer_use_agent.py"] + sys.argv[1:])

def main():
    if len(sys.argv) < 2:
        print("Usage: combined_entrypoint.py [gateway|agent] [args...]", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    # Remove the mode from argv so subcommands see their own args
    sys.argv = [sys.argv[0]] + sys.argv[2:]

    if mode == "gateway":
        run_gateway()
    elif mode == "agent":
        run_agent()
    else:
        print(f"Unknown mode: {mode}. Use 'gateway' or 'agent'.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()