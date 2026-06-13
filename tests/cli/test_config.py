# SPDX-License-Identifier: Apache-2.0
"""tests/cli/test_config.py

Tests for the SAFE sub-scope of centralized-env Packet B:
  tokenpak config doctor   (read-only diagnostics, design §1)
  tokenpak config init     (scaffold-only, design §2)

Test plan: design doc §1.6 (TC-D-01..14) and §2.6 (TC-I-01..10) —
vault 01_PROJECTS/tokenpak/design/
centralized-env-packet-b-config-doctor-init-loadorder-design-2026-06-06.md

Invariants enforced suite-wide:
  * All fixtures are fabricated homes via $TOKENPAK_HOME + a monkeypatched
    Path.home(); no test touches the real ~/.tpk/, ~/.tokenpak/, or any
    client settings file. No network. No real secret values (clearly-marked
    fakes only, e.g. sk-ant-EXAMPLE...).
  * Every doctor invocation runs through `run_doctor_checked`, which
    snapshots the fixture tree (paths + modes + mtimes + sizes) before and
    after and asserts identical — the read-only safety test (TC-D-14).
  * The masking test plants a fake secret value and asserts it never
    appears in any output mode (design §1.4 mask-always).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tokenpak.cli.commands import config_init as config_init_mod
from tokenpak.cli.commands.config_doctor import (
    _SECRET_SHAPE_PATTERNS,
    is_known_var,
    run_config_doctor,
    secret_class,
)
from tokenpak.cli.commands.config_init import run_config_init

# A clearly-marked NON-REAL token (Std 36 §1.4 classified false-positive
# fixture): real-credential *shape*, EXAMPLE marker, never a live value.
FAKE_SECRET = "sk-ant-EXAMPLE000000fake000000fixture"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Redirect Path.home() so ~/.tpk, ~/.tokenpak and ~/.claude are isolated."""
    fh = tmp_path / "fakehome"
    fh.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fh))
    return fh


