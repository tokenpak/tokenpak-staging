"""Tests for TPT-07: Capsule -> Pak family rename with deprecation aliases.

AC-1: Canonical imports work without DeprecationWarning.
AC-2: Legacy imports emit DeprecationWarning.
"""

from __future__ import annotations

import warnings

import pytest


# ---------------------------------------------------------------------------
# AC-1: Canonical imports produce NO DeprecationWarning
# ---------------------------------------------------------------------------


class TestCanonicalImportsNoWarning:
    """Canonical Pak-family imports must not emit DeprecationWarning."""

    def test_companion_paks_namespace(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.companion.paks import (  # noqa: F401
                HandoffPak,
                MemoryPak,
                Pak,
                PakBuilder,
                PakStore,
            )

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert deprecation_warnings == [], (
            f"Unexpected DeprecationWarning(s): {[str(x.message) for x in deprecation_warnings]}"
        )

    def test_proxy_pak_builder_module(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.proxy.pak_builder import (  # noqa: F401
                PakBuilder,
                make_pak_builder,
            )

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert deprecation_warnings == [], (
            f"Unexpected DeprecationWarning(s): {[str(x.message) for x in deprecation_warnings]}"
        )

    def test_companion_capsules_pakbuilder(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.companion.capsules.builder import PakBuilder  # noqa: F401
            from tokenpak.companion.capsules import PakBuilder as PB2  # noqa: F401

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert deprecation_warnings == [], (
            f"Unexpected DeprecationWarning(s): {[str(x.message) for x in deprecation_warnings]}"
        )

    def test_telemetry_pakbody(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.telemetry.server import PakBody  # noqa: F401

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert deprecation_warnings == [], (
            f"Unexpected DeprecationWarning(s): {[str(x.message) for x in deprecation_warnings]}"
        )

    def test_telemetry_pakresponse(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.telemetry.response_models import PakResponse  # noqa: F401

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert deprecation_warnings == [], (
            f"Unexpected DeprecationWarning(s): {[str(x.message) for x in deprecation_warnings]}"
        )

    def test_telemetry_contextpak(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.telemetry.models import ContextPak  # noqa: F401

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert deprecation_warnings == [], (
            f"Unexpected DeprecationWarning(s): {[str(x.message) for x in deprecation_warnings]}"
        )


# ---------------------------------------------------------------------------
# AC-2: Legacy / deprecated imports emit DeprecationWarning
# ---------------------------------------------------------------------------


class TestLegacyImportsEmitWarning:
    """Legacy Capsule-named imports must emit DeprecationWarning with removal target."""

    def test_capsulebuilder_from_builder_module(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            from tokenpak.companion.capsules import builder

            # Access the deprecated name via getattr
            _cls = getattr(builder, "CapsuleBuilder")

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert len(deprecation_warnings) >= 1
        msg = str(deprecation_warnings[0].message)
        assert "CapsuleBuilder" in msg
        assert "PakBuilder" in msg
        assert "v2.0.0" in msg
        # The alias resolves to the same class
        from tokenpak.companion.capsules.builder import PakBuilder

        assert _cls is PakBuilder

    def test_capsulebuilder_from_capsules_package(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import tokenpak.companion.capsules as capsules_pkg

            _cls = getattr(capsules_pkg, "CapsuleBuilder")

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert len(deprecation_warnings) >= 1
        msg = str(deprecation_warnings[0].message)
        assert "CapsuleBuilder" in msg
        assert "v2.0.0" in msg

    def test_proxy_capsule_builder_module_import(self):
        """Importing the deprecated proxy module emits a module-level warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # Force reimport by removing from sys.modules
            import sys

            sys.modules.pop("tokenpak.proxy.capsule_builder", None)
            import tokenpak.proxy.capsule_builder as deprecated_mod  # noqa: F401

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert len(deprecation_warnings) >= 1
        msg = str(deprecation_warnings[0].message)
        assert "capsule_builder" in msg
        assert "pak_builder" in msg
        assert "v2.0.0" in msg

    def test_capsuleresponse_deprecated(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import tokenpak.telemetry.response_models as rm

            _cls = getattr(rm, "CapsuleResponse")

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert len(deprecation_warnings) >= 1
        msg = str(deprecation_warnings[0].message)
        assert "CapsuleResponse" in msg
        assert "PakResponse" in msg
        assert "v2.0.0" in msg

    def test_contextcapsule_deprecated(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import tokenpak.telemetry.models as tm

            _cls = getattr(tm, "ContextCapsule")

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert len(deprecation_warnings) >= 1
        msg = str(deprecation_warnings[0].message)
        assert "ContextCapsule" in msg
        assert "ContextPak" in msg
        assert "v2.0.0" in msg

    def test_capsulebody_deprecated(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import tokenpak.telemetry.server as srv

            _cls = getattr(srv, "CapsuleBody")

        deprecation_warnings = [
            x for x in w
            if issubclass(x.category, DeprecationWarning)
            and any(kw in str(x.message) for kw in ("Capsule", "capsule", "Pak"))
        ]
        assert len(deprecation_warnings) >= 1
        msg = str(deprecation_warnings[0].message)
        assert "CapsuleBody" in msg
        assert "PakBody" in msg
        assert "v2.0.0" in msg
