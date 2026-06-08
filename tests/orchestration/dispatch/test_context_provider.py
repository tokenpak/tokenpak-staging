"""Tests for the ContextProvider boundary (Standards Delta v0 §5.9).

Verifies the OSS :class:`LocalContextProvider` against the §5.9 acceptance
criteria:

  * deterministic output given identical inputs (same bundle id + files);
  * gitignore-aware path filtering is respected;
  * per-station size budget is enforced;
  * per-station token budget is enforced;
  * the :class:`PaidContextProvider` stub raises ``NotImplementedError``.

Plus boundary properties: source precedence on path collision, repo-scan
determinism (sorted, no ranking), frontmatter/attachment inclusion, omitted-path
auditing, and Protocol conformance. No LLM and no network are exercised — the
provider is pure-Python over a tmp repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Dispatch ships its deps via the opt-in `dispatch` extra; skip cleanly on slim
# installs rather than erroring at collection time (mirrors the sibling suites).
pytest.importorskip("pydantic")

from tokenpak.orchestration.dispatch.context.local import (
    DEFAULT_STATION_SIZE_BUDGET_BYTES,
    DEFAULT_STATION_TOKEN_BUDGET,
    GitignoreMatcher,
    LocalContextProvider,
    estimate_tokens,
)
from tokenpak.orchestration.dispatch.context.provider import (
    ContextBundle,
    ContextProvider,
    ContextSource,
    PaidContextProvider,
)
from tokenpak.orchestration.dispatch.models.common import (
    ManifestPermissions,
    QualityRequirements,
)
from tokenpak.orchestration.dispatch.models.enums import (
    AutonomyMode,
    ManifestStatus,
)
from tokenpak.orchestration.dispatch.models.manifest import DispatchManifest
from tokenpak.orchestration.dispatch.models.route import RouteStation

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _manifest(manifest_id: str = "manifest_01TEST") -> DispatchManifest:
    return DispatchManifest(
        id=manifest_id,
        job_id="job_01TEST",
        route_id="route.code_task.v1",
        goal="exercise context assembly",
        permissions=ManifestPermissions(autonomy_mode=AutonomyMode.ADVISORY),
        quality_requirements=QualityRequirements(
            test_required=True,
            review_required=True,
            docs_required=False,
            evidence_required=False,
        ),
        status=ManifestStatus.ACTIVE,
    )


def _station(station_id: str = "station_build") -> RouteStation:
    return RouteStation(
        id=station_id,
        required_role="builder",
        output_schema="station_result.v1",
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small repo with a .gitignore and a few files."""

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# project\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("token=xyz\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("noise\n", encoding="utf-8")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.o").write_text("binary-ish\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        "# ignore logs and build output\n*.log\nbuild/\n/secret.txt\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# AC: deterministic output given identical inputs
# ---------------------------------------------------------------------------


