"""Reusable value-report object — three confidence tiers for status + cost.

Item A of the Universal Value Reporting CLI proposal. One report object,
consumed by both ``tokenpak status`` (concise snapshot) and ``tokenpak cost``
(deeper breakdown), so dollar figures are never presented as raw
``estimated_cost`` SQL sums.

Tiers mirror the existing telemetry confidence vocabulary
(``Cost.cost_source`` = provider|estimated|unknown) and the proxy attribution
contract (``cache_origin`` = proxy|client|unknown):

  confirmed  provider-confirmed savings: proxy-placed cache reads
             (origin='proxy') priced at (input_rate - cache_read_rate).
  estimated  TokenPak-modeled value: compression + would-have-saved tokens
             priced at the model input rate via the pricing catalog.
  unpriced   token/request efficiency with no defensible dollar conversion:
             platform/client-managed cache (origin='client' / unattributed)
             and tokens for models absent from the pricing catalog.

Truth-over-polish (Std 00 §5.3): a value is only labeled ``confirmed`` when
the provider confirmed the cache read on a proxy-placed block. Everything
modeled is labeled ``estimated``; everything we cannot defensibly price is
surfaced as token efficiency under ``unpriced`` rather than fabricated into a
dollar figure.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

TIER_CONFIRMED = "confirmed"
TIER_ESTIMATED = "estimated"
TIER_UNPRICED = "unpriced"

# Columns the report reads. Queried defensively against PRAGMA table_info so
# older schemas (or fixture DBs) that omit some of these degrade gracefully
# rather than raising.
_OPTIONAL_COLS = (
    "model",
    "compressed_tokens",
    "would_have_saved",
    "cache_read_tokens",
    "cache_origin",
    "estimated_cost",
    "timestamp",
)


def _default_db_path() -> Path:
    """Resolve the monitor DB path, honoring ``TOKENPAK_HOME`` for testability.

    Matches the resolution used elsewhere in the CLI (``~/.tokenpak``) while
    allowing tests / sandboxes to redirect via ``TOKENPAK_HOME``.
    """
    home = Path(os.environ.get("TOKENPAK_HOME", Path.home() / ".tokenpak"))
    return home / "monitor.db"


@dataclass
class Tier:
    """A single confidence tier of value."""

    label: str
    dollars: float = 0.0
    tokens: int = 0
    priced: bool = True

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "dollars": round(self.dollars, 6),
            "tokens": self.tokens,
            "priced": self.priced,
        }


@dataclass
class ModelValueRow:
    """Per-model value, rendered uniformly across heterogeneous telemetry."""

    model: str
    requests: int = 0
    confirmed_usd: float = 0.0
    estimated_usd: float = 0.0
    unpriced_tokens: int = 0
    priced: bool = True

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "requests": self.requests,
            "confirmed_usd": round(self.confirmed_usd, 6),
            "estimated_usd": round(self.estimated_usd, 6),
            "unpriced_tokens": self.unpriced_tokens,
            "priced": self.priced,
        }


@dataclass
class ValueReport:
    """Three-tier value report shared by status + cost."""

    confirmed: Tier = field(default_factory=lambda: Tier(TIER_CONFIRMED))
    estimated: Tier = field(default_factory=lambda: Tier(TIER_ESTIMATED))
    unpriced: Tier = field(default_factory=lambda: Tier(TIER_UNPRICED, priced=False))
    per_model: list[ModelValueRow] = field(default_factory=list)
    total_requests: int = 0
    total_estimated_cost: float = 0.0
    window_label: str = "all time"
    db_available: bool = False

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        db_path: Optional[os.PathLike] = None,
        since: Optional[str] = None,
        window_label: str = "all time",
        catalog=None,
    ) -> "ValueReport":
        """Build the report from the monitor DB.

        Parameters
        ----------
        db_path:
            Path to ``monitor.db``. Defaults to the resolved CLI path.
        since:
            Optional ISO timestamp lower bound (applied to ``timestamp`` when
            that column exists).
        window_label:
            Human label for the window, surfaced in render output.
        catalog:
            Optional pre-loaded ``PricingCatalog`` (injected in tests). When
            ``None`` the bundled catalog is loaded; if loading fails every
            value falls back to ``unpriced`` token efficiency.
        """
        report = cls(window_label=window_label)
        path = Path(db_path) if db_path is not None else _default_db_path()

        rows = _aggregate_by_model(path, since)
        if rows is None:
            # DB missing / unreadable — empty report, db_available stays False.
            return report
        report.db_available = True

        if catalog is None:
            try:
                from tokenpak.telemetry.pricing import PricingCatalog

                catalog = PricingCatalog.load()
            except Exception:
                catalog = None

        for row in rows:
            report._absorb_model(row, catalog)

        # Stable, useful ordering: priced models by total dollar value desc,
        # then unpriced models by token volume desc.
        report.per_model.sort(
            key=lambda m: (m.confirmed_usd + m.estimated_usd, m.unpriced_tokens),
            reverse=True,
        )
        return report

    def _absorb_model(self, row: dict, catalog) -> None:
        model = row["model"] or "unknown"
        requests = int(row.get("requests", 0))
        compressed = int(row.get("compressed_tokens", 0))
        would_saved = int(row.get("would_have_saved", 0))
        proxy_cache = int(row.get("proxy_cache_tokens", 0))
        client_cache = int(row.get("client_cache_tokens", 0))
        other_cache = int(row.get("other_cache_tokens", 0))
        est_cost = float(row.get("estimated_cost", 0.0))

        self.total_requests += requests
        self.total_estimated_cost += est_cost

        pricing = catalog.get_model(model) if catalog is not None else None

        confirmed_usd = 0.0
        estimated_usd = 0.0
        # Client/unattributed cache is always unpriced token efficiency.
        unpriced_tokens = client_cache + other_cache

        if pricing is None:
            # Model not in the catalog: we know tokens were saved but cannot
            # responsibly price them. Surface as token efficiency.
            unpriced_tokens += proxy_cache + compressed + would_saved
            priced = False
        else:
            priced = True
            input_rate = pricing.input_per_token
            cr_rate = pricing.cache_read_per_token
            # Confirmed: proxy-placed cache reads, billed at cache_read rate
            # instead of full input rate. Only defensible when the model has a
            # cache-read rate (Anthropic). Otherwise the cache tokens are real
            # efficiency but not priceable as a confirmed dollar saving.
            if cr_rate is not None and proxy_cache > 0:
                confirmed_usd = proxy_cache * max(0.0, input_rate - cr_rate)
            elif proxy_cache > 0:
                unpriced_tokens += proxy_cache
            # Estimated: modeled compression value (tokens removed before send,
            # plus savings that would have applied on byte-preserve routes).
            estimated_usd = (compressed + would_saved) * input_rate

        self.confirmed.dollars += confirmed_usd
        self.estimated.dollars += estimated_usd
        self.estimated.tokens += compressed + would_saved if priced else 0
        self.confirmed.tokens += proxy_cache if (priced and confirmed_usd > 0) else 0
        self.unpriced.tokens += unpriced_tokens

        self.per_model.append(
            ModelValueRow(
                model=model,
                requests=requests,
                confirmed_usd=confirmed_usd,
                estimated_usd=estimated_usd,
                unpriced_tokens=unpriced_tokens,
                priced=priced,
            )
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def as_dict(self) -> dict:
        return {
            "window": self.window_label,
            "db_available": self.db_available,
            "total_requests": self.total_requests,
            "total_estimated_cost": round(self.total_estimated_cost, 6),
            "tiers": {
                TIER_CONFIRMED: self.confirmed.as_dict(),
                TIER_ESTIMATED: self.estimated.as_dict(),
                TIER_UNPRICED: self.unpriced.as_dict(),
            },
            "per_model": [m.as_dict() for m in self.per_model],
        }

    # ------------------------------------------------------------------
    # Rendering — plain text so both status + cost can print uniformly.
    # ------------------------------------------------------------------

    def render(self, verbose: bool = False) -> str:
        """Render the tiered value block.

        ``verbose=False`` (status snapshot): three labeled tier lines + a
        one-line confidence footer. ``verbose=True`` (cost breakdown): adds a
        uniform per-model table.
        """
        if not self.db_available:
            return "  Value: no telemetry yet (run some requests through the proxy)."

        lines: list[str] = []
        lines.append(f"  Value ({self.window_label}, confidence-tiered):")
        lines.append(
            f"    Confirmed:  ${self.confirmed.dollars:.4f}"
            "   provider-confirmed cache savings"
        )
        lines.append(
            f"    Estimated:  ${self.estimated.dollars:.4f}"
            "   TokenPak-modeled compression value"
        )
        lines.append(
            f"    Unpriced:   {self.unpriced.tokens:,} tok"
            "   efficiency without a defensible $ value"
        )

        if verbose and self.per_model:
            lines.append("")
            lines.append(
                f"    {'MODEL':<28}{'REQ':>7}{'CONFIRMED$':>13}"
                f"{'ESTIMATED$':>13}{'UNPRICED(tok)':>16}"
            )
            lines.append(f"    {'-' * 73}")
            for m in self.per_model:
                tag = "" if m.priced else "  (unpriced model)"
                lines.append(
                    f"    {m.model[:28]:<28}{m.requests:>7}"
                    f"{m.confirmed_usd:>13.4f}{m.estimated_usd:>13.4f}"
                    f"{m.unpriced_tokens:>16,}{tag}"
                )

        lines.append(
            "    Labels: confirmed = provider cache hits on TokenPak-placed "
            "blocks; estimated = modeled; unpriced = token efficiency."
        )
        lines.append("    Run `tokenpak status --explain` for tier details.")
        return "\n".join(lines)

    def explain(self) -> str:
        """Confidence-notes surface for ``tokenpak status --explain`` (no arg)."""
        return "\n".join(
            [
                "Value confidence tiers — how TokenPak reports savings:",
                "",
                f"  confirmed  (${self.confirmed.dollars:.4f})",
                "    Provider-confirmed savings. Cache reads served on cache_control",
                "    blocks that TokenPak placed (cache_origin='proxy'), priced as the",
                "    gap between the full input rate and the provider's cache-read rate.",
                "    This is the only tier presented as a hard dollar saving.",
                "",
                f"  estimated  (${self.estimated.dollars:.4f})",
                "    TokenPak-modeled value. Tokens removed by compression (and savings",
                "    that would have applied on byte-preserve routes), priced at each",
                "    model's input rate. A model is what TokenPak would have paid had",
                "    those tokens been sent — modeled, not provider-confirmed.",
                "",
                f"  unpriced   ({self.unpriced.tokens:,} tokens)",
                "    Real token / request efficiency with no defensible dollar value:",
                "    platform-managed cache (cache_origin='client', not credited),",
                "    unattributed cache, and models absent from the pricing catalog.",
                "    Surfaced as token efficiency rather than a fabricated dollar figure.",
            ]
        )


def _aggregate_by_model(db_path: Path, since: Optional[str]) -> Optional[list[dict]]:
    """Return per-model aggregate rows, or ``None`` if the DB is unusable.

    Defensive against schema drift: only columns present in ``requests`` are
    referenced; missing columns degrade to zero rather than raising.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=1.0)
    except sqlite3.Error:
        return None
    try:
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
        except sqlite3.OperationalError:
            return None
        if not cols:
            return None

        has = {c: (c in cols) for c in _OPTIONAL_COLS}
        model_expr = "COALESCE(model, 'unknown')" if has["model"] else "'unknown'"

        def _sum(col: str) -> str:
            return f"COALESCE(SUM({col}), 0)" if has[col] else "0"

        # Origin-split cache reads.
        if has["cache_read_tokens"] and has["cache_origin"]:
            proxy_expr = (
                "COALESCE(SUM(CASE WHEN cache_origin='proxy' "
                "THEN cache_read_tokens ELSE 0 END), 0)"
            )
            client_expr = (
                "COALESCE(SUM(CASE WHEN cache_origin='client' "
                "THEN cache_read_tokens ELSE 0 END), 0)"
            )
            other_expr = (
                "COALESCE(SUM(CASE WHEN cache_origin NOT IN ('proxy','client') "
                "OR cache_origin IS NULL THEN cache_read_tokens ELSE 0 END), 0)"
            )
        elif has["cache_read_tokens"]:
            # No origin column: cache reads are unattributed → unpriced.
            proxy_expr = "0"
            client_expr = "0"
            other_expr = _sum("cache_read_tokens")
        else:
            proxy_expr = client_expr = other_expr = "0"

        where = ""
        params: list = []
        if since and has["timestamp"]:
            where = "WHERE timestamp >= ?"
            params.append(since)

        sql = (
            f"SELECT {model_expr} AS model, "
            f"COUNT(*) AS requests, "
            f"{_sum('compressed_tokens')} AS compressed_tokens, "
            f"{_sum('would_have_saved')} AS would_have_saved, "
            f"{proxy_expr} AS proxy_cache_tokens, "
            f"{client_expr} AS client_cache_tokens, "
            f"{other_expr} AS other_cache_tokens, "
            f"{_sum('estimated_cost')} AS estimated_cost "
            f"FROM requests {where} GROUP BY {model_expr}"
        )
        try:
            cur = conn.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return None
    finally:
        conn.close()
