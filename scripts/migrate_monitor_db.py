#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""One-shot legacy monitor.db migration: detect -> merge -> archive.

Earlier releases wrote the request-ledger database to several different
locations. This script consolidates them into the canonical path resolved
by ``tokenpak._paths.monitor_db()``.

Legacy locations detected (relative to the user's home directory):

    ~/tokenpak/monitor.db           oldest layout (no leading dot)
    ~/.tokenpak/monitor.db          legacy TokenPak home
    ~/.tokenpak/data/monitor.db     legacy data/ subdir used by some commands

Canonical target (mirrors the resolver's precedence):

    1. ``$TOKENPAK_DB`` (or compat ``$TOKENPAK_MONITOR_DB``) when set
    2. ``~/.tpk/monitor.db``

Behavior:

* **Dry-run by default.** Prints the planned merges and row counts without
  touching the filesystem. Pass ``--apply`` to mutate.
* **Schema-agnostic merge.** Table columns are introspected at runtime
  (``PRAGMA table_info``) and rows are merged over the intersection of
  source/target columns; synthetic single-column INTEGER PRIMARY KEYs
  (rowid aliases such as ``id``) are excluded so autoincrement ids never
  collide and schema drift between releases cannot misalign columns.
  Tables present in a legacy DB but absent from the target are created
  from the legacy CREATE statement.
* **Dedupe on natural key.** A source row whose shared-column values
  already exist in the target (NULL-safe comparison) is skipped. The
  check spans sources within one invocation, so dry-run counts match
  what ``--apply`` would do when several legacy DBs overlap.
* **Fail loud on schema drift.** A source table that holds rows but
  shares zero columns with the target aborts that source BEFORE any
  mutation (no merge, no archive) and exits non-zero — legacy data is
  never silently dropped or hidden behind an archive rename.
* **Archive, never delete.** After a successful merge each legacy file is
  RENAMED to ``<name>.legacy-<YYYY-MM-DD>`` in place. Nothing is ever
  removed; user history stays on disk.
* **Idempotent.** Re-running after archival finds no legacy DBs and exits
  as a no-op. Re-running after a partial failure is safe: already-merged
  rows are detected as duplicates and skipped.

Exit codes: 0 = success / nothing to do, 1 = error (unreadable source,
archive rename failure, etc.).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sqlite3
import sys
from pathlib import Path

try:  # Prefer the canonical resolver constants when tokenpak is importable.
    from tokenpak import _paths as _tp_paths

    _CANONICAL_DIRNAME = _tp_paths.CANONICAL_DIRNAME
    _LEGACY_DIRNAME = _tp_paths.LEGACY_DIRNAME
except Exception:  # pragma: no cover - standalone fallback
    _tp_paths = None
    _CANONICAL_DIRNAME = ".tpk"
    _LEGACY_DIRNAME = ".tokenpak"

_ENV_PRIMARY = "TOKENPAK_DB"
_ENV_COMPAT = "TOKENPAK_MONITOR_DB"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def resolve_target() -> Path:
    """Canonical merge target, mirroring the resolver's precedence.

    Env override first (same order as ``tokenpak._paths``), then the
    canonical fresh-install path. The resolver's legacy candidates are
    deliberately NOT consulted here — they are exactly what this script
    drains.
    """
    env_val = os.environ.get(_ENV_PRIMARY, "").strip()
    if not env_val:
        env_val = os.environ.get(_ENV_COMPAT, "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / _CANONICAL_DIRNAME / "monitor.db"


def legacy_candidates() -> list[Path]:
    """All legacy monitor.db locations this script knows how to drain."""
    home = Path.home()
    return [
        home / "tokenpak" / "monitor.db",
        home / _LEGACY_DIRNAME / "monitor.db",
        home / _LEGACY_DIRNAME / "data" / "monitor.db",
    ]


def detect_sources(target: Path) -> list[Path]:
    """Existing legacy DB files, excluding anything that IS the target."""
    sources = []
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target
    for cand in legacy_candidates():
        if not cand.is_file():
            continue
        try:
            if cand.resolve() == resolved_target:
                continue
        except OSError:
            pass
        sources.append(cand)
    return sources


# ---------------------------------------------------------------------------
# Schema introspection (runtime — never hardcode columns)
# ---------------------------------------------------------------------------


def _qident(name: str) -> str:
    """Quote an SQL identifier (table/column name) safely."""
    return '"' + name.replace('"', '""') + '"'


def _user_tables(conn: sqlite3.Connection) -> dict[str, str]:
    """Map of user table name -> CREATE statement."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {name: sql for name, sql in rows if sql}


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, int]]:
    """List of (name, decl_type, pk_position) for *table*."""
    rows = conn.execute(f"PRAGMA table_info({_qident(table)})").fetchall()
    return [(r[1], (r[2] or ""), r[5]) for r in rows]


def _merge_columns(cols: list[tuple[str, str, int]]) -> list[str]:
    """Column names to merge/dedupe over, excluding synthetic rowid aliases.

    A single-column INTEGER PRIMARY KEY is a rowid alias (e.g. the
    autoincrement ``id`` every ledger table carries) — synthetic, not part
    of the natural key, and guaranteed to collide between databases.
    Composite or non-INTEGER primary keys are real natural keys and are
    kept.
    """
    pk_cols = [c for c in cols if c[2] > 0]
    skip: set[str] = set()
    if len(pk_cols) == 1 and pk_cols[0][1].upper() == "INTEGER":
        skip.add(pk_cols[0][0])
    return [c[0] for c in cols if c[0] not in skip]


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


class SchemaMismatch(RuntimeError):
    """A source table holds rows but shares no columns with the target.

    Raised BEFORE any mutation so the source is neither merged nor
    archived — surfaced for explicit review, never silently dropped
    (and never hidden behind an archive rename).
    """


def merge_source(
    src: Path,
    target: Path,
    *,
    apply: bool,
    planned: dict[tuple[str, tuple[str, ...]], set[tuple]] | None = None,
) -> dict[str, dict[str, int]]:
    """Merge every user table from *src* into *target*.

    Returns per-table counts: {table: {rows, insert, dup, shared_cols}}.
    In dry-run mode the target is opened read-only when it exists (or
    treated as empty when it does not) and nothing is written.

    *planned* is a cross-source registry of rows already merged (or, in
    dry-run, already planned) by earlier sources in the same invocation,
    keyed by ``(table, shared-column-tuple)``. It keeps dry-run counts
    identical to what ``--apply`` would do when several legacy DBs carry
    overlapping rows. Pass the same dict for every source.

    Raises :class:`SchemaMismatch` (before any write) if a source table
    has rows but zero columns in common with the existing target table.
    """
    stats: dict[str, dict[str, int]] = {}
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    tgt_conn: sqlite3.Connection | None = None
    if planned is None:
        planned = {}
    try:
        if apply:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            tgt_conn = sqlite3.connect(str(target))
        elif target.is_file():
            tgt_conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)

        src_tables = _user_tables(src_conn)
        tgt_tables = _user_tables(tgt_conn) if tgt_conn is not None else {}

        # Pass 1 — plan + validate. No mutation happens here, so a schema
        # mismatch on ANY table aborts the whole source untouched.
        plan: list[tuple[str, str, list[str]]] = []
        for table, create_sql in sorted(src_tables.items()):
            src_cols = _merge_columns(_columns(src_conn, table))
            if table in tgt_tables:
                tgt_cols = _merge_columns(_columns(tgt_conn, table))
                shared = [c for c in src_cols if c in tgt_cols]
            else:
                shared = src_cols
            if not shared:
                n_rows = src_conn.execute(
                    f"SELECT COUNT(*) FROM {_qident(table)}"
                ).fetchone()[0]
                if n_rows:
                    raise SchemaMismatch(
                        f"table {table}: {n_rows} rows but 0 columns shared "
                        "with the target schema — source left untouched; "
                        "resolve the schema drift before migrating"
                    )
            plan.append((table, create_sql, shared))

        # Pass 2 — merge.
        for table, create_sql, shared in plan:
            counts = {"rows": 0, "insert": 0, "dup": 0, "shared_cols": len(shared)}
            stats[table] = counts
            target_has_table = table in tgt_tables
            if not target_has_table and apply:
                # Table missing from target: create it from the legacy DDL
                # so no legacy data is silently dropped.
                tgt_conn.execute(create_sql)
            if not shared:
                continue

            col_list = ", ".join(_qident(c) for c in shared)
            where = " AND ".join(f"{_qident(c)} IS ?" for c in shared)
            placeholders = ", ".join("?" for _ in shared)
            exists_sql = f"SELECT 1 FROM {_qident(table)} WHERE {where} LIMIT 1"
            insert_sql = (
                f"INSERT INTO {_qident(table)} ({col_list}) VALUES ({placeholders})"
            )

            cross = planned.setdefault((table, tuple(shared)), set())
            seen: set[tuple] = set()  # intra-source dupes (dry-run + apply)
            for row in src_conn.execute(f"SELECT {col_list} FROM {_qident(table)}"):
                counts["rows"] += 1
                duplicate = False
                if row in seen or row in cross:
                    duplicate = True
                elif target_has_table and tgt_conn is not None:
                    duplicate = (
                        tgt_conn.execute(exists_sql, row).fetchone() is not None
                    )
                seen.add(row)
                if duplicate:
                    counts["dup"] += 1
                    continue
                counts["insert"] += 1
                cross.add(row)
                if apply:
                    tgt_conn.execute(insert_sql, row)

        if apply and tgt_conn is not None:
            tgt_conn.commit()
    finally:
        src_conn.close()
        if tgt_conn is not None:
            tgt_conn.close()
    return stats


def archive_source(src: Path, *, date_tag: str) -> Path:
    """Rename *src* (and any WAL/SHM sidecars) to ``<name>.legacy-<date>``.

    Never deletes. If the archive name is already taken, a numeric suffix
    is appended rather than overwriting.
    """
    base = src.with_name(f"{src.name}.legacy-{date_tag}")
    dest = base
    n = 1
    while dest.exists():
        dest = base.with_name(f"{base.name}-{n}")
        n += 1
    src.rename(dest)
    for ext in ("-wal", "-shm"):
        sidecar = src.with_name(src.name + ext)
        if sidecar.exists():
            sidecar.rename(dest.with_name(dest.name + ext))
    return dest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_monitor_db.py",
        description=(
            "Merge legacy monitor.db files into the canonical location and "
            "archive the originals (rename, never delete). Dry-run by default."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually merge + archive (default is a dry-run report)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit dry-run (the default; kept for script compatibility)",
    )
    args = parser.parse_args(argv)
    apply = bool(args.apply) and not args.dry_run

    target = resolve_target()
    sources = detect_sources(target)
    mode = "APPLY" if apply else "DRY-RUN (pass --apply to mutate)"
    print(f"monitor.db migration — {mode}")
    print(f"  canonical target: {target}")

    if not sources:
        print("  no legacy monitor.db files found — nothing to do.")
        return 0

    date_tag = _dt.date.today().isoformat()
    exit_code = 0
    planned: dict[tuple[str, tuple[str, ...]], set[tuple]] = {}
    for src in sources:
        print(f"\n  legacy source: {src}")
        try:
            stats = merge_source(src, target, apply=apply, planned=planned)
        except SchemaMismatch as exc:
            print(f"    ERROR: schema mismatch — {exc} — skipped, NOT archived")
            exit_code = 1
            continue
        except sqlite3.Error as exc:
            print(f"    ERROR: merge failed ({exc}) — skipped, NOT archived")
            exit_code = 1
            continue
        for table, c in stats.items():
            print(
                f"    table {table}: {c['rows']} rows -> "
                f"{c['insert']} merged, {c['dup']} duplicates skipped "
                f"({c['shared_cols']} shared columns)"
            )
        if not stats:
            print("    no user tables found")
        if apply:
            try:
                dest = archive_source(src, date_tag=date_tag)
                print(f"    archived: renamed to {dest}")
            except OSError as exc:
                print(
                    f"    ERROR: merge committed but archive rename failed ({exc}). "
                    "Re-running is safe (rows dedupe)."
                )
                exit_code = 1
        else:
            print(f"    would archive: rename to {src.name}.legacy-{date_tag}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
