"""Facet gateway: local multi-provider LLM channel scheduling.

Zero runtime dependencies. See router.json for configuration.
"""
from __future__ import annotations

from .core import Config, StateStore, load_config, parse_cooldown
from .server import App, make_server

__version__ = "1.0.0"
__all__ = ["App", "Config", "StateStore", "load_config", "make_server", "parse_cooldown"]
