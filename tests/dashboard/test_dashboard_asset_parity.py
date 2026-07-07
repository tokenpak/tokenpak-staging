# SPDX-License-Identifier: Apache-2.0
"""Asset-reference parity for the served dashboard shell.

Every static asset that the served ``index.html`` references (``<script src>`` /
``<link href>``) must be in the serve whitelist, or the browser gets a 404 for it.
This is the regression that hid the Top Sessions panel: ``index.html`` loads
``sessions.js`` but it was absent from ``get_dashboard_files()``.

The mirror direction — whitelisted assets that nothing references — is pinned to a
known disposition set so a newly whitelisted-but-unwired asset forces an explicit
wire-or-remove decision (per the packet's disposition requirement) rather than
silently accreting dead package weight.
"""

from __future__ import annotations

import re

from tokenpak.dashboard import get_dashboard_files

# Whitelisted assets that no served HTML references, with a recorded disposition
# (see the PR: metrics.js/charts.js are legacy panels the current shell does not
# load; styles.css is superseded by index.html's inline <style>). Kept whitelisted
# so pre-existing bookmarks/tools do not 404; revisit if the shell starts loading
# them or they are removed from the package.
KNOWN_UNREFERENCED = frozenset({"metrics.js", "charts.js", "styles.css"})

# Reference extraction: local asset files only (relative, no scheme, no leading
# slash — that excludes nav links like href="/dashboard?mode=cli").
_SRC = re.compile(r'<script\b[^>]*\bsrc="([^"]+)"', re.IGNORECASE)
_HREF = re.compile(r'<link\b[^>]*\bhref="([^"]+)"', re.IGNORECASE)
_ASSET_SUFFIXES = (".js", ".css", ".html")


def _referenced_assets(html: str) -> set[str]:
    refs: set[str] = set()
    for ref in _SRC.findall(html) + _HREF.findall(html):
        candidate = ref.split("?", 1)[0].split("#", 1)[0]
        if candidate.startswith(("http://", "https://", "//", "/")):
            continue
        if candidate.endswith(_ASSET_SUFFIXES):
            refs.add(candidate)
    return refs


def _served_index_html() -> str:
    return get_dashboard_files()["index.html"].read_text(encoding="utf-8")


def test_every_referenced_asset_is_servable() -> None:
    whitelist = set(get_dashboard_files())
    referenced = _referenced_assets(_served_index_html())
    missing = referenced - whitelist
    assert not missing, (
        f"served index.html references assets that are not in the serve whitelist "
        f"(will 404): {sorted(missing)}"
    )


def test_sessions_js_is_referenced_and_servable() -> None:
    """Direct regression: the Top Sessions script must be both loaded and served."""
    referenced = _referenced_assets(_served_index_html())
    assert "sessions.js" in referenced, "index.html no longer loads sessions.js"
    assert "sessions.js" in get_dashboard_files()


def test_unreferenced_whitelisted_assets_match_known_disposition() -> None:
    whitelist = set(get_dashboard_files())
    referenced = _referenced_assets(_served_index_html())
    # index.html is the shell document, not an asset referenced by itself.
    unreferenced = whitelist - referenced - {"index.html"}
    assert unreferenced == KNOWN_UNREFERENCED, (
        "whitelisted-but-unreferenced dashboard assets changed; give the new "
        f"asset(s) a wire-or-remove disposition. expected={sorted(KNOWN_UNREFERENCED)} "
        f"actual={sorted(unreferenced)}"
    )
