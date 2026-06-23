---
"tokenpak": minor
---

Dashboard: rename the public dashboard-modes constant to a public-safe name and
keep the old name as a deprecation alias.

The exported constant `tokenpak.dashboard.CCI09_DASHBOARD_MODES` embedded an
internal review id ("CCI09") in a public-API symbol name. It is renamed to the
public-safe `tokenpak.dashboard.DASHBOARD_MODES` (same value — the supported
render modes `("cli", "tui", "tmux", "sdk", "ide", "cron")`).

Public-API change (additive; public-API snapshot contract per Std 21 §11.2):
- `tokenpak.dashboard.DASHBOARD_MODES` — new canonical public symbol.
- `tokenpak.dashboard.CCI09_DASHBOARD_MODES` — retained as a deprecation alias.
  Accessing it now emits a `DeprecationWarning` (PEP 562 module `__getattr__`)
  and resolves to `DASHBOARD_MODES`. It remains in the public-API snapshot and
  is scheduled for removal in the next minor release — NOT a bare rename.

No public-API removals in this change. Release scheduling for the alias removal
is a separate release-gate decision and is not made here.
