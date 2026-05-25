"""
tokenpak.companion.capsules
===========================

Context-block Pak compression for the TokenPak proxy pipeline.

The Pak builder compresses verbose historical message blocks into compact,
structured Paks before the request is forwarded to the upstream model.

Feature flag: ``TOKENPAK_CAPSULE_BUILDER=1`` (env var) enables the builder.
"""

from .builder import PakBuilder  # noqa: F401

__all__ = ["PakBuilder"]


def __getattr__(name: str):
    if name == "CapsuleBuilder":
        import warnings

        warnings.warn(
            "CapsuleBuilder is deprecated; use PakBuilder. Removal target: v2.0.0",
            DeprecationWarning,
            stacklevel=2,
        )
        return PakBuilder
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
