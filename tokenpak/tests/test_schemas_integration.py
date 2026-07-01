"""Tests for TokenPak schema definitions.

Covers: core/schemas/artifact.py, core/schemas/chunk.py — schema validation, serialization.
"""

from dataclasses import asdict, is_dataclass

import pytest

from tokenpak.core.schemas.artifact import ArtifactSchema
from tokenpak.core.schemas.chunk import ChunkSchema


class TestSchemaStructures:
    """Test: Schema dataclass definitions and structure."""

    def test_artifact_schema_exists(self):
        """ArtifactSchema is defined as a dataclass."""
        assert is_dataclass(ArtifactSchema)

    def test_chunk_schema_exists(self):
        """ChunkSchema is defined as a dataclass."""
        assert is_dataclass(ChunkSchema)

    def test_schema_serialization(self):
        """Schemas can be serialized to dict via to_dict()."""
        artifact = ArtifactSchema(
            id="test-1",
            session_id="sess-1",
            origin="code_dump",
            kind="code",
            content_ref="/tmp/test",
        )
        data = artifact.to_dict()
        assert isinstance(data, dict)
        assert data["id"] == "test-1"


class TestSchemaValidation:
    """Test: Schema field validation and constraints."""

    def test_artifact_required_fields(self):
        """ArtifactSchema requires id, session_id, origin, kind, content_ref."""
        with pytest.raises(TypeError):
            ArtifactSchema()

    def test_chunk_required_fields(self):
        """ChunkSchema requires id, source, content, token_estimate."""
        with pytest.raises(TypeError):
            ChunkSchema()


class TestSchemaInteroperability:
    """Test: Schema compatibility and composition."""

    def test_schemas_can_be_composed(self):
        """Schemas can be instantiated and collected."""
        artifacts = [
            ArtifactSchema(
                id=f"a{i}",
                session_id="sess-1",
                origin="tool_output",
                kind="markdown",
                content_ref=f"/tmp/content_{i}",
            )
            for i in range(3)
        ]
        assert len(artifacts) == 3
