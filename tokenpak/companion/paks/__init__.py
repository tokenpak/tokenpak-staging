"""
tokenpak.companion.paks
=======================

Convenience namespace for the Pak family of types (TPT-07, 2026-05-12).

Re-exports canonical Pak types from their home modules so consumers can do::

    from tokenpak.companion.paks import Pak, PakBuilder, PakStore
"""

from __future__ import annotations

from typing import TypeAlias

from tokenpak.companion.capsules.builder import PakBuilder
from tokenpak.companion.recall.store import RecallStore as PakStore
from tokenpak.tip.pak import Pak

MemoryPak: TypeAlias = Pak
HandoffPak: TypeAlias = Pak

__all__ = ["Pak", "MemoryPak", "HandoffPak", "PakBuilder", "PakStore"]
