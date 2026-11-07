"""Hybrid Critic Loop — Hermes plugin entry point.

Hermes loads this package's ``register(ctx)``.  The plugin directory is
inserted into ``sys.path`` first so the ``hybrid_critic_loop`` package is
importable even when the plugin dir is not already on the path.
"""

import sys
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from hybrid_critic_loop.plugin import register  # noqa: E402

__all__ = ["register"]
