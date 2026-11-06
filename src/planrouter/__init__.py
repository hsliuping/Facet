"""PlanRouter: local multi-provider LLM channel scheduling.

Zero runtime dependencies. See router.example.json for configuration.
"""
from __future__ import annotations

from .core import Config, StateStore, load_config, parse_cooldown
from .server import App, make_server

__version__ = "0.1.0"
__all__ = ["App", "Config", "StateStore", "load_config", "make_server", "parse_cooldown"]
