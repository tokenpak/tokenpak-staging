#!/usr/bin/env python3
"""Normalize a built sdist tarball to be byte-reproducible.

The ``build`` backend's wheel builder honors ``SOURCE_DATE_EPOCH`` for the
timestamps it writes into the zip (rebuilding the same commit yields the
same wheel bytes). setuptools' ``sdist`` command does not: it stamps every
tar member with that file's on-disk modification time, so a re-checkout at a
different wall-clock moment produces a different — but content-equivalent —
tarball each time.

This script rewrites an already-built sdist in place: every tar member's
mtime is pinned to ``SOURCE_DATE_EPOCH`` (member order, content, and mode are
left untouched), and the gzip container's own timestamp field is pinned to
the same value. Given the same input tarball built from the same commit, the
output is byte-identical no matter when or where it runs.

Usage:
    SOURCE_DATE_EPOCH=<epoch> python scripts/release_gate/normalize_sdist.py dist/*.tar.gz

Exit status: ``0`` on success, ``1`` if ``SOURCE_DATE_EPOCH`` is unset or a
given path cannot be normalized.
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import sys
import tarfile


def normalize(path: str, epoch: int) -> None:
    with open(path, "rb") as f:
        raw = f.read()

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as src:
        members = src.getmembers()
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as dst:
            for member in members:
                member.mtime = epoch
                # A PAX extended header carries its own string-typed 'mtime'
                # record (needed for the sub-second precision setuptools
                # writes). Clearing it forces the plain integer field above
                # to be used instead — otherwise the stale wall-clock value
                # survives the rewrite untouched.
                member.pax_headers.pop("mtime", None)
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)

    out = io.BytesIO()
    # filename="" + a fixed mtime keeps the 10-byte gzip header identical
    # across runs; reproducible-builds.org recommends pinning it to
    # SOURCE_DATE_EPOCH rather than zero so it stays traceable to the commit.
    with gzip.GzipFile(filename="", mode="wb", mtime=epoch, fileobj=out) as gz:
        gz.write(payload.getvalue())

    with open(path, "wb") as f:
        f.write(out.getvalue())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sdist", nargs="+", help="Path(s) to a built *.tar.gz sdist")
    args = parser.parse_args()

    epoch_raw = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch_raw:
        print(
            "::error::SOURCE_DATE_EPOCH is not set; refusing to normalize non-deterministically",
            file=sys.stderr,
        )
        return 1
    epoch = int(epoch_raw)

    for path in args.sdist:
        normalize(path, epoch)
        print(f"normalized {path} (mtime pinned to SOURCE_DATE_EPOCH={epoch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
