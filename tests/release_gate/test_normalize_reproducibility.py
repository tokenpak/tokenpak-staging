"""Owner/umask reproducible-build regressions.

v1.23.0 shipped a byte-level divergence between the CI-built published
artifacts and locally-built comparison artifacts even though payload content
and every timestamp matched exactly: tar member owner/group
(``runner/runner`` vs. ``sue/sue``) and umask-derived permission bits (also
in the wheel zip's external attributes). ``SOURCE_DATE_EPOCH`` normalization
covers timestamps only, and the two prior "independent" rebuild proofs that
validated the original fix both ran on the same machine under the same
user/umask, so this divergence class was structurally undetectable until a
CI-runner build was compared against one.

These tests construct synthetic sdist/wheel archives with distinct
owner/group/mode metadata (standing in for two different build
environments) but byte-identical payload, and prove the normalization
scripts collapse them to byte-identical output — the same property the
release workflow's dual-umask check verifies against a real build.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import stat
import tarfile
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(mod_name: str, *parts: str):
    path = _REPO_ROOT.joinpath(*parts)
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


normalize_sdist = _load("normalize_sdist", "scripts", "release_gate", "normalize_sdist.py")
normalize_wheel = _load("normalize_wheel", "scripts", "release_gate", "normalize_wheel.py")

EPOCH = 1_788_226_709  # arbitrary fixed SOURCE_DATE_EPOCH for these tests


def _make_sdist(
    tmp_path: Path, name: str, *, uid: int, gid: int, uname: str, gname: str, umask: int
) -> Path:
    """Build a synthetic sdist with owner/mode metadata as if produced under
    the given (uid, gid, uname, gname, umask) — standing in for "built by
    sue locally" vs. "built by runner on a CI box"."""
    path = tmp_path / name
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        dir_info = tarfile.TarInfo("pkg")
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o777 & ~umask
        dir_info.mtime = 1_700_000_000  # deliberately NOT the epoch — proves mtime gets pinned too
        dir_info.uid, dir_info.gid = uid, gid
        dir_info.uname, dir_info.gname = uname, gname
        tar.addfile(dir_info)

        file_info = tarfile.TarInfo("pkg/module.py")
        file_info.type = tarfile.REGTYPE
        data = b"print('hello')\n"
        file_info.size = len(data)
        file_info.mode = 0o666 & ~umask
        file_info.mtime = 1_700_000_001
        file_info.uid, file_info.gid = uid, gid
        file_info.uname, file_info.gname = uname, gname
        tar.addfile(file_info, io.BytesIO(data))

        script_info = tarfile.TarInfo("pkg/run.sh")
        script_info.type = tarfile.REGTYPE
        script_data = b"#!/bin/sh\necho hi\n"
        script_info.size = len(script_data)
        script_info.mode = 0o777 & ~umask  # was executable before the checkout's umask trimmed it
        script_info.mtime = 1_700_000_002
        script_info.uid, script_info.gid = uid, gid
        script_info.uname, script_info.gname = uname, gname
        tar.addfile(script_info, io.BytesIO(script_data))

    with gzip.GzipFile(
        filename="", mode="wb", mtime=1_700_000_003, fileobj=(fobj := io.BytesIO())
    ) as gz:
        gz.write(payload.getvalue())
    path.write_bytes(fobj.getvalue())
    return path


def test_sdist_normalize_collapses_owner_and_umask_divergence(tmp_path):
    """Two sdists built with different owner + umask, same payload, must
    become byte-identical after normalization — the exact class of
    divergence found at v1.23.0."""
    local = _make_sdist(
        tmp_path, "local.tar.gz", uid=1000, gid=1000, uname="sue", gname="sue", umask=0o002
    )
    ci = _make_sdist(
        tmp_path, "ci.tar.gz", uid=1001, gid=1001, uname="runner", gname="runner", umask=0o022
    )

    assert local.read_bytes() != ci.read_bytes(), (
        "fixture setup is wrong: inputs should differ pre-normalize"
    )

    normalize_sdist.normalize(str(local), EPOCH)
    normalize_sdist.normalize(str(ci), EPOCH)

    assert local.read_bytes() == ci.read_bytes()


def test_sdist_normalize_pins_owner_and_canonical_mode(tmp_path):
    path = _make_sdist(
        tmp_path, "a.tar.gz", uid=1000, gid=1000, uname="sue", gname="sue", umask=0o002
    )
    normalize_sdist.normalize(str(path), EPOCH)

    with tarfile.open(path, mode="r:gz") as tar:
        members = {m.name: m for m in tar.getmembers()}

    for member in members.values():
        assert member.uid == 0
        assert member.gid == 0
        assert member.uname == ""
        assert member.gname == ""
        assert member.mtime == EPOCH

    assert members["pkg"].mode == 0o755  # directory
    assert members["pkg/module.py"].mode == 0o644  # non-executable file
    assert members["pkg/run.sh"].mode == 0o755  # was executable — preserved, not just zeroed


def _make_wheel(tmp_path: Path, name: str, *, umask: int) -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, mode="w") as zf:
        info = zipfile.ZipInfo("pkg/module.py", date_time=(2026, 8, 31, 23, 4, 48))
        mode = 0o666 & ~umask
        info.external_attr = (mode | stat.S_IFREG) << 16
        zf.writestr(info, b"print('hello')\n")

        script = zipfile.ZipInfo("pkg/run.sh", date_time=(2026, 8, 31, 23, 4, 48))
        script_mode = 0o777 & ~umask
        script.external_attr = (script_mode | stat.S_IFREG) << 16
        zf.writestr(script, b"#!/bin/sh\necho hi\n")
    return path


def test_wheel_normalize_collapses_umask_divergence(tmp_path):
    local = _make_wheel(tmp_path, "local.whl", umask=0o002)
    ci = _make_wheel(tmp_path, "ci.whl", umask=0o022)

    assert local.read_bytes() != ci.read_bytes()

    normalize_wheel.normalize(str(local))
    normalize_wheel.normalize(str(ci))

    assert local.read_bytes() == ci.read_bytes()


def test_wheel_normalize_pins_canonical_mode_and_preserves_payload(tmp_path):
    path = _make_wheel(tmp_path, "a.whl", umask=0o002)
    with zipfile.ZipFile(path) as zf:
        before = {i.filename: zf.read(i.filename) for i in zf.infolist()}

    normalize_wheel.normalize(str(path))

    with zipfile.ZipFile(path) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        after = {i.filename: zf.read(i.filename) for i in zf.infolist()}

    assert after == before, (
        "normalization must not touch payload bytes (RECORD hashes must stay valid)"
    )
    assert (infos["pkg/module.py"].external_attr >> 16) & 0o7777 == 0o644
    assert (infos["pkg/run.sh"].external_attr >> 16) & 0o7777 == 0o755
    assert all(i.create_system == 3 for i in infos.values())
