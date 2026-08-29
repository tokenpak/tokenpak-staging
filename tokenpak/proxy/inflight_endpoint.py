# SPDX-License-Identifier: Apache-2.0
"""Read-only ``/inflight`` endpoint — lists admitted in-flight requests.

Local-only (served by the proxy's own HTTP handler, behind the same
``_enforce_proxy_auth()`` gate as every other GET route — see
``proxy/server.py::do_GET``) and strictly read-only: this module never
writes to ``inflight_registry`` or ``spend_guard.rolling_caps``, it only
reads their already-computed state. No estimator math, no time-remaining or
ETA figures — elapsed time and token counts are the only numbers reported,
both already tracked elsewhere for other purposes.
"""

from __future__ import annotations

from tokenpak.proxy.inflight_registry import snapshot as _inflight_snapshot


def build_response() -> dict:
    """Build the JSON-serializable body for ``GET /inflight``."""
    entries = _inflight_snapshot()
    return {"in_flight": entries, "count": len(entries)}
