"""
TokenPak Cost Calculation Engine (cost.py)

Service layer for cost calculation with:
  - tp_pricing table for versioned per-model rates
  - Baseline / actual / savings formulas
  - Pricing version resolution by event timestamp
  - CostResult dataclass
  - Reprocessing support helpers

The existing PricingCatalog (pricing.py) handles per-token math;
this module owns the DB schema, version resolution, and orchestration.

Usage:
    engine = CostEngine(db_path="telemetry.db")
    result = engine.calculate(
        model="claude-sonnet-4-6",
        raw_input_tokens=10000,
        final_input_tokens=6000,
        output_tokens=500,
        event_ts="2026-02-27T12:00:00Z",
    )
    # result.baseline_cost, result.actual_cost, result.savings_amount
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache cost multipliers — per-provider cache read/creation pricing
# Transferred from monolith (TPK-CONSOLIDATION-A2a)
# Source: Provider pricing docs (see CACHE-P4-002 task)
# read = fraction of input cost for cached tokens
# creation = multiplier on input cost for cache write (Anthropic only has surcharge)
# ---------------------------------------------------------------------------
try:
    from tokenpak.core.runtime.providers import Provider as _Provider

    CACHE_COST_MULTIPLIERS: Dict[object, Dict[str, float]] = {
        _Provider.ANTHROPIC: {"read": 0.10, "creation": 1.25},  # reads=10%, creation=125%
        _Provider.OPENAI: {"read": 0.50, "creation": 1.0},  # reads=50%, no creation surcharge
        _Provider.AZURE_OPENAI: {"read": 0.50, "creation": 1.0},
        _Provider.XAI: {"read": 0.50, "creation": 1.0},
        _Provider.GROQ: {"read": 0.0, "creation": 1.0},  # Free (volatile cache)
        _Provider.FIREWORKS: {"read": 0.0, "creation": 1.0},  # No cache pricing surcharge
        _Provider.TOGETHER: {"read": 0.0, "creation": 1.0},  # No cache pricing surcharge
        _Provider.GEMINI: {"read": 0.25, "creation": 1.0},  # 25% of input cost
        _Provider.BEDROCK: {"read": 0.10, "creation": 1.0},  # 10% of input cost
        _Provider.CODEX: {"read": 0.50, "creation": 1.0},  # Follows OpenAI pricing
        _Provider.UNKNOWN: {"read": 0.10, "creation": 1.25},  # Conservative default
    }
except (ImportError, AttributeError):
    # Fallback to string-keyed dict if Provider enum is unavailable
    CACHE_COST_MULTIPLIERS: Dict[str, Dict[str, float]] = {  # type: ignore[no-redef]
        "anthropic": {"read": 0.10, "creation": 1.25},
        "openai": {"read": 0.50, "creation": 1.0},
        "azure_openai": {"read": 0.50, "creation": 1.0},
        "xai": {"read": 0.50, "creation": 1.0},
        "groq": {"read": 0.0, "creation": 1.0},
        "fireworks": {"read": 0.0, "creation": 1.0},
        "together": {"read": 0.0, "creation": 1.0},
        "gemini": {"read": 0.25, "creation": 1.0},
        "bedrock": {"read": 0.10, "creation": 1.0},
        "codex": {"read": 0.50, "creation": 1.0},
        "unknown": {"read": 0.10, "creation": 1.25},
    }

# ---------------------------------------------------------------------------
# Current pricing rates (USD per 1,000,000 tokens).
# Loaded from the model registry seed so telemetry and routing cannot drift.
# ---------------------------------------------------------------------------
PricingSeedValue = str | float | None
PricingRowValue = str | int | float


def _load_seed_pricing() -> tuple[List[dict[str, PricingSeedValue]], str, str]:
    """Build telemetry seed rows from the canonical model catalog."""
    catalog_path = Path(__file__).parents[1] / "models" / "data" / "seed_catalog.json"
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    meta = raw["_meta"]
    rows: List[dict[str, PricingSeedValue]] = []
    for model, data in raw["models"].items():
        rows.append(
            {
                "provider": data["provider"],
                "model": model,
                "input_rate": float(data["input"]),
                "output_rate": float(data["output"]),
                "cache_read_rate": (
                    float(data["cache_read"]) if data.get("cache_read") is not None else None
                ),
                "cache_write_rate": (
                    float(data["cache_write"]) if data.get("cache_write") is not None else None
                ),
                "source": data.get("source", "official"),
            }
        )
    return rows, str(meta["pricing_version"]), str(meta["effective_date"])


SEED_PRICING, CURRENT_PRICING_VERSION, CURRENT_EFFECTIVE_DATE = _load_seed_pricing()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class CostResult:
    """Result of a cost calculation for a single event."""

    model: str
    pricing_version: str
    raw_input_tokens: int
    final_input_tokens: int
    output_tokens: int
    baseline_cost: float  # cost if no compression applied
    actual_cost: float  # cost after compression
    savings_amount: float  # baseline - actual (never negative)
    savings_pct: float  # savings_amount / baseline_cost * 100
    data_source: str  # "official" | "estimated" | "fallback"

    def to_dict(self) -> dict[str, PricingRowValue]:
        return {
            "model": self.model,
            "pricing_version": self.pricing_version,
            "raw_input_tokens": self.raw_input_tokens,
            "final_input_tokens": self.final_input_tokens,
            "output_tokens": self.output_tokens,
            "baseline_cost": round(self.baseline_cost, 6),
            "actual_cost": round(self.actual_cost, 6),
            "savings_amount": round(self.savings_amount, 6),
            "savings_pct": round(self.savings_pct, 4),
            "data_source": self.data_source,
        }


@dataclass
class Pricing:
    """A single model pricing record."""

    provider: str
    model: str
    input_rate: float  # USD per 1M tokens
    output_rate: float  # USD per 1M tokens
    version: str
    effective_date: str
    source: str = "official"
    cache_read_rate: float | None = None  # USD per 1M cache-read tokens
    cache_write_rate: float | None = None  # USD per 1M cache-write tokens

    @property
    def input_per_token(self) -> float:
        return self.input_rate / 1_000_000.0

    @property
    def output_per_token(self) -> float:
        return self.output_rate / 1_000_000.0

    @property
    def cache_read_per_token(self) -> float | None:
        if self.cache_read_rate is None:
            return None
        return self.cache_read_rate / 1_000_000.0

    @property
    def cache_write_per_token(self) -> float | None:
        if self.cache_write_rate is None:
            return None
        return self.cache_write_rate / 1_000_000.0


# ---------------------------------------------------------------------------
# Cost Engine
# ---------------------------------------------------------------------------
class CostEngine:
    """
    Cost calculation service with DB-backed versioned pricing.

    Args:
        db_path: Path to telemetry SQLite database.
    """

    DDL = """
    CREATE TABLE IF NOT EXISTS tp_pricing (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        version        TEXT    NOT NULL,
        effective_date DATE    NOT NULL,
        provider       TEXT    NOT NULL,
        model          TEXT    NOT NULL,
        input_rate     REAL    NOT NULL,
        output_rate    REAL    NOT NULL,
        cache_read_rate REAL,
        cache_write_rate REAL,
        currency       TEXT    NOT NULL DEFAULT 'USD',
        source         TEXT    NOT NULL DEFAULT 'official',
        created_at     DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_tp_pricing_model
        ON tp_pricing(model, effective_date);
    CREATE INDEX IF NOT EXISTS idx_tp_pricing_version
        ON tp_pricing(version);
    """

    # Uniqueness key: without it, concurrent COUNT-then-seed races (the
    # in-process lock does not cover other processes) insert the full seed
    # set twice, and pricing lookups return arbitrary duplicate rows.
    # Applied as an additive migration; pre-existing duplicate rows are
    # deduped (newest row wins) in the same transaction before the unique
    # index is created.
    _UNIQUE_INDEX_DDL = (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tp_pricing_unique "
        "ON tp_pricing(version, provider, model)"
    )

    # Fallback rates for unknown models
    _FALLBACK_INPUT_RATE = 3.00  # USD/1M (sonnet-tier estimate)
    _FALLBACK_OUTPUT_RATE = 15.00

    def __init__(self, db_path: str = ""):
        from tokenpak.core.paths import get_db_path

        resolved_path = Path(db_path).expanduser() if db_path else get_db_path("telemetry.db")
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved_path)
        self._lock = threading.Lock()
        self._pricing_cache: dict[tuple[str, str], Pricing] = {}
        self._init_db()

    # ------------------------------------------------------------------
    # DB init & seeding
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create tp_pricing table and seed with current rates if empty."""
        with self._lock:
            conn = self._connect()
            for stmt in self.DDL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.commit()
            self._ensure_rate_columns(conn)
            self._ensure_unique_pricing_key(conn)

            # Seed every catalog version. INSERT OR IGNORE makes this
            # idempotent while allowing a refreshed version to reach an
            # already-existing telemetry database.
            self._seed(conn)
            conn.close()

    @staticmethod
    def _ensure_rate_columns(conn: sqlite3.Connection) -> None:
        """Add cache-rate columns to databases created by older releases."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tp_pricing)").fetchall()}
        for name in ("cache_read_rate", "cache_write_rate"):
            if name not in columns:
                conn.execute(f"ALTER TABLE tp_pricing ADD COLUMN {name} REAL")
        conn.commit()

    @staticmethod
    def _ensure_unique_pricing_key(conn: sqlite3.Connection) -> None:
        """Create the UNIQUE(version, provider, model) index.

        Databases seeded before the uniqueness key existed may contain
        duplicate pricing rows; dedupe (keep the newest row per key, i.e.
        max rowid) and create the index inside one transaction so a crash
        mid-migration leaves the database unchanged.
        """
        try:
            conn.execute(CostEngine._UNIQUE_INDEX_DDL)
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            conn.execute(
                """
                DELETE FROM tp_pricing
                WHERE rowid NOT IN (
                    SELECT MAX(rowid) FROM tp_pricing
                    GROUP BY version, provider, model
                )
                """
            )
            conn.execute(CostEngine._UNIQUE_INDEX_DDL)
            conn.commit()

    def _seed(self, conn: sqlite3.Connection) -> None:
        """Insert default pricing rows (idempotent via the uniqueness key)."""
        for row in SEED_PRICING:
            conn.execute(
                """INSERT OR IGNORE INTO tp_pricing
                   (version, effective_date, provider, model, input_rate, output_rate,
                    cache_read_rate, cache_write_rate, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    CURRENT_PRICING_VERSION,
                    CURRENT_EFFECTIVE_DATE,
                    row["provider"],
                    row["model"],
                    row["input_rate"],
                    row["output_rate"],
                    row["cache_read_rate"],
                    row["cache_write_rate"],
                    row["source"],
                ),
            )
        conn.commit()
        logger.info(
            f"tp_pricing seeded with {len(SEED_PRICING)} rows (version {CURRENT_PRICING_VERSION})"
        )

    # ------------------------------------------------------------------
    # Pricing resolution
    # ------------------------------------------------------------------
    def get_pricing(self, model: str, event_ts: Optional[str] = None) -> Pricing:
        """
        Resolve pricing for a model at a given event timestamp.

        Uses the most recent version whose effective_date <= event date.
        Falls back to fallback pricing if model is unknown.

        Args:
            model: Model identifier (e.g. "claude-sonnet-4-6")
            event_ts: ISO timestamp of the event (default: now)

        Returns:
            Pricing record.
        """
        event_date = self._parse_date(event_ts)
        cache_key = (model, event_date)

        if cache_key in self._pricing_cache:
            return self._pricing_cache[cache_key]

        conn = self._connect()
        # Exact match first
        row = conn.execute(
            """SELECT * FROM tp_pricing
               WHERE model = ? AND effective_date <= ?
               ORDER BY effective_date DESC LIMIT 1""",
            (model, event_date),
        ).fetchone()

        if row is None:
            # Try fuzzy: partial model name match
            row = self._fuzzy_match(conn, model, event_date)

        conn.close()

        if row:
            pricing = Pricing(
                provider=row["provider"],
                model=row["model"],
                input_rate=row["input_rate"],
                output_rate=row["output_rate"],
                version=row["version"],
                effective_date=row["effective_date"],
                source=row["source"],
                cache_read_rate=row["cache_read_rate"],
                cache_write_rate=row["cache_write_rate"],
            )
        else:
            # Fallback pricing for unknown models
            pricing = Pricing(
                provider="unknown",
                model=model,
                input_rate=self._FALLBACK_INPUT_RATE,
                output_rate=self._FALLBACK_OUTPUT_RATE,
                version="fallback",
                effective_date=event_date,
                source="estimated",
            )
            logger.warning(f"No pricing found for model '{model}', using fallback")

        self._pricing_cache[cache_key] = pricing
        return pricing

    def _fuzzy_match(
        self, conn: sqlite3.Connection, model: str, event_date: str
    ) -> Optional[sqlite3.Row]:
        """Try matching by model name substring."""
        model_lower = model.lower()
        rows = cast(
            list[sqlite3.Row],
            conn.execute(
                """SELECT * FROM tp_pricing
                   WHERE effective_date <= ?
                   ORDER BY effective_date DESC, LENGTH(model) DESC""",
                (event_date,),
            ).fetchall(),
        )
        for row in rows:
            row_model = str(row["model"]).lower()
            if row_model in model_lower:
                return row
        return None

    @staticmethod
    def _parse_date(ts: Optional[str]) -> str:
        """Parse a timestamp string to YYYY-MM-DD, defaulting to today."""
        if not ts:
            return datetime.now(timezone.utc).date().isoformat()
        try:
            # Handle various ISO formats
            dt = ts.replace("Z", "+00:00")
            return datetime.fromisoformat(dt).date().isoformat()
        except (ValueError, AttributeError):
            return datetime.now(timezone.utc).date().isoformat()

    # ------------------------------------------------------------------
    # Cost calculation
    # ------------------------------------------------------------------
    def calculate(
        self,
        model: str,
        raw_input_tokens: int,
        final_input_tokens: int,
        output_tokens: int,
        event_ts: Optional[str] = None,
        cache_read_tokens: int = 0,
    ) -> CostResult:
        """
        Calculate baseline, actual, and savings for a single event.

        Args:
            model: Model identifier.
            raw_input_tokens: Tokens BEFORE compression (for baseline).
            final_input_tokens: Tokens AFTER compression (actual billing).
            output_tokens: Output tokens (same for baseline and actual).
            event_ts: Event ISO timestamp for pricing version resolution.
            cache_read_tokens: Tokens served from prompt cache.

        Returns:
            CostResult with all cost fields.
        """
        # Clamp negative values
        raw = max(0, raw_input_tokens)
        final = max(0, final_input_tokens)
        out = max(0, output_tokens)
        cache_read = min(max(0, cache_read_tokens), final)

        pricing = self.get_pricing(model, event_ts)

        # Baseline: what would have been billed without compression
        baseline_cost = raw * pricing.input_per_token + out * pricing.output_per_token

        # Actual: billed tokens after compression
        effective_input = final - cache_read
        # A missing cache rate means the model has no verified cache discount;
        # charge those tokens at the normal input rate instead of treating them
        # as free.
        cache_read_rate = pricing.cache_read_per_token
        if cache_read_rate is None:
            cache_read_rate = pricing.input_per_token
        actual_cost = (
            effective_input * pricing.input_per_token
            + cache_read * cache_read_rate
            + out * pricing.output_per_token
        )

        # Savings (never negative — rounding artifacts clamped)
        savings_amount = max(0.0, baseline_cost - actual_cost)
        savings_pct = (savings_amount / baseline_cost * 100.0) if baseline_cost > 0 else 0.0

        return CostResult(
            model=model,
            pricing_version=pricing.version,
            raw_input_tokens=raw,
            final_input_tokens=final,
            output_tokens=out,
            baseline_cost=baseline_cost,
            actual_cost=actual_cost,
            savings_amount=savings_amount,
            savings_pct=savings_pct,
            data_source=pricing.source,
        )

    # ------------------------------------------------------------------
    # Pricing catalog management
    # ------------------------------------------------------------------
    def list_pricing(self, version: Optional[str] = None) -> List[dict[str, object]]:
        """List all pricing entries, optionally filtered by version."""
        conn = self._connect()
        if version:
            rows = conn.execute(
                "SELECT * FROM tp_pricing WHERE version = ? ORDER BY provider, model",
                (version,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tp_pricing ORDER BY version DESC, provider, model"
            ).fetchall()
        conn.close()
        return [{str(column): value for column, value in dict(row).items()} for row in rows]

    def add_pricing(
        self,
        provider: str,
        model: str,
        input_rate: float,
        output_rate: float,
        version: Optional[str] = None,
        effective_date: Optional[str] = None,
        source: str = "official",
        cache_read_rate: float | None = None,
        cache_write_rate: float | None = None,
    ) -> int:
        """Insert a new pricing record. Returns the new row id."""
        version = version or CURRENT_PRICING_VERSION
        effective_date = effective_date or datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """INSERT INTO tp_pricing
                   (version, effective_date, provider, model, input_rate, output_rate,
                    cache_read_rate, cache_write_rate, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version,
                    effective_date,
                    provider,
                    model,
                    input_rate,
                    output_rate,
                    cache_read_rate,
                    cache_write_rate,
                    source,
                ),
            )
            conn.commit()
            row_id = cur.lastrowid
            conn.close()
        # Invalidate cache
        self._pricing_cache.clear()
        assert row_id is not None
        return row_id

    # ------------------------------------------------------------------
    # Reprocessing
    # ------------------------------------------------------------------
    def reprocess_costs(
        self,
        from_date: str,
        to_date: str,
        pricing_version: Optional[str] = None,
    ) -> dict[str, str | int]:
        """
        Recalculate costs for events in a date range.

        Reads raw/final token counts from tp_usage + tp_events,
        recalculates using current (or specified) pricing,
        and updates tp_costs rows.

        Args:
            from_date: Start date YYYY-MM-DD (inclusive).
            to_date:   End date YYYY-MM-DD (inclusive).
            pricing_version: Override pricing version (default: current).

        Returns:
            Summary dict with rows_processed, rows_updated, errors.
        """
        conn = self._connect()
        rows_processed = 0
        rows_updated = 0
        errors = 0

        try:
            # Fetch events in range
            events = conn.execute(
                """SELECT e.trace_id, e.model, e.ts,
                          u.input_billed AS final_input, u.input_est AS raw_input,
                          u.output_billed AS output
                   FROM tp_events e
                   LEFT JOIN tp_usage u ON u.trace_id = e.trace_id
                   WHERE DATE(e.ts) >= ? AND DATE(e.ts) <= ?
                     AND e.status != 'error'""",
                (from_date, to_date),
            ).fetchall()

            for event in events:
                rows_processed += 1
                try:
                    model = event["model"] or "unknown"
                    raw = event["raw_input"] or 0
                    final = event["final_input"] or 0
                    out = event["output"] or 0
                    ts = event["ts"]

                    # Use override version if specified
                    if pricing_version:
                        pricing = self._get_pricing_by_version(conn, model, pricing_version)
                        if pricing is None:
                            pricing = self.get_pricing(model, ts)
                    else:
                        pricing = self.get_pricing(model, ts)

                    result = self.calculate(model, raw, final, out, event_ts=ts)

                    # Update tp_costs
                    existing = conn.execute(
                        "SELECT trace_id FROM tp_costs WHERE trace_id = ?",
                        (event["trace_id"],),
                    ).fetchone()

                    if existing:
                        conn.execute(
                            """UPDATE tp_costs SET
                               baseline_cost = ?, actual_cost = ?,
                               savings_total = ?, pricing_version = ?,
                               cost_source = ?
                               WHERE trace_id = ?""",
                            (
                                result.baseline_cost,
                                result.actual_cost,
                                result.savings_amount,
                                result.pricing_version,
                                result.data_source,
                                event["trace_id"],
                            ),
                        )
                        rows_updated += 1
                except Exception as e:
                    logger.warning(f"Reprocess error for trace {event['trace_id']}: {e}")
                    errors += 1

            conn.commit()
        finally:
            conn.close()

        logger.info(
            f"Reprocess complete: {rows_processed} events, "
            f"{rows_updated} updated, {errors} errors "
            f"({from_date} → {to_date})"
        )
        return {
            "from_date": from_date,
            "to_date": to_date,
            "rows_processed": rows_processed,
            "rows_updated": rows_updated,
            "errors": errors,
            "pricing_version": pricing_version or CURRENT_PRICING_VERSION,
        }

    def _get_pricing_by_version(
        self, conn: sqlite3.Connection, model: str, version: str
    ) -> Optional[Pricing]:
        """Look up pricing for a specific version."""
        row = conn.execute(
            "SELECT * FROM tp_pricing WHERE model = ? AND version = ? LIMIT 1",
            (model, version),
        ).fetchone()
        if row:
            return Pricing(
                provider=row["provider"],
                model=row["model"],
                input_rate=row["input_rate"],
                output_rate=row["output_rate"],
                version=row["version"],
                effective_date=row["effective_date"],
                source=row["source"],
                cache_read_rate=row["cache_read_rate"],
                cache_write_rate=row["cache_write_rate"],
            )
        return None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------
def calculate_baseline(raw_input_tokens: int, output_tokens: int, pricing: Pricing) -> float:
    """Compute baseline cost (no compression applied)."""
    return max(
        0.0, raw_input_tokens * pricing.input_per_token + output_tokens * pricing.output_per_token
    )


def calculate_actual(
    final_input_tokens: int, output_tokens: int, pricing: Pricing, cache_read_tokens: int = 0
) -> float:
    """Compute actual cost (after compression)."""
    final = max(0, final_input_tokens)
    cache_read = min(max(0, cache_read_tokens), final)
    effective = final - cache_read
    cache_read_rate = pricing.cache_read_per_token
    if cache_read_rate is None:
        cache_read_rate = pricing.input_per_token
    return max(
        0.0,
        effective * pricing.input_per_token
        + cache_read * cache_read_rate
        + max(0, output_tokens) * pricing.output_per_token,
    )


def calculate_savings(baseline: float, actual: float) -> tuple[float, float]:
    """Return (savings_amount, savings_pct). Never negative."""
    amount = max(0.0, baseline - actual)
    pct = (amount / baseline * 100.0) if baseline > 0 else 0.0
    return amount, pct
