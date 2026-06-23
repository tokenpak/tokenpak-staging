"""TokenPak Metrics Dashboard - web UI for observability."""

import os  # noqa: F401 — reserved for dashboard file path expansion
import warnings as _warnings
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent

# Canonical public name for the set of supported dashboard render modes.
# The legacy export ``CCI09_DASHBOARD_MODES`` embedded an internal review id
# ("CCI09") in a public-API symbol name; it is retained as a deprecation alias
# (see ``__getattr__`` below) and scheduled for removal in the next minor
# release per Std 21 §11.2 (deprecation-alias declaration + api-snapshot
# ratchet, NOT a bare rename of a public-API symbol).
DASHBOARD_MODES = ("cli", "tui", "tmux", "sdk", "ide", "cron")


def get_dashboard_files():
    """Return paths to dashboard files."""
    return {
        "index.html": DASHBOARD_DIR / "index.html",
        "metrics.js": DASHBOARD_DIR / "metrics.js",
        "charts.js": DASHBOARD_DIR / "charts.js",
        "styles.css": DASHBOARD_DIR / "styles.css",
    }


async def serve_dashboard_file(path: str) -> tuple[str, str] | None:
    """Serve a dashboard file. Returns (content, mime_type) or None.

    Query strings are ignored so `/dashboard?mode=cli` resolves to the same
    local dashboard shell as `/dashboard`; client-side code reads the `mode`
    parameter and renders the requested panel.
    """
    files = get_dashboard_files()

    # Normalize request-target fragments from the proxy server before lookup.
    path = path.split("?", 1)[0].split("#", 1)[0]

    # Default to index.html
    if path in ("", "/"):
        path = "index.html"

    # Remove leading slash
    if path.startswith("/"):
        path = path[1:]

    if path not in files:
        return None

    filepath = files[path]
    if not filepath.exists():
        return None

    content = filepath.read_text()

    mime_types = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
    }

    ext = filepath.suffix
    mime_type = mime_types.get(ext, "text/plain")

    return content, mime_type


# Python API exports (from agent/dashboard/)
try:
    from .export_api import ExportAPI
    from .export_csv import CSVExporter, ExportDataType, ExportFormat
    from .session_filter import SessionFilter
    __all__ = ['get_dashboard_files', 'serve_dashboard_file', 'DASHBOARD_MODES', 'CCI09_DASHBOARD_MODES', 'ExportAPI', 'CSVExporter', 'ExportDataType', 'ExportFormat', 'SessionFilter', 'account_dashboard', 'app', 'export_api', 'export_csv', 'session_filter']
except ImportError:
    __all__ = ["get_dashboard_files", "serve_dashboard_file", "DASHBOARD_MODES", "CCI09_DASHBOARD_MODES", 'account_dashboard', 'app', 'export_api', 'export_csv', 'session_filter']


# Public-API symbols renamed for a public-safe surface but kept importable as
# deprecation aliases. Accessing one emits a ``DeprecationWarning`` and resolves
# to its canonical replacement. The alias is retained through the next minor
# release and then removed (Std 21 §11.2). Listed in ``__all__`` above so the
# api-snapshot continues to record it as a (deprecated) public symbol — the
# snapshot generator reads ``__all__`` without dereferencing names, so emitting
# the snapshot does not trip this warning.
_DEPRECATED_ALIASES = {
    "CCI09_DASHBOARD_MODES": "DASHBOARD_MODES",
}


def __getattr__(name):
    """PEP 562 module-level hook resolving deprecation aliases with a warning."""
    replacement = _DEPRECATED_ALIASES.get(name)
    if replacement is not None:
        _warnings.warn(
            f"tokenpak.dashboard.{name} is deprecated and will be removed in the "
            f"next minor release; use {replacement} instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[replacement]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
