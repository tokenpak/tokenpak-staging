# SPDX-License-Identifier: Apache-2.0
"""Tests for the PromptPacker / PromptPackingService / PromptPackingResult surface."""

from __future__ import annotations

import importlib

import pytest

from tokenpak.compression.prompt_packing import (
    CompressionMetadata,
    PackingPolicy,
    PromptPacker,
    PromptPackingResult,
    PromptPackingService,
)
from tokenpak.tip.pak import Pak


class TestPublicImportPath:
    """AC-1 + AC-4d: canonical import path works without warning."""

    def test_import_from_compression_package(self):
        from tokenpak.compression import (
            PromptPacker,
            PromptPackingResult,
            PromptPackingService,
        )

        assert PromptPacker is PromptPackingService

    def test_no_deprecation_warning(self):
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            importlib.import_module("tokenpak.compression.prompt_packing")
        deprecation_msgs = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecation_msgs == [], [str(w.message) for w in deprecation_msgs]


class TestPromptPackingServiceEndToEnd:
    """AC-2 + AC-4a: end-to-end pack call returns PromptPackingResult with Pak."""

    def test_pack_string_prompt(self):
        svc = PromptPackingService()
        result = svc.pack("hello world", policy=None)

        assert isinstance(result, PromptPackingResult)
        assert isinstance(result.pak, Pak)
        assert isinstance(result.text, str)
        assert len(result.text) > 0

    def test_pack_messages_list(self):
        svc = PromptPackingService()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Summarize this."},
        ]
        result = svc.pack(messages, policy=None)

        assert isinstance(result, PromptPackingResult)
        assert isinstance(result.pak, Pak)

    def test_pack_with_policy(self):
        policy = PackingPolicy(budget=4096, project="test-project", topic="testing")
        svc = PromptPackingService()
        result = svc.pack("test prompt", policy=policy)

        assert isinstance(result, PromptPackingResult)
        assert result.pak.scope.project == "test-project"
        assert result.pak.scope.topic == "testing"


class TestPromptPackingResultContract:
    """AC-2 + AC-4b: result type assertions."""

    @pytest.fixture()
    def result(self) -> PromptPackingResult:
        return PromptPackingService().pack("test input")

    def test_pak_is_pak_instance(self, result: PromptPackingResult):
        assert isinstance(result.pak, Pak)

    def test_pak_type_is_recall(self, result: PromptPackingResult):
        from tokenpak.tip.pak import PakSubtype

        assert result.pak.pak_type == PakSubtype.RECALL

    def test_pak_id_prefixed(self, result: PromptPackingResult):
        assert result.pak.pak_id.startswith("ppack-")

    def test_pak_source_platform(self, result: PromptPackingResult):
        assert result.pak.source.platform == "tokenpak"

    def test_pak_has_source_hash(self, result: PromptPackingResult):
        assert len(result.pak.source.source_hash) == 64

    def test_pak_roundtrips_to_dict(self, result: PromptPackingResult):
        d = result.pak.to_dict()
        restored = Pak.from_dict(d)
        assert restored.pak_id == result.pak.pak_id


class TestCompressionMetadata:
    """AC-4c: compression metadata is present and populated."""

    @pytest.fixture()
    def result(self) -> PromptPackingResult:
        return PromptPackingService().pack("Some text that should be processed.")

    def test_metadata_type(self, result: PromptPackingResult):
        assert isinstance(result.compression_metadata, CompressionMetadata)

    def test_metadata_has_token_counts(self, result: PromptPackingResult):
        m = result.compression_metadata
        assert m.tokens_raw >= 0
        assert m.tokens_after >= 0
        assert m.tokens_saved >= 0

    def test_metadata_has_duration(self, result: PromptPackingResult):
        assert result.compression_metadata.duration_ms >= 0

    def test_metadata_has_stages(self, result: PromptPackingResult):
        assert isinstance(result.compression_metadata.stages_run, list)

    def test_metadata_has_pipeline_result(self, result: PromptPackingResult):
        from tokenpak.compression.pipeline import PipelineResult

        assert isinstance(result.compression_metadata.pipeline_result, PipelineResult)


class TestPromptPackerAlias:
    """PromptPacker is a convenience alias for PromptPackingService."""

    def test_alias_identity(self):
        assert PromptPacker is PromptPackingService

    def test_alias_works(self):
        packer = PromptPacker()
        result = packer.pack("test")
        assert isinstance(result, PromptPackingResult)


