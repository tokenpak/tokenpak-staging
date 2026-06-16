# SPDX-License-Identifier: Apache-2.0
"""Legacy monitor.db migration matrix — scripts/migrate_monitor_db.py.

Covers the detect -> merge -> archive contract:

  * three legacy input states: ``~/tokenpak/`` only, ``~/.tokenpak/`` only,
    both at once (with overlapping rows that must dedupe);
  * dry-run by default — zero filesystem mutation, counts identical to
    what ``--apply`` would do;
  * archive by RENAME to ``.legacy-<date>`` — never delete, content
    preserved byte-for-byte;
  * idempotency — a second run is a no-op;
  * natural-key dedupe that ignores the synthetic autoincrement ``id``;
  * fail-loud schema mismatch — no merge, no archive, non-zero exit.

All paths live under a monkeypatched ``HOME``; no real user data is read
or written.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "migrate_monitor_db.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_monitor_db", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mig():
    return _load_script()


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir and clear every env override the resolver honors."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("TOKENPAK_DB", "TOKENPAK_MONITOR_DB", "TOKENPAK_HOME"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# Fixture DB helpers
# ---------------------------------------------------------------------------

ROWS_A = [
    ("2026-01-01T00:00:00", "sess-1", "model-a", 0.01),
    ("2026-01-02T00:00:00", "sess-2", "model-b", 0.02),
]
ROWS_B = [
    ("2026-01-02T00:00:00", "sess-2", "model-b", 0.02),  # overlaps ROWS_A[1]
    ("2026-01-03T00:00:00", "sess-3", "model-c", 0.03),
]


def make_legacy_db(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT, session_id TEXT, model TEXT, cost REAL)"
    )
    conn.executemany(
        "INSERT INTO requests (ts, session_id, model, cost) VALUES (?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def natural_rows(path: Path):
    conn = sqlite3.connect(str(path))
    try:
        return sorted(
            conn.execute("SELECT ts, session_id, model, cost FROM requests")
        )
    finally:
        conn.close()


def canonical_db(home: Path) -> Path:
    return home / ".tpk" / "monitor.db"


def archives_of(src: Path):
    return sorted(src.parent.glob(src.name + ".legacy-*"))


# ---------------------------------------------------------------------------
# Dry-run default
# ---------------------------------------------------------------------------


def test_dry_run_is_default_and_mutates_nothing(mig, tmp_path, capsys):
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)
    before = src.read_bytes()

    rc = mig.main([])

    assert rc == 0
    assert src.exists() and src.read_bytes() == before
    assert not canonical_db(tmp_path).exists()
    assert not archives_of(src)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "2 rows -> 2 merged" in out


def test_explicit_dry_run_flag_overrides_apply(mig, tmp_path):
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)

    rc = mig.main(["--apply", "--dry-run"])

    assert rc == 0
    assert src.exists()
    assert not canonical_db(tmp_path).exists()


# ---------------------------------------------------------------------------
# Migration matrix — apply
# ---------------------------------------------------------------------------


def test_apply_only_old_tokenpak(mig, tmp_path):
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)
    original = natural_rows(src)

    rc = mig.main(["--apply"])

    assert rc == 0
    target = canonical_db(tmp_path)
    assert natural_rows(target) == original
    assert not src.exists()  # renamed, not deleted
    archived = archives_of(src)
    assert len(archived) == 1
    assert natural_rows(archived[0]) == original  # content preserved


def test_apply_only_old_dot_tokenpak(mig, tmp_path):
    src = tmp_path / ".tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_B)

    rc = mig.main(["--apply"])

    assert rc == 0
    assert natural_rows(canonical_db(tmp_path)) == sorted(ROWS_B)
    assert not src.exists()
    assert len(archives_of(src)) == 1


def test_apply_dot_tokenpak_data_subdir_is_drained(mig, tmp_path):
    # budget/optimize historically defaulted to ~/.tokenpak/data/monitor.db;
    # the resolver never scans it, so the migration must drain it too.
    src = tmp_path / ".tokenpak" / "data" / "monitor.db"
    make_legacy_db(src, ROWS_A)

    rc = mig.main(["--apply"])

    assert rc == 0
    assert natural_rows(canonical_db(tmp_path)) == sorted(ROWS_A)
    assert not src.exists()
    assert len(archives_of(src)) == 1


def test_apply_both_sources_merges_with_dedupe(mig, tmp_path):
    src1 = tmp_path / "tokenpak" / "monitor.db"
    src2 = tmp_path / ".tokenpak" / "monitor.db"
    make_legacy_db(src1, ROWS_A)
    make_legacy_db(src2, ROWS_B)  # one row overlaps ROWS_A

    rc = mig.main(["--apply"])

    assert rc == 0
    merged = natural_rows(canonical_db(tmp_path))
    assert merged == sorted(set(ROWS_A) | set(ROWS_B))  # union, no double-count
    assert not src1.exists() and not src2.exists()
    assert len(archives_of(src1)) == 1
    assert len(archives_of(src2)) == 1


def test_apply_into_existing_target_dedupes(mig, tmp_path):
    target = canonical_db(tmp_path)
    make_legacy_db(target, ROWS_A)
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_B)

    rc = mig.main(["--apply"])

    assert rc == 0
    assert natural_rows(target) == sorted(set(ROWS_A) | set(ROWS_B))
    # target itself must never be detected as a source / archived
    assert target.exists()


# ---------------------------------------------------------------------------
# Dry-run counts == apply counts (incl. the cross-source overlap case)
# ---------------------------------------------------------------------------


def _merged_counts(out: str) -> list[int]:
    return [
        int(line.split("->")[1].split("merged")[0].strip())
        for line in out.splitlines()
        if "merged" in line
    ]


def test_dry_run_counts_match_apply_when_sources_overlap(mig, tmp_path, capsys):
    make_legacy_db(tmp_path / "tokenpak" / "monitor.db", ROWS_A)
    make_legacy_db(tmp_path / ".tokenpak" / "monitor.db", ROWS_B)

    assert mig.main([]) == 0
    dry_counts = _merged_counts(capsys.readouterr().out)

    assert mig.main(["--apply"]) == 0
    apply_counts = _merged_counts(capsys.readouterr().out)

    assert dry_counts == apply_counts
    assert sum(dry_counts) == len(set(ROWS_A) | set(ROWS_B))


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_second_run_is_noop(mig, tmp_path, capsys):
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)

    assert mig.main(["--apply"]) == 0
    rows_after_first = natural_rows(canonical_db(tmp_path))
    capsys.readouterr()

    assert mig.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out
    assert natural_rows(canonical_db(tmp_path)) == rows_after_first
    assert len(archives_of(src)) == 1  # no second archive appeared


def test_rerun_after_partial_failure_dedupes(mig, tmp_path):
    # Same logical rows already in the target (as after a merge whose
    # archive rename failed) must all be skipped as duplicates.
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)
    make_legacy_db(canonical_db(tmp_path), ROWS_A)

    rc = mig.main(["--apply"])

    assert rc == 0
    assert natural_rows(canonical_db(tmp_path)) == sorted(ROWS_A)


# ---------------------------------------------------------------------------
# Natural-key dedupe ignores the synthetic autoincrement id
# ---------------------------------------------------------------------------


def test_synthetic_id_is_not_part_of_the_natural_key(mig, tmp_path):
    src1 = tmp_path / "tokenpak" / "monitor.db"
    src2 = tmp_path / ".tokenpak" / "monitor.db"
    make_legacy_db(src1, ROWS_A)
    # Same logical rows but shifted autoincrement ids in the second DB.
    src2.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(src2))
    conn.execute(
        "CREATE TABLE requests ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT, session_id TEXT, model TEXT, cost REAL)"
    )
    conn.executemany(
        "INSERT INTO requests (id, ts, session_id, model, cost) VALUES (?,?,?,?,?)",
        [(100 + i, *row) for i, row in enumerate(ROWS_A)],
    )
    conn.commit()
    conn.close()

    rc = mig.main(["--apply"])

    assert rc == 0
    # Identical natural keys despite different ids -> single copy survives.
    assert natural_rows(canonical_db(tmp_path)) == sorted(ROWS_A)


# ---------------------------------------------------------------------------
# Failure modes — never archive what was not merged
# ---------------------------------------------------------------------------


def test_schema_mismatch_aborts_source_without_archiving(mig, tmp_path, capsys):
    target = canonical_db(tmp_path)
    make_legacy_db(target, ROWS_A)
    src = tmp_path / "tokenpak" / "monitor.db"
    src.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, alien_col TEXT)")
    conn.execute("INSERT INTO requests (alien_col) VALUES ('x')")
    conn.commit()
    conn.close()

    rc = mig.main(["--apply"])

    assert rc == 1
    assert src.exists()  # NOT archived
    assert not archives_of(src)
    assert natural_rows(target) == sorted(ROWS_A)  # target untouched
    assert "schema mismatch" in capsys.readouterr().out


def test_unreadable_source_errors_without_archiving(mig, tmp_path, capsys):
    src = tmp_path / "tokenpak" / "monitor.db"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"this is not a sqlite database, just 100+ bytes of junk " * 4)

    rc = mig.main(["--apply"])

    assert rc == 1
    assert src.exists()
    assert not archives_of(src)
    assert "NOT archived" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Target resolution + archive mechanics
# ---------------------------------------------------------------------------


def test_env_override_sets_merge_target(mig, tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere" / "monitor.db"
    monkeypatch.setenv("TOKENPAK_DB", str(custom))
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)

    rc = mig.main(["--apply"])

    assert rc == 0
    assert natural_rows(custom) == sorted(ROWS_A)
    assert not canonical_db(tmp_path).exists()


def test_env_override_pointing_at_legacy_path_excludes_it_from_sources(
    mig, tmp_path, monkeypatch
):
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)
    monkeypatch.setenv("TOKENPAK_DB", str(src))

    rc = mig.main(["--apply"])

    assert rc == 0
    assert src.exists()  # it IS the target; never merged into itself / archived
    assert not archives_of(src)


def test_archive_name_collision_gets_numeric_suffix(mig, tmp_path):
    src = tmp_path / "tokenpak" / "monitor.db"
    make_legacy_db(src, ROWS_A)
    mod = mig
    taken = src.with_name(f"{src.name}.legacy-2026-06-10")
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"pre-existing archive")

    dest = mod.archive_source(src, date_tag="2026-06-10")

    assert dest != taken
    assert dest.name.startswith("monitor.db.legacy-2026-06-10-")
    assert taken.read_bytes() == b"pre-existing archive"  # never overwritten
    assert not src.exists()
