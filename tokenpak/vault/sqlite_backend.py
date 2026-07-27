"""
TokenPak — SQLite Retrieval Backend for proxy
=================================================

Provides a drop-in alternative to the in-memory JSON/blocks BM25 backend.
Stores block content and precomputed BM25 stats in SQLite for faster
load times and incremental updates at larger index scales.

Usage (via env var in proxy.py)::

    TOKENPAK_RETRIEVAL_BACKEND=sqlite  # or json_blocks (default)

The SQLite DB is stored alongside the vault index at::

    <TOKENPAK_VAULT_INDEX>/retrieval.db

Incremental update strategy:
- On each reload check, compare index.json mtime vs db checkpoint mtime.
- If index.json is newer, rebuild only changed/new/deleted blocks.
- Checkpoint mtime is stored in the ``meta`` table.

Schema::

    blocks(block_id TEXT PK, source_path TEXT, risk_class TEXT,
           must_keep INT, raw_tokens INT, content TEXT,
           term_count INT)

    block_terms(block_id TEXT, term TEXT, tf INT)
               INDEX(term) for IDF lookups

    block_projects(block_id TEXT, project_id TEXT)
               PK(block_id, project_id), INDEX(project_id)
               Project membership. A join table rather than a column because a
               shared resource genuinely belongs to several projects; ``*`` is
               the sentinel for "visible to every scope".

    doc_stats(id INT PK, doc_count INT, total_dl INT, avg_dl REAL)

    meta(key TEXT PK, value TEXT)

Project scoping:
    When a scope is resolved, membership is applied as a **pre-filter** inside
    the scoring SQL — wrong-project blocks are never scored, so they cannot
    outrank the right ones on term overlap. IDF stays corpus-wide (within-source
    normalization) so a term does not look artificially rare inside a small
    project. See :mod:`tokenpak.vault.project_scope`.
"""

from __future__ import annotations

__all__ = ("SQLiteRetrievalBackend", "ScopedSearchResult")


import hashlib
import json
import logging
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, TypedDict, cast

from tokenpak.vault.project_scope import (
    DEFAULT_EXCLUDED_ROLES,
    SHARED,
    AmbiguityPolicy,
    ProjectRegistry,
    ScopeConflictError,
    ScopeResolution,
)


class VaultBlock(TypedDict):
    block_id: str
    source_path: str
    risk_class: str
    must_keep: bool
    raw_tokens: int
    content: str


@dataclass
class ScopedSearchResult:
    """Search results plus the scope decision that produced them.

    ``suppressed`` is the fail-closed outcome: scope could not be resolved and
    the candidate set spanned more than one project, so nothing is returned
    rather than a silent blend. ``spanned`` names what was in contention, which
    is what a caller needs to ask a useful follow-up question.
    """

    results: List[Tuple[VaultBlock, float]] = field(default_factory=list)
    scope: Optional[ScopeResolution] = None
    spanned: Tuple[str, ...] = ()
    suppressed: bool = False

    @property
    def project_id(self) -> Optional[str]:
        return self.scope.project_id if self.scope else None


# ---------------------------------------------------------------------------
# BM25 tokenizer (mirrors proxy._bm25_tokenize)
# ---------------------------------------------------------------------------


def _bm25_tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


def _load_registry() -> Tuple[ProjectRegistry, Optional[str]]:
    """Load the declared project registry.

    Returns ``(registry, error)``. A *missing* ``vault.yaml`` is not an error —
    it means no scoping was ever requested, which is the pre-existing behaviour.
    A *present but broken* declaration is different: it fires precisely when a
    user is trying to turn scoping on, and silently becoming "no scoping" would
    fail open into the cross-project blend this module exists to prevent.

    So a load failure is reported, not swallowed. The caller keeps any
    last-known-good membership and refuses to answer explicitly-scoped queries
    rather than answering them wrongly.
    """
    try:
        from tokenpak.vault import config as vault_config

        return vault_config.load().registry(), None
    except Exception as e:  # noqa: BLE001 — config/IO/validation all degrade alike
        detail = f"{type(e).__name__}: {e}"
        logging.error(
            "Vault project registry failed to load (%s); scoped queries will be "
            "refused and existing membership left intact",
            detail,
        )
        return ProjectRegistry(), detail


