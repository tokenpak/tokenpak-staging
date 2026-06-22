from __future__ import annotations

import stat


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_monitor_db_write_respects_tokenpak_home_and_secure_modes(tmp_path, monkeypatch):
    from tokenpak import _paths
    from tokenpak.proxy.monitor import Monitor

    home = tmp_path / "home"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))
    monkeypatch.delenv("TOKENPAK_DB", raising=False)
    monkeypatch.delenv("TOKENPAK_MONITOR_DB", raising=False)

    db_path = _paths.monitor_db(mode="write")
    assert db_path == home / "monitor.db"

    Monitor(str(db_path))

    assert _mode(home) == 0o700
    assert _mode(db_path) == 0o600


def test_spend_guard_default_db_uses_tokenpak_home_and_secure_modes(tmp_path, monkeypatch):
    from tokenpak.proxy.spend_guard.audit import write_audit
    from tokenpak.proxy.spend_guard.grants import GrantStore
    from tokenpak.proxy.spend_guard.pending import PendingStore
    from tokenpak.proxy.spend_guard.reservation import ReservationStore

    home = tmp_path / "home"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))

    expected = home / "spend_guard.db"
    assert PendingStore().path == expected
    assert GrantStore().path == expected
    assert ReservationStore().path == expected

    write_audit(event_type="allow", session_id="sess-1", decision_str="allow")

    assert _mode(home) == 0o700
    assert _mode(expected) == 0o600


def test_memory_sqlite_target_does_not_chmod_cwd(tmp_path, monkeypatch):
    from tokenpak.proxy._local_data import secure_sqlite_connect

    monkeypatch.chdir(tmp_path)
    tmp_path.chmod(0o755)

    conn = secure_sqlite_connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    finally:
        conn.close()

    assert _mode(tmp_path) == 0o755


def test_default_proxy_file_logger_uses_private_log_dir_and_file(tmp_path, monkeypatch):
    from tokenpak.proxy.middleware.logger import LoggingConfig, RequestLogger

    home = tmp_path / "home"
    monkeypatch.setenv("TOKENPAK_HOME", str(home))

    request_logger = RequestLogger(
        LoggingConfig(destination="file", async_buffer_size=1, flush_interval_sec=60)
    )
    try:
        request_logger.log_request(endpoint="/v1/messages", message="ok")
    finally:
        request_logger.stop()

    log_dir = home / "logs"
    logs = list(log_dir.glob("proxy-*.log"))

    assert _mode(home) == 0o700
    assert _mode(log_dir) == 0o700
    assert len(logs) == 1
    assert _mode(logs[0]) == 0o600