@pytest.fixture()
def clean_env(monkeypatch):
    """Scrub TokenPak/provider vars inherited from the host environment."""
    scrub_prefixes = ("TOKENPAK_", "OPENCLAW_", "ANTHROPIC_")
    scrub_exact = (
        "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
        "GITHUB_TOKEN", "NOTION_API_TOKEN", "TELEGRAM_BOT_TOKEN", "CODEX_HOME",
    )
    for name in list(os.environ):
        if name.startswith(scrub_prefixes) or name in scrub_exact:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def work_cwd(tmp_path, monkeypatch):
    """Fresh non-git cwd so doctor's ./.env + tracked-file scans are hermetic."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd


@pytest.fixture()
def tpk_home(tmp_path, monkeypatch, fake_home, clean_env, work_cwd):
    """Fabricated TokenPak home selected via the $TOKENPAK_HOME operator override."""
    home = tmp_path / "tpkhome"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    return home


def _tree_snapshot(root: Path) -> dict:
    """Paths + modes + mtimes + sizes under *root* (the TC-D-14 invariant)."""
    snap = {}
    if not root.exists():
        return snap
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        snap[str(p)] = (st.st_mode, st.st_mtime_ns, st.st_size)
    return snap


def run_doctor_checked(*roots: Path, **kwargs) -> int:
    """Run doctor asserting it performed no filesystem writes under *roots*."""
    before = [_tree_snapshot(r) for r in roots]
    code = run_config_doctor(**kwargs)
    after = [_tree_snapshot(r) for r in roots]
    assert before == after, "config doctor must be read-only (TC-D-14)"
    return code


def _doctor_json(capsys, *roots: Path, **kwargs):
    code = run_doctor_checked(*roots, json_output=True, **kwargs)
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def _check(payload: dict, check_id: str, name: str | None = None) -> dict:
    for c in payload["checks"]:
        if c["id"] == check_id and (name is None or c["check"] == name):
            return c
    raise AssertionError(f"check {check_id}/{name} not found in {payload['checks']}")


# ---------------------------------------------------------------------------
# config doctor
# ---------------------------------------------------------------------------


class TestConfigDoctor:
    def test_home_rule_env_no_writes(self, tpk_home, fake_home, capsys):
        """TC-D-01: $TOKENPAK_HOME → D1 ok rule env; exit 0; nothing created."""
        listing_before = sorted(tpk_home.iterdir())
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        assert payload["exit_code"] == 0
        assert payload["home"] == {"path": str(tpk_home), "rule": "env"}
        d1 = _check(payload, "D1")
        assert d1["status"] == "ok"
        assert sorted(tpk_home.iterdir()) == listing_before

    def test_valid_config_yaml(self, tpk_home, fake_home, capsys):
        """TC-D-02: valid config.yaml → D2 ok; exit 0."""
        (tpk_home / "config.yaml").write_text("port: 9000\nmode: hybrid\n")
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        assert _check(payload, "D2")["status"] == "ok"

    def test_malformed_config_yaml(self, tpk_home, fake_home, capsys):
        """TC-D-03: unparseable config.yaml → D2 fail; exit 4."""
        (tpk_home / "config.yaml").write_text("port: [unclosed\n  bad: :::\n")
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 4
        assert payload["exit_code"] == 4
        assert _check(payload, "D2")["status"] == "fail"

    def test_legacy_only_home(self, fake_home, clean_env, work_cwd, capsys):
        """TC-D-04: legacy-only resolution → D1 warn rule legacy; exit 0."""
        legacy = fake_home / ".tokenpak"
        legacy.mkdir()
        code, payload = _doctor_json(capsys, fake_home)
        assert code == 0
        assert payload["home"]["rule"] == "legacy"
        d1 = _check(payload, "D1")
        assert d1["status"] == "warn"
        assert "home migrate" in d1["detail"]  # cli-dx F1: relocation verb

    def test_masks_planted_secret(self, tpk_home, fake_home, monkeypatch, capsys):
        """TC-D-05: planted fake secret value must NEVER appear in output."""
        monkeypatch.setenv("TOKENPAK_PORT", "9999")
        monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_SECRET)
        # JSON mode
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        raw = json.dumps(payload)
        assert FAKE_SECRET not in raw
        names = [c["check"] for c in payload["checks"] if c["id"] == "D4"]
        assert "env:ANTHROPIC_API_KEY" in names
        assert "env:TOKENPAK_PORT" in names
        key_check = _check(payload, "D4", "env:ANTHROPIC_API_KEY")
        assert "set (env)" in key_check["message"]
        assert "masked" in key_check["message"]
        # Human + verbose mode
        run_doctor_checked(tpk_home, fake_home, verbose=True)
        out = capsys.readouterr()
        assert FAKE_SECRET not in out.out
        assert FAKE_SECRET not in out.err
        # Quiet mode
        run_doctor_checked(tpk_home, fake_home, quiet=True)
        out = capsys.readouterr()
        assert FAKE_SECRET not in out.out

    def test_unknown_tokenpak_var_warns(self, tpk_home, fake_home, monkeypatch, capsys):
        """TC-D-06: unknown TOKENPAK_* name → D4 warn, never fail; exit 0."""
        monkeypatch.setenv("TOKENPAK_BOGUS", "1")
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        d4 = _check(payload, "D4", "env_vars")
        assert d4["status"] == "warn"
        assert "TOKENPAK_BOGUS" in d4["message"]
        assert "not in the documented schema" in d4["message"]  # cli-dx F3

    def test_attach_state_local_proxy(self, tpk_home, fake_home, capsys):
        """TC-D-07: client settings → local proxy URL → D5 ok; no write."""
        claude = fake_home / ".claude"
        claude.mkdir()
        settings = claude / "settings.json"
        settings.write_text(json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8766"}}
        ))
        mtime_before = settings.stat().st_mtime_ns
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        d5 = _check(payload, "D5")
        assert d5["status"] == "ok"
        assert "local proxy" in d5["message"]
        assert settings.stat().st_mtime_ns == mtime_before

    def test_attach_state_upstream(self, tpk_home, fake_home, capsys):
        """TC-D-08: non-default upstream base URL → D5 info."""
        claude = fake_home / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(json.dumps(
            {"env": {"ANTHROPIC_BASE_URL": "https://gateway.example.com/v1"}}
        ))
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        assert _check(payload, "D5")["status"] == "info"

    def test_env_file_loose_mode_not_fixed(self, tpk_home, fake_home, capsys):
        """TC-D-09: <tpk-home>/.env mode 0644 → D6 warn; mode NOT chmod'd."""
        env_file = tpk_home / ".env"
        env_file.write_text("# names only here\nTOKENPAK_PORT=1234\n")
        env_file.chmod(0o644)
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        d6 = _check(payload, "D6")
        assert d6["status"] == "warn"
        assert "0600" in d6["message"] or "0600" in d6["detail"]
        assert (env_file.stat().st_mode & 0o777) == 0o644

    def test_split_home_warns(self, fake_home, clean_env, work_cwd, capsys):
        """TC-D-10: both ~/.tpk/ and ~/.tokenpak/ present → D7 warn split-home."""
        (fake_home / ".tpk").mkdir()
        (fake_home / ".tokenpak").mkdir()
        code, payload = _doctor_json(capsys, fake_home)
        assert code == 0
        assert payload["home"]["rule"] == "canonical"
        d7 = _check(payload, "D7")
        assert d7["status"] == "warn"
        assert "split-home" in d7["message"]

    def test_committed_secret_shape_fails(self, tpk_home, fake_home, work_cwd, capsys):
        """TC-D-11: tracked file with secret-shaped value → D7 fail; exit 4.

        Fixture token is clearly-marked non-real (sk-ant-EXAMPLE...): the
        check fires on *shape* (Std 36 §1.4); the value is never echoed.
        """
        import shutil as _shutil
        import subprocess as _sp

        if _shutil.which("git") is None:
            pytest.skip("git not available")
        _sp.run(["git", "init", "-q"], cwd=work_cwd, check=True)
        leaked = work_cwd / "secrets.env"
        leaked.write_text(f"ANTHROPIC_API_KEY={FAKE_SECRET}\n")
        _sp.run(["git", "add", "secrets.env"], cwd=work_cwd, check=True)
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 4
        d7 = _check(payload, "D7")
        assert d7["status"] == "fail"
        assert "secrets.env" in d7["message"]
        # mask-always even in the failure report
        assert FAKE_SECRET not in json.dumps(payload)

    def test_json_schema_stable(self, tpk_home, fake_home, capsys):
        """TC-D-12: --json parses; schema keys present; exit_code matches."""
        (tpk_home / "config.yaml").write_text("port: 9000\n")
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert set(payload) == {"home", "checks", "summary", "exit_code"}
        assert set(payload["home"]) == {"path", "rule"}
        assert set(payload["summary"]) == {"ok", "warn", "fail", "info"}
        for c in payload["checks"]:
            assert set(c) == {"id", "check", "status", "message", "detail"}
        assert payload["exit_code"] == code == 0

    def test_quiet_all_ok_empty_stdout(self, tpk_home, fake_home, capsys):
        """TC-D-13: --quiet on an all-ok home → empty stdout; exit 0."""
        code = run_doctor_checked(tpk_home, fake_home, quiet=True)
        out = capsys.readouterr()
        assert code == 0
        assert out.out == ""

    def test_read_only_invariant_all_modes(self, tpk_home, fake_home, capsys):
        """TC-D-14: populated home tree identical before/after every mode."""
        (tpk_home / "config.yaml").write_text("port: 9000\n")
        (tpk_home / "config.json").write_text('{"stats_footer": true}')
        env_file = tpk_home / ".env"
        env_file.write_text("TOKENPAK_MODE=strict\n")
        env_file.chmod(0o600)
        claude = fake_home / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text('{"env": {}}')
        for kwargs in (
            {},
            {"verbose": True},
            {"quiet": True},
            {"json_output": True},
        ):
            run_doctor_checked(tpk_home, fake_home, **kwargs)
            capsys.readouterr()

    def test_precedence_reporting_layers(self, tpk_home, fake_home, monkeypatch, capsys):
        """Brief requirement: D3 reports which layer supplies each curated key."""
        (tpk_home / "config.yaml").write_text("port: 7000\nmode: strict\n")
        monkeypatch.setenv("TOKENPAK_PORT", "9999")
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        # env beats user config
        assert "env" in _check(payload, "D3", "precedence:port")["message"]
        # user config beats default
        assert "user config" in _check(payload, "D3", "precedence:mode")["message"]
        # nothing set → default
        assert "default" in _check(payload, "D3", "precedence:rate_limit_rpm")["message"]
        # the chain itself is rendered and marks .env layers as spec-only
        chain = _check(payload, "D3", "precedence_chain")
        assert "spec layers" in chain["detail"]

    def test_precedence_notes_spec_env_layer(self, tpk_home, fake_home, capsys):
        """A key named in <tpk-home>/.env is reported as a spec-layer
        observation (drift/fallback, §0.2) — not as the effective source."""
        env_file = tpk_home / ".env"
        env_file.write_text("TOKENPAK_MODE=aggressive\n")
        env_file.chmod(0o600)
        code, payload = _doctor_json(capsys, tpk_home, fake_home)
        assert code == 0
        mode_check = _check(payload, "D3", "precedence:mode")
        assert "default" in mode_check["message"]  # live loader ignores .env
        assert "spec layer 4" in mode_check["message"]
        assert "aggressive" not in json.dumps(payload)  # names only, no values

    def test_doctor_exit_code_via_dispatcher(self, tpk_home, fake_home, capsys):
        """The argparse wrapper returns doctor's exit code to the dispatcher."""
        import argparse

        from tokenpak._cli_core import cmd_config_doctor

        args = argparse.Namespace(json=True, quiet=False, verbose=False)
        rc = cmd_config_doctor(args)
        capsys.readouterr()
        assert rc == 0


class TestManifestHelpers:
    def test_known_names_and_families(self):
        assert is_known_var("TOKENPAK_PORT")
        assert is_known_var("TOKENPAK_SPEND_GUARD_WARN_TOKENS")
        assert is_known_var("ANTHROPIC_API_KEY_3")  # slot pattern
        assert is_known_var("OPENCLAW_GATEWAY_URL")  # legacy family
        assert not is_known_var("TOKENPAK_BOGUS")

    def test_secret_classes(self):
        assert secret_class("ANTHROPIC_API_KEY") == "high"
        assert secret_class("TOKENPAK_TELEGRAM_BOT_TOKEN") == "high"
        assert secret_class("TOKENPAK_SLACK_WEBHOOK") == "high"
        assert secret_class("ANTHROPIC_BASE_URL") == "low"  # cli-dx F6: *_URL endpoint, not a credential
        assert secret_class("TOKENPAK_PORT") == "low"


# ---------------------------------------------------------------------------
# config init
# ---------------------------------------------------------------------------


@pytest.fixture()
def init_home(tmp_path, monkeypatch, fake_home, clean_env, work_cwd):
    """$TOKENPAK_HOME pointing at a NOT-yet-existing dir (init creates it)."""
    home = tmp_path / "newhome"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    return home


def _make_interactive(monkeypatch, answer: str | None = None):
    monkeypatch.setattr(config_init_mod, "_is_interactive", lambda: True)
    if answer is not None:
        import builtins

        monkeypatch.setattr(builtins, "input", lambda *_: answer)


