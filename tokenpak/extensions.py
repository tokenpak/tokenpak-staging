"""Extension discovery and registration for TokenPak plugins."""
import logging
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)
_lock = threading.Lock()
_EXTENSIONS: dict[str, Any] = {}
_discovered = False


def register(name: str, extension: Any) -> None:
    """Register an extension by name.
    
    Args:
        name: Unique extension identifier
        extension: Extension object (module or callable)
    """
    with _lock:
        if name in _EXTENSIONS:
            logger.warning(f"Extension {name} already registered, overwriting")
        _EXTENSIONS[name] = extension
        logger.info(f"Extension registered: {name}")


def get(name: str, default=None) -> Any:
    """Get a registered extension by name.
    
    Args:
        name: Extension identifier
        default: Value to return if extension not found
        
    Returns:
        Extension object or default
    """
    return _EXTENSIONS.get(name, default)


def list_extensions() -> list[str]:
    """List all registered extension names.
    
    Returns:
        List of registered extension identifiers
    """
    return list(_EXTENSIONS.keys())


def is_loaded(name: str) -> bool:
    """Check if an extension is loaded.
    
    Args:
        name: Extension identifier
        
    Returns:
        True if extension is registered
    """
    return name in _EXTENSIONS


def is_pro_available() -> bool:
    """Check if the tokenpak-pro extension is available.
    
    Returns:
        True if pro extension is loaded
    """
    return "pro" in _EXTENSIONS


def discover() -> dict[str, Any]:
    """Discover and load extensions via entry_points.
    
    Auto-discovers extensions registered under the "tokenpak.extensions"
    entry_points group. Extensions are loaded lazily and failures are
    logged as warnings without crashing the system.
    
    Returns:
        Dictionary with discovery results: {'count': int, 'loaded': list, 'failed': list}
    """
    global _discovered
    if _discovered:
        return {"count": len(_EXTENSIONS), "loaded": list_extensions(), "failed": []}
    _discovered = True
    
    loaded = []
    failed = []
    
    try:
        from importlib.metadata import entry_points
        eps = entry_points(group="tokenpak.extensions")
        for ep in eps:
            try:
                loader = ep.load()
                if callable(loader):
                    loader()
                logger.info(f"Loaded extension: {ep.name}")
                loaded.append(ep.name)
            except Exception as e:
                logger.warning(f"Failed to load extension {ep.name}: {e}")
                failed.append((ep.name, str(e)))
    except Exception as e:
        logger.debug(f"Extension discovery unavailable: {e}")
    
    return {"count": len(_EXTENSIONS), "loaded": loaded, "failed": failed}
