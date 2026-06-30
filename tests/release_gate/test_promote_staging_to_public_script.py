"""Focused tests for ``scripts/promote-staging-to-public.sh``.

The script encodes the staging→public *prepare-and-verify* re-anchor mechanic
and MUST stop before any public push. These tests exercise
each governed outcome against throwaway git fixtures (no network — every run
passes ``--no-fetch``) and assert the script never grows an executed push / tag
/ publish / workflow-dispatch.

Acceptance coverage:
  * identity mismatch (author / committer / co-authored-by)   -> exit 3
  * non-fast-forward state (cherry-pick conflict)             -> exit 4
  * already-promoted content (empty re-anchor)               -> exit 0, clean
  * divergent-lineage rejection (shared / tokenpak-origin)    -> exit 5
  * missing local checks (leak/identity scanner absent)       -> exit 6
  * successful prepare-and-verify output                      -> exit 0 + SHA

Plus: usage/setup errors, a forbidden-public-surface leak (exit 7), the
no-executed-push/tag/publish source invariant, executability, and shellcheck
when it is available.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ENCODING = "utf-8"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "promote-staging-to-public.sh"
IDENTITY = "TokenPak <hello@tokenpak.ai>"

# Exit-code contract (mirrors the readonly constants in the script).
EX_OK = 0
EX_USAGE = 2
EX_IDENTITY = 3
EX_NONFF = 4
EX_DIVERGENT = 5
EX_MISSING_CHECK = 6
EX_LEAK = 7
EX_SETUP = 8


# ── helpers ─────────────────────────────────────────────────────────────────
def _split_identity(ident: str) -> tuple[str, str]:
    name = ident.split("<", 1)[0].strip()
    email = ident.split("<", 1)[1].rstrip(">").strip()
    return name, email


def run_git(repo: Path, *args: str, env: dict, author: str = IDENTITY,
            check: bool = True) -> subprocess.CompletedProcess:
    name, email = _split_identity(author)
    e = dict(env)
    e.update(
        GIT_AUTHOR_NAME=name,
        GIT_AUTHOR_EMAIL=email,
        GIT_COMMITTER_NAME=name,
        GIT_COMMITTER_EMAIL=email,
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=e, capture_output=True, text=True, check=check,
    )


def rev(repo: Path, ref: str, env: dict) -> str:
    return run_git(repo, "rev-parse", ref, env=env).stdout.strip()


def commit(repo: Path, msg: str, env: dict, author: str = IDENTITY) -> str:
    run_git(repo, "add", "-A", env=env)
    run_git(repo, "commit", "-q", "-m", msg, env=env, author=author)
    return rev(repo, "HEAD", env)


def make_staging(repo: Path, env: dict, *, fname: str, content: str,
                 msg: str, author: str = IDENTITY) -> tuple[str, str]:
    """Create a staging commit on top of current main, then reset main back so
    the content is staging-only. Returns (staging_sha, public_sha)."""
    public = rev(repo, "main", env)
    (repo / "tokenpak" / fname).write_text(content, encoding=ENCODING)
    staging = commit(repo, msg, env, author=author)
    run_git(repo, "reset", "-q", "--hard", public, env=env)
    return staging, public


def write_leak_stub(path: Path) -> Path:
    """A stand-in for scripts/release_gate/check_release_leaks.py honouring the
    same contract: exit 1 if any scanned file contains ``FORBIDDEN_TOKEN``,
    else exit 0."""
    path.write_text(
        "import os, sys\n"
        "tree = sys.argv[sys.argv.index('--tree') + 1] if '--tree' in sys.argv else None\n"
        "bad = 0\n"
        "if tree and os.path.isdir(tree):\n"
        "    for root, _, files in os.walk(tree):\n"
        "        for f in files:\n"
        "            p = os.path.join(root, f)\n"
        "            try:\n"
        "                data = open(p, encoding='utf-8', errors='ignore').read()\n"
        "            except OSError:\n"
        "                continue\n"
        "            if 'FORBIDDEN_TOKEN' in data:\n"
        "                bad += 1\n"
        "print(f'stub-leak-scan tree={tree} bad={bad}')\n"
        "sys.exit(1 if bad else 0)\n",
        encoding=ENCODING,
    )
    return path


def run_script(repo: Path, env: dict, *, staging_ref: str, public_ref: str,
               leak_check: Path | str, prepared: str = "promote/test",
               extra: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    cmd = [
        "bash", str(SCRIPT),
        "--repo", str(repo), "--no-fetch",
        "--public-ref", public_ref, "--staging-ref", staging_ref,
        "--leak-check", str(leak_check), "--leak-tree", "tokenpak",
        "--prepared-branch", prepared, *extra,
    ]
    return subprocess.run(cmd, env=env, capture_output=True, text=True)


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
def env(tmp_path: Path) -> dict:
    """Git env isolated from the developer's global/system config (gpgsign,
    commit templates, …) so commits are deterministic in CI."""
    e = os.environ.copy()
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding=ENCODING)
    e["GIT_CONFIG_GLOBAL"] = str(empty)
    e["GIT_CONFIG_SYSTEM"] = os.devnull
    e["GIT_TERMINAL_PROMPT"] = "0"
    return e


@pytest.fixture
def repo(tmp_path: Path, env: dict) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    run_git(r, "init", "-q", "-b", "main", ".", env=env)
    (r / "tokenpak").mkdir()
    (r / "tokenpak" / "a.txt").write_text("base\n", encoding=ENCODING)
    commit(r, "base public commit", env)
    return r


@pytest.fixture
def leak_stub(tmp_path: Path) -> Path:
    return write_leak_stub(tmp_path / "leak_stub.py")


# ── presence / usage ────────────────────────────────────────────────────────
def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "script is not executable (chmod +x)"


def test_help_exits_zero():
    cp = subprocess.run(["bash", str(SCRIPT), "--help"],
                        capture_output=True, text=True)
    assert cp.returncode == EX_OK
    assert "PREPARE" in cp.stdout
    assert "never pushes" in cp.stdout.lower()


def test_missing_staging_ref_is_usage_error():
    cp = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert cp.returncode == EX_USAGE


def test_unknown_argument_is_usage_error():
    cp = subprocess.run(["bash", str(SCRIPT), "--frobnicate"],
                        capture_output=True, text=True)
    assert cp.returncode == EX_USAGE


def test_non_git_repo_is_setup_error(tmp_path: Path, env: dict, leak_stub: Path):
    cp = run_script(tmp_path, env, staging_ref="HEAD",
                    public_ref="HEAD", leak_check=leak_stub)
    assert cp.returncode == EX_SETUP


# ── successful prepare-and-verify ───────────────────────────────────────────
def test_successful_prepare_and_verify(repo: Path, env: dict, leak_stub: Path):
    staging, public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature (STAGE-1)",
    )
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=leak_stub, prepared="promote/landing")
    assert cp.returncode == EX_OK, cp.stderr + cp.stdout
    # Output names the prepared SHA + the human approval push command.
    m = re.search(r"RESULT: prepared ([0-9a-f]{40})", cp.stdout)
    assert m, f"no prepared SHA in output:\n{cp.stdout}"
    prepared_sha = m.group(1)
    assert "does NOT push" in cp.stdout
    assert "git push" in cp.stdout  # the printed approval command
    # The prepared branch records exactly that commit, re-anchored on public.
    assert rev(repo, "promote/landing", env) == prepared_sha
    parents = run_git(repo, "rev-list", "--parents", "-n1", prepared_sha,
                      env=env).stdout.split()
    assert parents[1:] == [public], "prepared commit is not a linear ff of public"


def test_no_public_side_effect(repo: Path, env: dict, leak_stub: Path):
    """A prepare-and-verify run must not move public main or push anything."""
    staging, public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature (STAGE-1)",
    )
    main_before = rev(repo, "main", env)
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=leak_stub)
    assert cp.returncode == EX_OK
    assert rev(repo, "main", env) == main_before, "public main was mutated"


def test_already_promoted_is_clean(repo: Path, env: dict, leak_stub: Path):
    staging, _public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature (STAGE-1)",
    )
    # Advance public main to already contain the staging content.
    run_git(repo, "merge", "--ff-only", staging, env=env)
    public_now = rev(repo, "main", env)
    cp = run_script(repo, env, staging_ref=staging, public_ref=public_now,
                    leak_check=leak_stub)
    assert cp.returncode == EX_OK
    assert "already-promoted" in cp.stdout


# ── identity ────────────────────────────────────────────────────────────────
def test_identity_mismatch_author_rejected(repo: Path, env: dict, leak_stub: Path):
    staging, public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature by outsider",
        author="Outside Dev <dev@example.com>",
    )
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=leak_stub)
    assert cp.returncode == EX_IDENTITY, cp.stdout + cp.stderr


def test_co_authored_by_trailer_rejected(repo: Path, env: dict, leak_stub: Path):
    staging, public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature\n\nCo-authored-by: Someone <s@example.com>",
    )
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=leak_stub)
    assert cp.returncode == EX_IDENTITY, cp.stdout + cp.stderr


# ── non-fast-forward ────────────────────────────────────────────────────────
def test_non_fast_forward_conflict_rejected(repo: Path, env: dict, leak_stub: Path):
    public = rev(repo, "main", env)
    # public edits a.txt
    (repo / "tokenpak" / "a.txt").write_text("base\npublic-line\n", encoding=ENCODING)
    public2 = commit(repo, "chore: public edits a", env)
    # staging branches off the ORIGINAL base and edits the same line differently
    run_git(repo, "checkout", "-q", "-B", "stg", public, env=env)
    (repo / "tokenpak" / "a.txt").write_text("base\nstaging-line\n", encoding=ENCODING)
    staging = commit(repo, "feat: staging edits a", env)
    run_git(repo, "checkout", "-q", "main", env=env)
    cp = run_script(repo, env, staging_ref=staging, public_ref=public2,
                    leak_check=leak_stub)
    assert cp.returncode == EX_NONFF, cp.stdout + cp.stderr


# ── divergent lineage ───────────────────────────────────────────────────────
def test_divergent_forbidden_remote_rejected(repo: Path, env: dict, leak_stub: Path):
    staging, public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature",
    )
    run_git(repo, "remote", "add", "shared",
            "/srv/git/tokenpak-origin.git", env=env)
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=leak_stub, extra=("--staging-remote", "shared"))
    assert cp.returncode == EX_DIVERGENT, cp.stdout + cp.stderr


def test_divergent_shared_ref_namespace_rejected(repo: Path, env: dict, leak_stub: Path):
    public = rev(repo, "main", env)
    cp = run_script(repo, env, staging_ref="shared/main", public_ref=public,
                    leak_check=leak_stub)
    assert cp.returncode == EX_DIVERGENT, cp.stdout + cp.stderr


# ── local-check enforcement ─────────────────────────────────────────────────
def test_missing_local_check_fails_closed(repo: Path, env: dict, tmp_path: Path):
    staging, public = make_staging(
        repo, env, fname="feature.txt", content="clean feature\n",
        msg="feat: add feature",
    )
    missing = tmp_path / "does-not-exist" / "check_release_leaks.py"
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=missing)
    assert cp.returncode == EX_MISSING_CHECK, cp.stdout + cp.stderr
    # Fails closed with the exact command an operator must run.
    assert "--tree" in (cp.stdout + cp.stderr)


def test_leak_in_prepared_tree_rejected(repo: Path, env: dict, leak_stub: Path):
    staging, public = make_staging(
        repo, env, fname="feature.txt",
        content="this file contains FORBIDDEN_TOKEN and must not ship\n",
        msg="feat: add feature with forbidden leak",
    )
    cp = run_script(repo, env, staging_ref=staging, public_ref=public,
                    leak_check=leak_stub)
    assert cp.returncode == EX_LEAK, cp.stdout + cp.stderr


# ── hard-boundary source invariant ──────────────────────────────────────────
def test_script_never_executes_push_tag_or_publish():
    """Static guard: the script may *print* the push command, but must never
    execute a public push / tag / publish / workflow-dispatch."""
    forbidden_anywhere = (
        "gh release", "gh pr merge", "gh workflow run", "workflow_dispatch",
        "twine ", "twine.", "pypi-upload", "--tags",
    )
    push_tag = ("git push", "git tag")
    for i, line in enumerate(SCRIPT.read_text(encoding=ENCODING).splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        for needle in forbidden_anywhere:
            assert needle not in line, f"line {i} contains release verb {needle!r}: {line!r}"
        for needle in push_tag:
            if needle in line:
                before = line.split(needle, 1)[0]
                assert '"' in before or "'" in before, (
                    f"line {i}: {needle!r} appears outside a quoted string — "
                    f"that would execute a push/tag: {line!r}"
                )


# ── shellcheck (when available) ─────────────────────────────────────────────
@pytest.mark.skipif(shutil.which("shellcheck") is None,
                    reason="shellcheck not installed")
def test_shellcheck_clean():
    cp = subprocess.run(["shellcheck", str(SCRIPT)],
                        capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr
