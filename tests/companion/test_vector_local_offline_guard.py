"""Regression: the local embedding model in
``tokenpak.vault.retrieval.vector_local`` loads **offline-only by default**.

Background
----------
``LocalVectorRetriever._ensure_model()`` constructs a sentence-transformers
model. By default that backend (via huggingface_hub) will silently fetch a
missing model from the Hub at runtime — a network egress path the offline-first
product does not otherwise take. The guard makes the load offline-only by
default: a missing model FAILS CLOSED (no download, graceful empty results,
actionable operator message) unless the operator explicitly opts in via the
``TOKENPAK_ALLOW_MODEL_DOWNLOAD`` env flag.

These tests inject a fake ``sentence_transformers`` backend so the load *wiring*
is exercised deterministically, without paying the real ~13s torch import. The
fake records whether it was constructed under offline mode, so we can assert the
guard is active on a cold miss with no real network access.
"""
from __future__ import annotations

import os

import pytest

import tokenpak.vault.retrieval.vector_local as vl


class _RecordingFakeST:
    """Fake SentenceTransformer that records the env it was constructed under.

    Simulates a backend that honours offline mode: when offline env vars are
    set it raises (as the real backend does for a model that is not cached
    locally), proving no download is attempted.
    """

    instances: list = []

    def __init__(self, model_name, *args, **kwargs):
        offline = (
            os.environ.get("HF_HUB_OFFLINE") == "1"
            or os.environ.get("TRANSFORMERS_OFFLINE") == "1"
            or kwargs.get("local_files_only") is True
        )
        _RecordingFakeST.instances.append(
            {"model_name": model_name, "offline": offline, "kwargs": kwargs}
        )
        if offline:
            # Real backends raise here when the model is not cached locally and
            # offline mode forbids fetching it.
            raise OSError(
                f"{model_name} is not a local folder and offline mode is on"
            )


@pytest.fixture
def fake_st_backend(monkeypatch):
    """Install the recording fake backend into vector_local."""
    _RecordingFakeST.instances = []
    monkeypatch.setattr(vl, "SentenceTransformer", _RecordingFakeST)
    monkeypatch.setattr(vl, "_load_sentence_transformer", lambda: _RecordingFakeST)
    monkeypatch.setattr(vl, "_ST_AVAILABLE", True)
    monkeypatch.setattr(vl, "_NP_AVAILABLE", True)
    # Ensure no ambient opt-in from the host environment.
    monkeypatch.delenv(vl.ALLOW_MODEL_DOWNLOAD_ENV, raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    return _RecordingFakeST


def test_cold_miss_fails_closed_offline_no_egress(fake_st_backend):
    """With no opt-in and the model absent locally, the load is attempted in
    OFFLINE mode (no network egress) and fails closed (returns False)."""
    retriever = vl.LocalVectorRetriever(model_name="missing-model-xyz")

    ok = retriever._ensure_model()

    assert ok is False, "cold miss must fail closed, not silently degrade to a download"
    assert fake_st_backend.instances, "model construction should have been attempted"
    # Every construction attempt on a cold miss must have been offline.
    assert all(rec["offline"] for rec in fake_st_backend.instances), (
        "model load on a cold miss attempted a NON-offline (network-capable) "
        f"construction: {fake_st_backend.instances!r}"
    )
    # Retriever degrades cleanly rather than holding a half-loaded model.
    assert retriever._model is None
    assert retriever.is_available() is False


def test_offline_env_restored_after_failed_load(fake_st_backend):
    """The offline guard must not leak its env mutation past the load."""
    before = {
        "HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
        "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE"),
    }
    retriever = vl.LocalVectorRetriever(model_name="missing-model-xyz")
    retriever._ensure_model()
    assert os.environ.get("HF_HUB_OFFLINE") == before["HF_HUB_OFFLINE"]
    assert os.environ.get("TRANSFORMERS_OFFLINE") == before["TRANSFORMERS_OFFLINE"]


def test_explicit_opt_in_permits_non_offline_load(fake_st_backend, monkeypatch):
    """With the explicit opt-in env set, the load is NOT forced offline,
    restoring the prior download-capable behaviour for users who want it."""
    monkeypatch.setenv(vl.ALLOW_MODEL_DOWNLOAD_ENV, "1")
    retriever = vl.LocalVectorRetriever(model_name="some-model")

    ok = retriever._ensure_model()

    assert ok is True, "opt-in should allow the (fake) load to succeed"
    assert fake_st_backend.instances, "model construction should have been attempted"
    assert all(not rec["offline"] for rec in fake_st_backend.instances), (
        "explicit opt-in must NOT force offline mode: "
        f"{fake_st_backend.instances!r}"
    )
    assert retriever._model is not None


def test_model_present_locally_load_succeeds_offline(fake_st_backend, monkeypatch):
    """When the model IS available locally, an offline load succeeds and
    existing retrieval behaviour is unchanged.

    A backend that does not raise under offline mode models the
    'cached-locally' case: the construction is still offline (no egress) but
    succeeds.
    """

    class _CachedFakeST:
        last = {}

        def __init__(self, model_name, *args, **kwargs):
            _CachedFakeST.last = {
                "model_name": model_name,
                "offline": os.environ.get("HF_HUB_OFFLINE") == "1"
                or kwargs.get("local_files_only") is True,
            }

    monkeypatch.setattr(vl, "SentenceTransformer", _CachedFakeST)
    monkeypatch.setattr(vl, "_load_sentence_transformer", lambda: _CachedFakeST)
    retriever = vl.LocalVectorRetriever(model_name="cached-model")

    ok = retriever._ensure_model()

    assert ok is True
    assert retriever._model is not None
    assert _CachedFakeST.last["offline"] is True, (
        "a present-locally model should still be loaded offline by default"
    )


def test_download_flag_truthy_values(monkeypatch):
    """The opt-in flag parsing matches the project's truthy convention."""
    for truthy in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv(vl.ALLOW_MODEL_DOWNLOAD_ENV, truthy)
        assert vl._model_download_allowed() is True, truthy
    for falsy in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv(vl.ALLOW_MODEL_DOWNLOAD_ENV, falsy)
        assert vl._model_download_allowed() is False, falsy
