#!/usr/bin/env python3
"""Normalize a built wheel's zip permission bits to be byte-reproducible.

The ``build`` backend's wheel writer already honors ``SOURCE_DATE_EPOCH``
for each zip entry's timestamp, so a wheel built from the same commit at a
different wall-clock moment already matches on content and dates. What it
does not normalize is the Unix permission bits embedded in each entry's
*external file attributes* field: those are derived from the umask in
effect at build time, so a wheel built under one umask (e.g. a local dev
machine) differs byte-for-byte in its zip metadata from the same wheel
built under another (e.g. a CI runner) even though every file's content is
identical. Zip has no owner/group concept to normalize (unlike a tar
member) — permission bits are the only environment-derived field.

This script rewrites an already-built wheel in place: every entry's
external attributes are canonicalized to ``0755`` for anything that was
executable, ``0644`` otherwise (wheels have no directory entries), and
``create_system`` is pinned to Unix. Entry order, content, compression, and
timestamps are left untouched. Because only metadata changes and every
entry's bytes are re-written unmodified, ``RECORD`` (and the hashes it
lists, which cover payload content only) stays valid across the rewrite.

Usage:
    python scripts/release_gate/normalize_wheel.py dist/*.whl

Exit status: ``0`` on success, ``1`` if a given path cannot be normalized.
"""

from __future__ import annotations

import argparse
import io
import stat
import zipfile


def _canonical_external_attr(filename: str, external_attr: int) -> int:
    is_dir = filename.endswith("/")
    unix_mode = (external_attr >> 16) & 0xFFFF
    executable = bool(unix_mode & 0o111)
    mode = 0o755 if (is_dir or executable) else 0o644
    mode |= stat.S_IFDIR if is_dir else stat.S_IFREG
    return mode << 16


def normalize(path: str) -> None:
    with open(path, "rb") as f:
        raw = f.read()

    with zipfile.ZipFile(io.BytesIO(raw), mode="r") as src:
        infos = src.infolist()
        out = io.BytesIO()
        with zipfile.ZipFile(out, mode="w") as dst:
            for info in infos:
                data = src.read(info.filename)
                new_info = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                new_info.compress_type = info.compress_type
                new_info.comment = info.comment
                new_info.extra = info.extra
                new_info.create_system = 3  # Unix — matches the high-bit mode below
                new_info.create_version = info.create_version
                new_info.extract_version = info.extract_version
                new_info.flag_bits = info.flag_bits
                new_info.internal_attr = info.internal_attr
                new_info.external_attr = _canonical_external_attr(info.filename, info.external_attr)
                dst.writestr(new_info, data, compress_type=info.compress_type)

    with open(path, "wb") as f:
        f.write(out.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", nargs="+", help="Path(s) to a built *.whl")
    args = parser.parse_args()

    for path in args.wheel:
        normalize(path)
        print(f"normalized {path} (external attrs canonicalized)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
