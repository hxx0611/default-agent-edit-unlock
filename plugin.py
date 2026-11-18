# -*- coding: utf-8 -*-
"""QwenPaw plugin entry: unlock the default agent's console edit buttons.

Applies the console bundle patch at startup (idempotent, self-healing
after pip upgrades / container rebuilds) and restores the stock bundle
when the plugin is uninstalled. Logic lives in ``console_edit_patch.py`` next to this file.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_sibling(name):
    """Import a module sitting next to this file without touching sys.path.

    Inserting the plugin dir into sys.path changes the
    dependency-admission path set and can raise
    DEPENDENCY_PLAN_UNVERIFIED for channel plugins.
    """
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_HERE, name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


console_edit_patch = _load_sibling("console_edit_patch")

logger = logging.getLogger(__name__)


def _startup() -> None:
    try:
        report = console_edit_patch.install()
        logger.info("default-agent-edit-unlock: startup report=%s", report)
    except Exception:
        # Never take the host down over a cosmetic console patch.
        logger.exception("default-agent-edit-unlock: startup patch failed")


def _uninstall(plugin_id: str = "", delete_files: bool = False, **kwargs) -> None:
    try:
        restored = console_edit_patch.restore()
        logger.info(
            "default-agent-edit-unlock: uninstall restore=%s "
            "(plugin_id=%s delete_files=%s)",
            restored,
            plugin_id,
            delete_files,
        )
    except Exception:
        logger.exception("default-agent-edit-unlock: uninstall restore failed")


class DefaultAgentEditUnlockPlugin:
    """Plugin entry point (manifest: plugin.json)."""

    def register(self, api) -> None:
        # priority 90: late startup; static files are read per-request,
        # so no remount is needed and channel ordering is irrelevant.
        api.register_startup_hook(
            "default_agent_edit_unlock_install",
            _startup,
            priority=90,
        )
        api.register_uninstall_hook(
            "default_agent_edit_unlock_restore",
            _uninstall,
            priority=100,
        )


plugin = DefaultAgentEditUnlockPlugin()