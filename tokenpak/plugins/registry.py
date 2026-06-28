"""Plugin registry — discovery, registration, and ordered retrieval."""

import importlib
import json
import logging
import os
from pathlib import Path
from typing import List, Type

from tokenpak.plugins.base import CompressorPlugin

logger = logging.getLogger(__name__)
_CWD_PLUGIN_CONFIG_ENV = "TOKENPAK_ALLOW_CWD_PLUGIN_CONFIG"


def _env_truthy(name: str) -> bool:
    """Return whether an opt-in env var is explicitly enabled."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class PluginRegistry:
    """Registry for CompressorPlugin subclasses."""

    def __init__(self) -> None:
        self._plugins: List[CompressorPlugin] = []
        self._names: set = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, plugin_cls: Type[CompressorPlugin]) -> None:
        """Register a plugin class (instantiates it immediately).

        Raises:
            ValueError: if a plugin with the same ``name`` is already registered.
        """
        instance = plugin_cls()
        pname = instance.name or plugin_cls.__name__
        if pname in self._names:
            raise ValueError(f"Plugin name collision: '{pname}' is already registered")
        self._names.add(pname)
        self._plugins.append(instance)
        logger.debug("Plugin registered: %s (priority=%d)", pname, instance.priority())

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> None:
        """Load plugins from env var and config file."""
        self._discover_from_env()
        self._discover_from_config()

    def _load_plugin_path(self, dotted_path: str) -> None:
        """Import *dotted_path* (``module.ClassName``) and register it."""
        dotted_path = dotted_path.strip()
        if not dotted_path:
            return
        try:
            module_path, cls_name = dotted_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            plugin_cls = getattr(module, cls_name)
            if not (isinstance(plugin_cls, type) and issubclass(plugin_cls, CompressorPlugin)):
                logger.warning(
                    "Plugin path '%s' does not point to a CompressorPlugin subclass — skipping",
                    dotted_path,
                )
                return
            self.register(plugin_cls)
        except (ImportError, ModuleNotFoundError, AttributeError, ValueError) as exc:
            logger.warning("Could not load plugin '%s': %s — skipping", dotted_path, exc)

    def _discover_from_env(self) -> None:
        """Load plugins listed in ``TOKENPAK_PLUGINS`` (comma-separated)."""
        raw = os.environ.get("TOKENPAK_PLUGINS", "").strip()
        if not raw:
            return
        for path in raw.split(","):
            self._load_plugin_path(path)

    def _discover_from_config(self) -> None:
        """Load plugins from config.yaml ``plugins.enabled``.

        Resolution order:
          1. config.yaml ``plugins.enabled`` list (canonical)
          2. Legacy CWD ``tokenpak.config.json`` only with explicit opt-in
        """
        from tokenpak.core.config_loader import get as config_get

        # 1. Canonical: config.yaml
        try:
            plugin_list = config_get("plugins.enabled", [], None, list)
            if plugin_list:
                for path in plugin_list:
                    self._load_plugin_path(path)
                return
        except Exception as exc:
            logger.debug("Could not read plugins.enabled from config.yaml: %s", exc)

        # 2. Legacy fallback: tokenpak.config.json in CWD (explicit migration support)
        legacy_path = Path("tokenpak.config.json")
        if legacy_path.exists() and not _env_truthy(_CWD_PLUGIN_CONFIG_ENV):
            logger.warning(
                "Ignoring tokenpak.config.json in the current working directory; "
                "move plugins to config.yaml under 'plugins.enabled' or set %s=1 "
                "for a one-off migration run",
                _CWD_PLUGIN_CONFIG_ENV,
            )
            return

        if legacy_path.exists():
            try:
                data = json.loads(legacy_path.read_text())
                plugin_list = data.get("plugins", [])
                if plugin_list:
                    logger.warning(
                        "tokenpak.config.json in the current working directory is "
                        "deprecated and only loaded because %s is enabled; move plugins "
                        "to config.yaml under 'plugins.enabled' key",
                        _CWD_PLUGIN_CONFIG_ENV,
                    )
                    for path in plugin_list:
                        self._load_plugin_path(path)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read tokenpak.config.json: %s — skipping", exc)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_plugins(self) -> List[CompressorPlugin]:
        """Return plugins sorted by priority, highest first."""
        return sorted(self._plugins, key=lambda p: p.priority(), reverse=True)