def test_deterministic_identical_inputs(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    kwargs = dict(
        explicit_files=["src/a.py", "README.md"],
        repo_scan=["src/*.py"],
        task_frontmatter={"task_id": "P-CONTEXT-01", "status": "in_progress"},
        attached={"note": "hand attached"},
    )
    b1 = provider.build_context(_manifest(), _station(), **kwargs)
    b2 = provider.build_context(_manifest(), _station(), **kwargs)

    assert b1.id == b2.id
    assert b1.model_dump() == b2.model_dump()
    # id is a content hash, not random/time-based.
    assert b1.id.startswith("ctxbundle_")


def test_determinism_independent_of_input_order(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    b1 = provider.build_context(
        _manifest(), _station(), explicit_files=["src/a.py", "README.md"]
    )
    b2 = provider.build_context(
        _manifest(), _station(), explicit_files=["README.md", "src/a.py"]
    )
    # Same set of inputs -> identical deterministic ordering and id.
    assert b1.id == b2.id
    assert [f.path for f in b1.files] == [f.path for f in b2.files]


def test_bundle_links_manifest_and_station(repo: Path) -> None:
    bundle = LocalContextProvider(repo).build_context(
        _manifest("manifest_X"), _station("station_Y"), explicit_files=["src/a.py"]
    )
    assert bundle.manifest_id == "manifest_X"
    assert bundle.station_id == "station_Y"


# ---------------------------------------------------------------------------
# AC: gitignore filter respected
# ---------------------------------------------------------------------------


def test_gitignore_filters_explicit_paths(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    bundle = provider.build_context(
        _manifest(),
        _station(),
        explicit_files=["src/a.py", "debug.log", "secret.txt", "build/out.o"],
    )
    paths = {f.path for f in bundle.files}
    assert paths == {"src/a.py"}
    for ignored in ("debug.log", "secret.txt", "build/out.o"):
        assert ignored in bundle.omitted_paths


def test_gitignore_filters_repo_scan(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    bundle = provider.build_context(
        _manifest(), _station(), repo_scan=["**/*"]
    )
    paths = {f.path for f in bundle.files}
    assert "debug.log" not in paths
    assert "build/out.o" not in paths
    assert "secret.txt" not in paths
    assert "src/a.py" in paths


def test_gitignore_disabled_includes_everything(repo: Path) -> None:
    provider = LocalContextProvider(repo, follow_gitignore=False)
    bundle = provider.build_context(
        _manifest(), _station(), explicit_files=["debug.log", "secret.txt"]
    )
    paths = {f.path for f in bundle.files}
    assert paths == {"debug.log", "secret.txt"}


def test_gitignore_matcher_patterns() -> None:
    m = GitignoreMatcher(
        ["*.log", "build/", "/root_only.txt", "a/b.py", "!keep.log", "**/temp"]
    )
    assert m.is_ignored("x.log")
    assert m.is_ignored("nested/dir/y.log")
    assert m.is_ignored("build")
    assert m.is_ignored("build/sub/out.o")
    assert m.is_ignored("root_only.txt")
    assert not m.is_ignored("sub/root_only.txt")  # anchored to root
    assert m.is_ignored("a/b.py")
    assert not m.is_ignored("z/a/b.py")  # nested-path patterns anchor at root
    assert m.is_ignored("deep/nested/temp")
    # Negation: last matching rule wins (!keep.log re-includes).
    assert not m.is_ignored("keep.log")


# ---------------------------------------------------------------------------
# AC: size budget enforced
# ---------------------------------------------------------------------------


def test_size_budget_enforced(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "small.txt").write_text("y" * 10, encoding="utf-8")
    provider = LocalContextProvider(
        tmp_path, size_budget_bytes=1000, token_budget=10_000_000
    )
    bundle = provider.build_context(
        _manifest(), _station(), explicit_files=["big.txt", "small.txt"]
    )
    paths = {f.path for f in bundle.files}
    assert "big.txt" not in paths  # 5000 bytes > 1000 budget
    assert "small.txt" in paths  # 10 bytes fits
    assert bundle.truncated is True
    assert "big.txt" in bundle.omitted_paths
    assert bundle.total_size_bytes <= bundle.size_budget_bytes


def test_size_budget_zero_omits_all(repo: Path) -> None:
    provider = LocalContextProvider(repo, size_budget_bytes=0)
    bundle = provider.build_context(
        _manifest(), _station(), explicit_files=["src/a.py"]
    )
    assert bundle.files == []
    assert bundle.truncated is True


# ---------------------------------------------------------------------------
# AC: token budget enforced
# ---------------------------------------------------------------------------


def test_token_budget_enforced(tmp_path: Path) -> None:
    # ~4 chars/token: 400 chars -> ~100 tokens; 8 chars -> 2 tokens.
    (tmp_path / "big.txt").write_text("z" * 400, encoding="utf-8")
    (tmp_path / "small.txt").write_text("z" * 8, encoding="utf-8")
    provider = LocalContextProvider(
        tmp_path, size_budget_bytes=10_000_000, token_budget=10
    )
    bundle = provider.build_context(
        _manifest(), _station(), explicit_files=["big.txt", "small.txt"]
    )
    paths = {f.path for f in bundle.files}
    assert "big.txt" not in paths  # ~100 tokens > 10 budget
    assert "small.txt" in paths  # 2 tokens fits
    assert bundle.truncated is True
    assert bundle.total_estimated_tokens <= bundle.token_budget


def test_estimate_tokens_is_deterministic_and_model_free() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    # pure function: repeated calls match
    assert estimate_tokens("hello world") == estimate_tokens("hello world")


# ---------------------------------------------------------------------------
# Source precedence, frontmatter, attachments, repo scan
# ---------------------------------------------------------------------------


def test_source_precedence_on_path_collision(repo: Path) -> None:
    # Same path from explicit + repo_scan -> explicit wins.
    provider = LocalContextProvider(repo)
    bundle = provider.build_context(
        _manifest(),
        _station(),
        explicit_files=["src/a.py"],
        repo_scan=["src/a.py"],
    )
    matches = [f for f in bundle.files if f.path == "src/a.py"]
    assert len(matches) == 1
    assert matches[0].source is ContextSource.EXPLICIT


def test_frontmatter_and_attachments_included(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    bundle = provider.build_context(
        _manifest(),
        _station(),
        task_frontmatter={"b": 2, "a": 1},
        attached={"hint": "remember determinism"},
    )
    by_source = {f.source: f for f in bundle.files}
    assert ContextSource.TASK_FRONTMATTER in by_source
    assert ContextSource.ATTACHED in by_source
    # frontmatter rendered with sorted keys -> deterministic.
    assert by_source[ContextSource.TASK_FRONTMATTER].content == "a: 1\nb: 2"


def test_repo_scan_sorted_no_ranking(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    bundle = provider.build_context(_manifest(), _station(), repo_scan=["src/*.py"])
    scan_paths = [f.path for f in bundle.files if f.source is ContextSource.REPO_SCAN]
    assert scan_paths == sorted(scan_paths)
    assert scan_paths == ["src/a.py", "src/b.py"]


def test_missing_file_recorded_as_omitted(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    bundle = provider.build_context(
        _manifest(), _station(), explicit_files=["does/not/exist.py"]
    )
    assert bundle.files == []
    assert "does/not/exist.py" in bundle.omitted_paths


def test_empty_request_yields_empty_bundle(repo: Path) -> None:
    bundle = LocalContextProvider(repo).build_context(_manifest(), _station())
    assert bundle.files == []
    assert bundle.total_size_bytes == 0
    assert bundle.total_estimated_tokens == 0
    assert bundle.truncated is False


# ---------------------------------------------------------------------------
# Protocol conformance + defaults
# ---------------------------------------------------------------------------


def test_local_provider_satisfies_protocol(repo: Path) -> None:
    provider = LocalContextProvider(repo)
    assert isinstance(provider, ContextProvider)
    # callable with the bare Protocol signature (no extra kwargs)
    bundle = provider.build_context(_manifest(), _station())
    assert isinstance(bundle, ContextBundle)


def test_provider_defaults_inherit_documented_caps(repo: Path) -> None:
    bundle = LocalContextProvider(repo).build_context(_manifest(), _station())
    assert bundle.size_budget_bytes == DEFAULT_STATION_SIZE_BUDGET_BYTES
    assert bundle.token_budget == DEFAULT_STATION_TOKEN_BUDGET


def test_negative_budgets_rejected(repo: Path) -> None:
    with pytest.raises(ValueError):
        LocalContextProvider(repo, token_budget=-1)


# ---------------------------------------------------------------------------
# AC: PaidContextProvider stub raises NotImplementedError
# ---------------------------------------------------------------------------


def test_paid_context_provider_is_stub() -> None:
    with pytest.raises(NotImplementedError):
        PaidContextProvider()


def test_paid_context_provider_message_cites_boundary() -> None:
    with pytest.raises(NotImplementedError) as exc:
        PaidContextProvider(object(), repo_root="x")
    msg = str(exc.value)
    assert "tokenpak-paid" in msg
    assert "LocalContextProvider" in msg