# ---------------------------------------------------------------------------
# SQLiteRetrievalBackend
# ---------------------------------------------------------------------------


class SQLiteRetrievalBackend:
    """SQLite-backed BM25 retrieval for proxy vault injection.

    Implements the same public interface as ``VaultIndex``:
      - ``available: bool``
      - ``maybe_reload()``
      - ``search(query, top_k, min_score) -> [(block, score)]``
      - ``compile_injection(query, budget, top_k, min_score) -> (text, tokens, refs)``

    Block count and token count are exposed for metrics parity.
    """

    DB_VERSION = 1

    def __init__(self, tokenpak_dir: str, registry: Optional[ProjectRegistry] = None):
        self.tokenpak_dir = Path(tokenpak_dir)
        self.db_path = self.tokenpak_dir / "retrieval.db"
        self._lock = threading.Lock()
        self._last_checked = 0.0
        self._block_count = 0
        self._token_count = 0
        self._check_interval = 60  # seconds between mtime checks
        self._initialized = False
        if registry is not None:
            self._registry, self._registry_error = registry, None
        else:
            self._registry, self._registry_error = _load_registry()
        self._synced_signature: Optional[str] = None

    # ------------------------------------------------------------------
    # Project scope
    # ------------------------------------------------------------------

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    def set_registry(self, registry: ProjectRegistry) -> None:
        """Replace the project registry and re-resolve membership on next check.

        Editing ``projects:`` in ``vault.yaml`` changes what each block belongs
        to without touching ``index.json``, so membership cannot be keyed off
        the index checkpoint alone.
        """
        self._registry = registry
        self._registry_error = None
        self._synced_signature = None
        self._last_checked = 0.0

    def _registry_signature(self) -> str:
        """Stable fingerprint of the declared roots and their membership."""
        parts = [
            f"{r.path}\x1f{r.role}\x1f{','.join(r.projects)}" for r in self._registry.roots
        ]
        return hashlib.sha256("\x1e".join(parts).encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------
    # Public interface (mirrors VaultIndex)
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._initialized and self._block_count > 0

    @property
    def block_count(self) -> int:
        return self._block_count

    @property
    def token_count(self) -> int:
        return self._token_count

    def maybe_reload(self) -> None:
        """Check if the vault index has changed and rebuild if necessary."""
        now = time.time()
        if now - self._last_checked < self._check_interval:
            return
        self._last_checked = now

        index_path = self.tokenpak_dir / "index.json"
        if not index_path.exists():
            return

        try:
            index_mtime = index_path.stat().st_mtime
        except OSError:
            return

        db_checkpoint = self._get_checkpoint()
        if not self._initialized or index_mtime > db_checkpoint:
            self._rebuild_incremental(index_path, index_mtime)

        # Membership can go stale without index.json moving — editing
        # ``projects:`` re-partitions blocks that themselves never changed.
        self._sync_memberships_if_stale()

    def _sync_memberships_if_stale(self) -> None:
        """Recompute block→project membership when the registry has changed.

        Reload runs on a timer, so the steady state — signature unchanged — must
        cost nothing. The in-memory guard short-circuits before any connection
        is opened; the persisted signature is only consulted once per process.
        """
        if self._registry_error is not None:
            # Keep the last known-good membership. Re-resolving against an
            # empty registry would delete it, destroying the only correct data
            # we still have on the basis of a config we could not read.
            return
        signature = self._registry_signature()
        if self._synced_signature == signature:
            return
        if not self.db_path.exists():
            return
        try:
            conn = self._connect()
        except sqlite3.Error:
            return
        with self._lock:
            try:
                self._ensure_schema(conn)
                row = conn.execute(
                    "SELECT value FROM meta WHERE key='scope_signature'"
                ).fetchone()
                if row and str(row[0]) == signature:
                    self._synced_signature = signature
                    return
                self._resolve_memberships(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('scope_signature', ?)",
                    (signature,),
                )
                conn.commit()
                self._synced_signature = signature
            except sqlite3.Error as e:
                print(f"  ⚠️ SQLite backend: scope sync error: {e}")
                conn.rollback()
            finally:
                conn.close()

    def _resolve_memberships(self, conn: sqlite3.Connection) -> None:
        """Rebuild ``block_projects`` and ``blocks.scope_role`` from the registry.

        Path→project resolution reads no content, so a full re-resolve is cheap
        enough to prefer over trying to diff which roots moved.
        """
        if not self._registry.roots:
            # Nothing declared. Only clear if something is actually there —
            # otherwise every fresh database would pay a full-table UPDATE for
            # a registry that does not exist, which is the overwhelmingly
            # common case and is measurable on a large vault.
            if conn.execute("SELECT 1 FROM block_projects LIMIT 1").fetchone():
                conn.execute("DELETE FROM block_projects")
                conn.execute(
                    "UPDATE blocks SET scope_role='unspecified' "
                    "WHERE scope_role != 'unspecified'"
                )
            return

        conn.execute("DELETE FROM block_projects")

        # Read the identity columns fully before writing: updating ``blocks``
        # while a cursor over ``blocks`` is still open is undefined in SQLite.
        # Only ids and paths are held — block content is never loaded — so this
        # stays small even on a large vault, and a sync only runs when the
        # registry actually changed.
        rows = conn.execute("SELECT block_id, source_path FROM blocks").fetchall()

        batch = 1000
        memberships: list[Tuple[str, str]] = []
        roles: list[Tuple[str, str]] = []

        def flush() -> None:
            if memberships:
                conn.executemany(
                    "INSERT OR IGNORE INTO block_projects (block_id, project_id) "
                    "VALUES (?, ?)",
                    memberships,
                )
                memberships.clear()
            if roles:
                conn.executemany(
                    "UPDATE blocks SET scope_role=? WHERE block_id=? AND scope_role != ?",
                    [(role, bid, role) for role, bid in roles],
                )
                roles.clear()

        for block_id, source_path in rows:
            membership = self._registry.resolve_path(str(source_path))
            roles.append((membership.role, str(block_id)))
            for pid in membership.project_ids:
                memberships.append((str(block_id), pid))
            if len(roles) >= batch:
                flush()
        flush()

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 2.0,
        *,
        project: Optional[str] = None,
        exclude_roles: Optional[Sequence[str]] = None,
    ) -> List[Tuple[VaultBlock, float]]:
        """BM25 search. Returns [(block_dict, score), ...] sorted deterministically.

        When *project* is given, membership is applied as a pre-filter so blocks
        outside that scope are never scored. Positional arity is unchanged, so
        this stays a drop-in for ``VaultIndex.search``.
        """
        if not self.available:
            return []

        query_terms = list(set(_bm25_tokenize(query)))
        if not query_terms:
            return []

        with self._lock:
            try:
                conn = self._connect()
                return self._bm25_search(
                    conn,
                    query_terms,
                    top_k,
                    min_score,
                    project=project,
                    exclude_roles=exclude_roles,
                )
            except sqlite3.Error as e:
                print(f"  ⚠️ SQLite retrieval search error: {e}")
                return []

    def search_scoped(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 2.0,
        *,
        project: Optional[str] = None,
        cwd: Optional[str] = None,
        exclude_roles: Optional[Sequence[str]] = None,
        on_ambiguous: str = AmbiguityPolicy.SUPPRESS,
    ) -> ScopedSearchResult:
        """Search with project scope resolved, and refuse to blend when it isn't.

        Resolution order is explicit → env → cwd → project named in the query.
        If none of those identify a project *and* the results span more than one,
        the outcome depends on *on_ambiguous*:

        ``suppress`` (default)
            Return nothing. A wrong-project answer is worse than no answer
            because it is indistinguishable from a right one.
        ``dominant``
            Keep only the highest-scoring project's blocks.
        ``unscoped``
            Return the blend unchanged (pre-scoping behaviour).

        A single-project result set is never ambiguous, so an unscoped query
        against a single-project vault behaves exactly as before.
        """
        if self._registry_error is not None:
            # The declaration exists but could not be read. Treating that as
            # "no scoping" would fail open into the blend, at exactly the moment
            # the user is trying to switch scoping on.
            raise ScopeConflictError(
                f"project registry failed to load ({self._registry_error}); "
                "refusing to answer rather than returning unscoped results"
            )

        if not self._registry.active:
            # An explicit scope request must bind or fail. Returning unscoped
            # results here would silently ignore what the caller asked for and
            # manufacture exactly the false confidence this module exists to
            # prevent — a vault.yaml typo would quietly unscope every call.
            if project:
                raise ScopeConflictError(
                    f"project {project!r} requested but no project registry is "
                    "declared; refusing to return unscoped results for an "
                    "explicitly scoped query"
                )
            return ScopedSearchResult(
                results=self.search(query, top_k, min_score, exclude_roles=exclude_roles),
                scope=ScopeResolution(project_id=None, source="no_registry"),
            )

        scope = self._registry.resolve_scope(explicit=project, cwd=cwd, query=query)

        if scope.resolved:
            return ScopedSearchResult(
                results=self.search(
                    query,
                    top_k,
                    min_score,
                    project=scope.project_id,
                    exclude_roles=exclude_roles,
                ),
                scope=scope,
            )

        results = self.search(query, top_k, min_score, exclude_roles=exclude_roles)
        spanned = self._projects_spanned([b["block_id"] for b, _ in results])

        if len(spanned) <= 1:
            return ScopedSearchResult(results=results, scope=scope, spanned=spanned)

        if on_ambiguous == AmbiguityPolicy.UNSCOPED:
            return ScopedSearchResult(results=results, scope=scope, spanned=spanned)

        if on_ambiguous == AmbiguityPolicy.DOMINANT:
            dominant = self._dominant_project(results)
            if dominant is not None:
                kept = self.search(
                    query, top_k, min_score, project=dominant, exclude_roles=exclude_roles
                )
                return ScopedSearchResult(
                    results=kept,
                    scope=ScopeResolution(project_id=dominant, source="dominant"),
                    spanned=spanned,
                )

        return ScopedSearchResult(results=[], scope=scope, spanned=spanned, suppressed=True)

    # ------------------------------------------------------------------
    # Internal — membership lookups
    # ------------------------------------------------------------------

    def _projects_spanned(self, block_ids: Sequence[str]) -> Tuple[str, ...]:
        """Distinct competing projects represented by *block_ids*.

        Shared blocks are excluded wholesale — not merely their ``*`` row. A
        resource visible to every scope is never evidence that two projects are
        in contention, and counting the library that owns it as a competitor
        would both inflate the ambiguity count and let it win a dominance vote
        it should not be standing in.
        """
        if not block_ids:
            return ()
        try:
            conn = self._connect()
        except sqlite3.Error:
            return ()
        try:
            placeholders = ",".join("?" * len(block_ids))
            rows = conn.execute(
                f"SELECT DISTINCT project_id FROM block_projects "
                f"WHERE block_id IN ({placeholders}) AND project_id != ? "
                f"AND block_id NOT IN ("
                f"    SELECT block_id FROM block_projects WHERE project_id = ?)",
                [*block_ids, SHARED, SHARED],
            ).fetchall()
            return tuple(sorted(str(r[0]) for r in rows))
        except sqlite3.Error:
            return ()
        finally:
            conn.close()

    def _dominant_project(
        self, results: Sequence[Tuple[VaultBlock, float]]
    ) -> Optional[str]:
        """The project holding the most scoring mass in *results*."""
        if not results:
            return None
        try:
            conn = self._connect()
        except sqlite3.Error:
            return None
        try:
            mass: Dict[str, float] = {}
            for block, score in results:
                rows = conn.execute(
                    "SELECT project_id FROM block_projects "
                    "WHERE block_id = ? AND project_id != ? "
                    "AND block_id NOT IN ("
                    "    SELECT block_id FROM block_projects WHERE project_id = ?)",
                    (block["block_id"], SHARED, SHARED),
                ).fetchall()
                for row in rows:
                    pid = str(row[0])
                    mass[pid] = mass.get(pid, 0.0) + score
            if not mass:
                return None
            # Ties break on project id so the choice is reproducible.
            return sorted(mass.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def compile_injection(
        self,
        query: str,
        budget: int = 4000,
        top_k: int = 5,
        min_score: float = 2.0,
        *,
        project: Optional[str] = None,
        cwd: Optional[str] = None,
        on_ambiguous: str = AmbiguityPolicy.SUPPRESS,
    ) -> Tuple[str, int, List[str]]:
        """Search and compile injection text within token budget.

        Returns (injection_text, tokens_used, source_refs).
        Mirrors VaultIndex.compile_injection exactly for cache stability.

        This is the automatic path — the caller never sees what was injected, so
        an unresolvable multi-project match returns nothing rather than a blend.
        With no project registry declared the behaviour is unchanged.
        """
        try:
            scoped = self.search_scoped(
                query,
                top_k=top_k,
                min_score=min_score,
                project=project,
                cwd=cwd,
                on_ambiguous=on_ambiguous,
            )
        except ScopeConflictError as exc:
            # This is the automatic path: a bad scope declaration (e.g. a typo in
            # $TOKENPAK_PROJECT) must not raise through the request pipeline and
            # take down every proxied call. Injection is an enhancement — the
            # safe failure is to contribute nothing, loudly.
            print(f"  ⚠️ Vault injection skipped: unusable project scope ({exc})")
            return "", 0, []
        if scoped.suppressed:
            print(
                "  ⚠️ Vault injection suppressed: query matched "
                f"{len(scoped.spanned)} projects ({', '.join(scoped.spanned)}) "
                "and no scope was resolved"
            )
            return "", 0, []
        results = scoped.results
        if not results:
            return "", 0, []

        # Import count_tokens lazily to avoid circular imports
        try:
            from tokenpak.telemetry.tokens import count_tokens as _count_tokens

            count_tokens_fn = _count_tokens
        except ImportError:

            def count_tokens_fn(t: str) -> int:  # type: ignore[misc]
                return max(1, len(t) // 4)

        injection_parts: List[str] = []
        tokens_used = 0
        source_refs: List[str] = []

        for block, score in results:
            content = block["content"]
            block_tokens = block.get("raw_tokens", count_tokens_fn(content))

            remaining = budget - tokens_used
            if remaining <= 100:
                break

            if block_tokens > remaining:
                char_limit = remaining * 4
                content = content[:char_limit].rsplit("\n", 1)[0]
                block_tokens = count_tokens_fn(content)

            source_path = block["source_path"]
            injection_parts.append(f"--- [{source_path}] (relevance: {score:.1f}) ---\n{content}")
            tokens_used += block_tokens
            source_refs.append(source_path)

        if not injection_parts:
            return "", 0, []

        header = "\n\n## Retrieved Context\n"
        injection_text = header + "\n\n".join(injection_parts)
        tokens_used = count_tokens_fn(injection_text)

        return injection_text, tokens_used, source_refs

    # ------------------------------------------------------------------
    # Internal — DB init + schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-16000")  # 16 MB page cache
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                block_id    TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                risk_class  TEXT DEFAULT 'narrative',
                must_keep   INTEGER DEFAULT 0,
                raw_tokens  INTEGER DEFAULT 0,
                content     TEXT NOT NULL DEFAULT '',
                term_count  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS block_terms (
                block_id TEXT NOT NULL,
                term     TEXT NOT NULL,
                tf       INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (block_id, term)
            );
            CREATE INDEX IF NOT EXISTS idx_block_terms_term ON block_terms(term);

            CREATE TABLE IF NOT EXISTS block_projects (
                block_id   TEXT NOT NULL,
                project_id TEXT NOT NULL,
                PRIMARY KEY (block_id, project_id)
            );
            CREATE INDEX IF NOT EXISTS idx_block_projects_pid
                ON block_projects(project_id);

            CREATE TABLE IF NOT EXISTS doc_stats (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                doc_count  INTEGER NOT NULL DEFAULT 0,
                total_dl   INTEGER NOT NULL DEFAULT 0,
                avg_dl     REAL NOT NULL DEFAULT 0.0
            );
            INSERT OR IGNORE INTO doc_stats (id, doc_count, total_dl, avg_dl)
            VALUES (1, 0, 0, 0.0);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._ensure_columns(conn)
        conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a DB was first created.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so new
        columns need an explicit idempotent ALTER rather than riding the
        schema script.
        """
        existing = {row[1] for row in conn.execute("PRAGMA table_info(blocks)").fetchall()}
        if "scope_role" not in existing:
            conn.execute(
                "ALTER TABLE blocks ADD COLUMN scope_role TEXT NOT NULL DEFAULT 'unspecified'"
            )

    # ------------------------------------------------------------------
    # Internal — checkpoint management
    # ------------------------------------------------------------------

    def _get_checkpoint(self) -> float:
        """Return the mtime of the last successful build, or 0.0."""
        if not self.db_path.exists():
            return 0.0
        try:
            conn = self._connect()
            self._ensure_schema(conn)
            row = conn.execute("SELECT value FROM meta WHERE key='index_mtime'").fetchone()
            conn.close()
            return float(row[0]) if row else 0.0
        except sqlite3.Error:
            return 0.0

    def _set_checkpoint(self, conn: sqlite3.Connection, mtime: float) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('index_mtime', ?)",
            (str(mtime),),
        )

    # ------------------------------------------------------------------
    # Internal — incremental rebuild
    # ------------------------------------------------------------------

    def _rebuild_incremental(self, index_path: Path, mtime: float) -> None:
        """Load index.json, diff against DB, upsert changed blocks, prune deleted."""
        try:
            data = json.loads(index_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠️ SQLite backend: index read error: {e}")
            return

        raw_blocks = data.get("blocks", {})
        if not isinstance(raw_blocks, dict):
            return
        typed_blocks = cast(dict[object, object], raw_blocks)

        blocks_dir = self.tokenpak_dir / "blocks"
        conn = self._connect()

        with self._lock:
            try:
                self._ensure_schema(conn)

                # Existing block_ids in DB
                existing_ids = {
                    r[0] for r in conn.execute("SELECT block_id FROM blocks").fetchall()
                }
                new_ids = {str(block_id) for block_id in typed_blocks}

                # Blocks to delete (removed from index)
                deleted = existing_ids - new_ids
                if deleted:
                    placeholders = ",".join("?" * len(deleted))
                    conn.execute(
                        f"DELETE FROM blocks WHERE block_id IN ({placeholders})",
                        list(deleted),
                    )
                    conn.execute(
                        f"DELETE FROM block_terms WHERE block_id IN ({placeholders})",
                        list(deleted),
                    )
                    conn.execute(
                        f"DELETE FROM block_projects WHERE block_id IN ({placeholders})",
                        list(deleted),
                    )

                # Blocks to upsert
                added = 0
                for raw_bid, raw_bdata in typed_blocks.items():
                    bid = str(raw_bid)
                    if not isinstance(raw_bdata, dict):
                        continue
                    bdata = cast(dict[str, object], raw_bdata)
                    content_file = blocks_dir / f"{bid}.txt"
                    try:
                        content = (
                            content_file.read_text(errors="replace")
                            if content_file.exists()
                            else ""
                        )
                    except OSError:
                        content = ""

                    terms = _bm25_tokenize(content)
                    tf: Dict[str, int] = {}
                    for t in terms:
                        tf[t] = tf.get(t, 0) + 1

                    conn.execute(
                        """INSERT OR REPLACE INTO blocks
                           (block_id, source_path, risk_class, must_keep, raw_tokens, content, term_count)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            bid,
                            bdata.get("source_path", bid),
                            bdata.get("risk_class", "narrative"),
                            int(bool(bdata.get("must_keep", False))),
                            bdata.get("raw_tokens", 0),
                            content,
                            len(terms),
                        ),
                    )
                    # Replace term frequencies
                    conn.execute("DELETE FROM block_terms WHERE block_id=?", (bid,))
                    if tf:
                        conn.executemany(
                            "INSERT INTO block_terms (block_id, term, tf) VALUES (?, ?, ?)",
                            [(bid, term, freq) for term, freq in tf.items()],
                        )
                    added += 1

                # Update global stats
                doc_count = len(new_ids)
                total_dl_row = conn.execute(
                    "SELECT COALESCE(SUM(term_count), 0) FROM blocks"
                ).fetchone()
                total_dl = total_dl_row[0] if total_dl_row else 0
                avg_dl = total_dl / doc_count if doc_count > 0 else 0.0

                conn.execute(
                    """INSERT OR REPLACE INTO doc_stats (id, doc_count, total_dl, avg_dl)
                       VALUES (1, ?, ?, ?)""",
                    (doc_count, total_dl, avg_dl),
                )
                self._set_checkpoint(conn, mtime)
                # New/changed blocks have no membership yet. Invalidating the
                # signature makes the sync pass that follows re-resolve them
                # instead of leaving fresh blocks unscoped (and so invisible to
                # every scoped query).
                conn.execute("DELETE FROM meta WHERE key='scope_signature'")
                conn.commit()
                # Clear the in-memory guard too, or the short-circuit would skip
                # the very sync this invalidation exists to trigger.
                self._synced_signature = None

                # Update cached counters
                self._block_count = doc_count
                token_row = conn.execute(
                    "SELECT COALESCE(SUM(raw_tokens), 0) FROM blocks"
                ).fetchone()
                self._token_count = token_row[0] if token_row else 0
                self._initialized = True

                print(
                    f"  📚 SQLite vault backend: {doc_count} blocks "
                    f"({added} upserted, {len(deleted)} removed), "
                    f"{self._token_count:,} tokens"
                )
            except sqlite3.Error as e:
                print(f"  ⚠️ SQLite backend rebuild error: {e}")
                conn.rollback()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Internal — BM25 search
    # ------------------------------------------------------------------

    def _scope_predicate(
        self,
        project: Optional[str],
        exclude_roles: Optional[Sequence[str]],
    ) -> Tuple[str, List[str]]:
        """Build the scope SQL fragment and its bind parameters.

        Uses ``EXISTS`` rather than a join: a block that is both a member of
        *project* and shared would match twice under a join and have its BM25
        score double-counted.

        Role exclusion (archived copies by default) applies only when a registry
        is declared — without one every block is ``unspecified`` and nothing
        should change.
        """
        clauses: List[str] = []
        params: List[str] = []

        if project:
            clauses.append(
                " AND EXISTS (SELECT 1 FROM block_projects bp "
                "WHERE bp.block_id = b.block_id AND bp.project_id IN (?, ?))"
            )
            params.extend([project, SHARED])

        roles = (
            list(exclude_roles)
            if exclude_roles is not None
            else (sorted(DEFAULT_EXCLUDED_ROLES) if self._registry.active else [])
        )
        if roles:
            placeholders = ",".join("?" * len(roles))
            clauses.append(f" AND b.scope_role NOT IN ({placeholders})")
            params.extend(roles)

        return "".join(clauses), params

    def _bm25_search(
        self,
        conn: sqlite3.Connection,
        query_terms: List[str],
        top_k: int,
        min_score: float,
        *,
        project: Optional[str] = None,
        exclude_roles: Optional[Sequence[str]] = None,
    ) -> List[Tuple[VaultBlock, float]]:
        """Execute BM25 retrieval via SQL aggregation.

        The scope predicate is applied here rather than as a post-filter so that
        out-of-scope blocks never enter the ranking at all — filtering after
        ``top_k`` would let a wrong-project block consume a slot and return
        fewer right-project results than asked for.
        """
        k1 = 1.5
        b_param = 0.75

        # Load global stats
        # IDF stays corpus-wide even under a scope filter: recomputing it over
        # the surviving subset would make a common term look rare inside a small
        # project and distort scores between scopes.
        stats_row = conn.execute("SELECT doc_count, avg_dl FROM doc_stats WHERE id=1").fetchone()
        if not stats_row or stats_row[0] == 0:
            return []
        doc_count, avg_dl = stats_row

        if avg_dl == 0:
            return []

        # Per-term DF lookup
        placeholders = ",".join("?" * len(query_terms))
        df_rows = conn.execute(
            f"SELECT term, COUNT(DISTINCT block_id) FROM block_terms "
            f"WHERE term IN ({placeholders}) GROUP BY term",
            query_terms,
        ).fetchall()
        df: Dict[str, int] = {r[0]: r[1] for r in df_rows}

        # Filter to terms that actually appear
        active_terms = [t for t in query_terms if t in df]
        if not active_terms:
            return []

        # Phase 1: Score-only pass — no content fetch (fast)
        placeholders2 = ",".join("?" * len(active_terms))
        scope_sql, scope_params = self._scope_predicate(project, exclude_roles)
        tf_rows = conn.execute(
            f"""SELECT bt.block_id, bt.term, bt.tf, b.term_count, b.source_path
                FROM block_terms bt
                JOIN blocks b ON bt.block_id = b.block_id
                WHERE bt.term IN ({placeholders2}){scope_sql}""",
            [*active_terms, *scope_params],
        ).fetchall()

        # Aggregate per-block scores (no content loaded yet)
        block_scores: Dict[str, float] = {}
        block_source: Dict[str, str] = {}

        for row in tf_rows:
            bid, term, tf_val, dl, source_path = row
            df_val = df.get(term, 0)
            if df_val == 0:
                continue

            idf = math.log((doc_count - df_val + 0.5) / (df_val + 0.5) + 1)
            norm_dl = dl if dl > 0 else avg_dl
            numerator = tf_val * (k1 + 1)
            denominator = tf_val + k1 * (1 - b_param + b_param * norm_dl / avg_dl)
            term_score = idf * numerator / denominator

            block_scores[bid] = block_scores.get(bid, 0.0) + term_score
            block_source.setdefault(bid, source_path)

        # Filter by min_score, sort deterministically (score desc, path asc, id asc)
        ranked = sorted(
            ((bid, score) for bid, score in block_scores.items() if score >= min_score),
            key=lambda x: (
                -x[1],
                block_source.get(x[0], ""),
                x[0],
            ),
        )[:top_k]

        if not ranked:
            return []

        # Phase 2: Fetch full block data only for top_k results
        top_ids = [bid for bid, _ in ranked]
        top_placeholders = ",".join("?" * len(top_ids))
        detail_rows = conn.execute(
            f"""SELECT block_id, source_path, risk_class, must_keep, raw_tokens, content
                FROM blocks WHERE block_id IN ({top_placeholders})""",
            top_ids,
        ).fetchall()
        detail_map: Dict[str, VaultBlock] = {
            str(r[0]): {
                "block_id": str(r[0]),
                "source_path": str(r[1]),
                "risk_class": str(r[2]),
                "must_keep": bool(r[3]),
                "raw_tokens": int(r[4]),
                "content": str(r[5]),
            }
            for r in detail_rows
        }

        return [(detail_map[bid], score) for bid, score in ranked if bid in detail_map]

    # ------------------------------------------------------------------
    # Compatibility — blocks dict (used by debug endpoints / metrics)
    # ------------------------------------------------------------------

    @property
    def blocks(self) -> Dict[str, VaultBlock]:
        """Return all blocks as a dict for compatibility with VaultIndex callers.

        NOTE: This loads all block content from SQLite — use for debug/metrics
        endpoints only, not hot-path retrieval.
        """
        if not self.db_path.exists():
            return {}
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT block_id, source_path, risk_class, must_keep, raw_tokens, content "
                "FROM blocks"
            ).fetchall()
            conn.close()
            return {
                str(r["block_id"]): {
                    "block_id": str(r["block_id"]),
                    "source_path": str(r["source_path"]),
                    "risk_class": str(r["risk_class"]),
                    "must_keep": bool(r["must_keep"]),
                    "raw_tokens": int(r["raw_tokens"]),
                    "content": str(r["content"]),
                }
                for r in rows
            }
        except sqlite3.Error:
            return {}