class TestConfigInit:
    def test_creates_config(self, init_home, capsys):
        """TC-I-01: scaffolds <home>/config.yaml; dir 0700; exit 0; parses.

        NOTE (flagged conflict): the design's TC-I-01 also expects the
        scaffold to pass `config validate`, but the repo's shipped
        validators (schemas via cli_validate_config and
        tokenpak/config/schema.json) pre-date generate_default_yaml()'s
        current key set and reject it — a pre-existing inconsistency on
        staging main, surfaced for a separate fix, not silently patched
        here. Parse correctness is asserted instead.
        """
        import yaml

        rc = run_config_init()
        capsys.readouterr()
        assert rc == 0
        target = init_home / "config.yaml"
        assert target.is_file()
        assert (init_home.stat().st_mode & 0o777) == 0o700
        parsed = yaml.safe_load(target.read_text())
        assert parsed["port"] == 8766

    def test_idempotent_noop(self, init_home, monkeypatch, capsys):
        """TC-I-02: second (interactive) run is a no-op; exit 0."""
        assert run_config_init() == 0
        target = init_home / "config.yaml"
        mtime = target.stat().st_mtime_ns
        _make_interactive(monkeypatch)
        capsys.readouterr()
        rc = run_config_init()
        out = capsys.readouterr().out
        assert rc == 0
        assert target.stat().st_mtime_ns == mtime
        assert "already present" in out

    def test_force_backs_up_first(self, init_home, monkeypatch, capsys):
        """TC-I-03: --force (TTY, confirm yes) → .bak holds old content."""
        init_home.mkdir(mode=0o700)
        target = init_home / "config.yaml"
        target.write_text("port: 1111  # user-customized\n")
        _make_interactive(monkeypatch, answer="y")
        rc = run_config_init(force=True)
        capsys.readouterr()
        assert rc == 0
        backup = init_home / "config.yaml.bak"
        assert backup.is_file()
        assert "user-customized" in backup.read_text()
        assert "user-customized" not in target.read_text()
        assert "port: 8766" in target.read_text()

    def test_force_tty_decline_aborts(self, init_home, monkeypatch, capsys):
        """--force on a TTY with 'n' answer → nothing changed; exit 0."""
        init_home.mkdir(mode=0o700)
        target = init_home / "config.yaml"
        target.write_text("port: 1111\n")
        _make_interactive(monkeypatch, answer="n")
        rc = run_config_init(force=True)
        capsys.readouterr()
        assert rc == 0
        assert target.read_text() == "port: 1111\n"
        assert not (init_home / "config.yaml.bak").exists()

    def test_existing_noninteractive_without_force(self, init_home, monkeypatch, capsys):
        """TC-I-04: non-interactive + existing config + no --force → exit 1,
        stderr names --force, nothing written."""
        init_home.mkdir(mode=0o700)
        target = init_home / "config.yaml"
        target.write_text("port: 1111\n")
        mtime = target.stat().st_mtime_ns
        monkeypatch.setenv("TOKENPAK_NONINTERACTIVE", "1")
        rc = run_config_init()
        err = capsys.readouterr().err
        assert rc == 1
        assert "--force" in err
        assert target.stat().st_mtime_ns == mtime

    def test_env_stub_placeholders_only(self, init_home, capsys):
        """TC-I-05: --with-env-stub writes .env.example, placeholders only;
        the real .env is NOT created."""
        rc = run_config_init(with_env_stub=True)
        capsys.readouterr()
        assert rc == 0
        stub = init_home / ".env.example"
        assert stub.is_file()
        assert not (init_home / ".env").exists()
        for line in stub.read_text().splitlines():
            stripped = line.lstrip("# ").strip()
            if "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            name = name.strip()
            if secret_class(name) in ("high", "medium"):
                # secret-class keys carry NO value (comment tails excluded)
                assert value.split("#")[0].strip() == "", (
                    f"secret-class key {name} must have no placeholder value"
                )

    def test_never_writes_secret_values(self, init_home, capsys):
        """TC-I-06: no file under <home> contains a provider-key-shaped value."""
        rc = run_config_init(with_env_stub=True)
        capsys.readouterr()
        assert rc == 0
        for p in init_home.rglob("*"):
            if not p.is_file():
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
            for family, pattern in _SECRET_SHAPE_PATTERNS:
                assert not pattern.search(content), f"{family} shape in {p}"
        assert not (init_home / ".env").exists()

    def test_no_global_mutation(self, init_home, fake_home, capsys):
        """TC-I-07: $HOME (incl. a fake ~/.claude) and os.environ untouched."""
        claude = fake_home / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text('{"env": {}}')
        home_before = _tree_snapshot(fake_home)
        environ_before = dict(os.environ)
        rc = run_config_init(with_env_stub=True)
        capsys.readouterr()
        assert rc == 0
        assert _tree_snapshot(fake_home) == home_before
        assert dict(os.environ) == environ_before

    def test_print_dry_run(self, init_home, capsys):
        """TC-I-08: --print writes nothing; planned-changes block printed."""
        rc = run_config_init(print_only=True)
        out = capsys.readouterr().out
        assert rc == 0
        assert not init_home.exists()  # not even the home dir is created
        assert "Planned changes" in out
        assert "config.yaml" in out

    def test_json_output_schema(self, init_home, capsys):
        """TC-I-09: --json emits {"created": [...], "skipped": [...], ...}."""
        rc = run_config_init(json_output=True)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["exit_code"] == 0
        assert str(init_home / "config.yaml") in payload["created"]
        assert payload["skipped"] == []

    def test_noninteractive_fresh_scaffolds_without_prompt(
        self, init_home, monkeypatch, capsys
    ):
        """TC-I-10: TOKENPAK_NONINTERACTIVE=1 + empty home → scaffolds, no prompt."""
        import builtins

        monkeypatch.setenv("TOKENPAK_NONINTERACTIVE", "1")

        def _no_prompt(*_args, **_kwargs):
            raise AssertionError("init must not prompt in non-interactive mode")

        monkeypatch.setattr(builtins, "input", _no_prompt)
        rc = run_config_init()
        capsys.readouterr()
        assert rc == 0
        assert (init_home / "config.yaml").is_file()

    def test_init_dispatcher_wrapper(self, init_home, capsys):
        """The argparse wrapper forwards flags and returns the exit code."""
        import argparse

        from tokenpak._cli_core import cmd_config_init

        args = argparse.Namespace(
            force=False, with_env_stub=False, print_only=False,
            json=True, quiet=False,
        )
        rc = cmd_config_init(args)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["exit_code"] == 0
