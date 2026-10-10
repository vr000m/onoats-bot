"""Pin the ``mlx_audio`` contract the them-channel diarizer depends on.

Local-only, NOT a CI gate (CI is MLX-free): every real-package test skips when
``mlx_audio`` is not importable, unless ``ONOATS_REQUIRE_DIARIZATION_CONTRACT=1``
is set, in which case a missing package is a hard failure (that is how
``scripts/release_check.sh`` runs it). The weights-dependent tests additionally
skip when the model is not in the local HF cache -- the cache is never a
hard-fail, and nothing here downloads.

Run: ``uv run --extra diarization pytest tests/test_diarization_contract.py``

Facts pinned are the Phase 0 results in the dev plan's ``## Findings``
(mlx-audio 0.5.8; valid for ``>=0.5.8,<0.6``, re-verify at each bump).
"""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import sys
import types

import pytest

from onoats.processors import diarization_feeder
from onoats.stt import diarization_model

REQUIRE_ENV = "ONOATS_REQUIRE_DIARIZATION_CONTRACT"
MODEL_ID = "mlx-community/Nemotron-3-Diarization"
PRESETS = {"offline", "low", "very_low", "ultra_low"}


@pytest.fixture(scope="module", autouse=True)
def _purge_mlx_audio_after_module():
    """Importing the real ``mlx_audio`` here must not leak into other test
    modules: several assert ``mlx_audio`` is absent from ``sys.modules`` to pin
    the import-free guarantee, and they pass in isolation."""
    yield
    for name in [
        n for n in sys.modules if n == "mlx_audio" or n.startswith("mlx_audio.")
    ]:
        del sys.modules[name]


# --- always-on (no mlx_audio needed) ---------------------------------------


def test_production_loader_uses_pinned_symbols_and_kwargs(monkeypatch, tmp_path):
    """The loader must call ``mlx_audio.vad.load(<cached snapshot dir>, strict=True)``.

    The snapshot directory (not the bare repo id) is passed so the load neither
    consults the CWD for a shadowing ``mlx-community/...`` directory nor hits
    the hub on every start.
    """
    calls: list[tuple] = []
    sentinel = object()

    def fake_load(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    fake_pkg = types.ModuleType("mlx_audio")
    fake_vad = types.ModuleType("mlx_audio.vad")
    fake_vad.load = fake_load
    fake_pkg.vad = fake_vad
    monkeypatch.setitem(sys.modules, "mlx_audio", fake_pkg)
    monkeypatch.setitem(sys.modules, "mlx_audio.vad", fake_vad)

    snap = tmp_path / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    probed: list[tuple] = []

    def fake_probe(repo_id, filename):
        probed.append((repo_id, filename))
        return str(snap / filename)

    monkeypatch.setattr("huggingface_hub.try_to_load_from_cache", fake_probe)

    assert diarization_model.REPO_ID == MODEL_ID
    assert diarization_feeder.load_mlx_diarizer() is sentinel
    # Every weight file is verified (same check as the startup preflight).
    assert probed == [(MODEL_ID, "config.json"), (MODEL_ID, "model.safetensors")]
    assert calls == [((str(snap),), {"strict": True})]


def test_production_loader_fails_loud_when_weights_not_cached(monkeypatch):
    """No silent fall back to a hub download / bare-repo-id load."""
    monkeypatch.setattr(
        "huggingface_hub.try_to_load_from_cache", lambda repo_id, filename: None
    )
    with pytest.raises(RuntimeError, match="hf download"):
        diarization_feeder.load_mlx_diarizer()


# --- real-package contract --------------------------------------------------


def _import_mlx_vad():
    try:
        return importlib.import_module("mlx_audio.vad")
    except ImportError as exc:
        return exc


@pytest.fixture(scope="module")
def mlx_vad():
    got = _import_mlx_vad()
    if isinstance(got, ImportError):
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(
                f"{REQUIRE_ENV}=1 but mlx_audio is not importable ({got}); "
                "install with `uv sync --extra diarization`",
                pytrace=False,
            )
        pytest.skip("mlx_audio not importable (install the `diarization` extra)")
    return got


def _weights_cached() -> bool:
    from huggingface_hub import try_to_load_from_cache

    for name in ("config.json", "model.safetensors"):
        if not isinstance(try_to_load_from_cache(MODEL_ID, name), str):
            return False
    return True


@pytest.fixture(scope="module")
def model(mlx_vad):
    if not _weights_cached():
        pytest.skip(f"{MODEL_ID} weights not in the local HF cache")
    # The production loader (cached snapshot dir), so the contract pins the
    # exact path the worker takes.
    return diarization_feeder.load_mlx_diarizer()


@pytest.fixture(scope="module")
def model_cls(mlx_vad):
    """The class carrying ``feed`` + ``set_streaming_config`` (no weights)."""
    models = importlib.import_module("mlx_audio.vad.models")
    for info in pkgutil.walk_packages(models.__path__, models.__name__ + "."):
        try:
            mod = importlib.import_module(info.name)
        except Exception:  # pragma: no cover - unrelated model deps
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                obj.__module__ == mod.__name__
                and hasattr(obj, "feed")
                and hasattr(obj, "set_streaming_config")
            ):
                return obj
    pytest.fail("no class with feed() + set_streaming_config() under mlx_audio.vad")


def test_vad_load_exists(mlx_vad):
    assert callable(mlx_vad.load)
    assert "strict" in inspect.signature(mlx_vad.load).parameters or any(
        p.kind is inspect.Parameter.VAR_KEYWORD
        for p in inspect.signature(mlx_vad.load).parameters.values()
    )


def test_feed_signature(model_cls):
    sig = inspect.signature(model_cls.feed)
    params = sig.parameters
    names = [n for n in params if n != "self"]
    assert names[:3] == ["audio", "state", "sample_rate"]
    assert params["sample_rate"].default == 16000
    assert params["final"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["final"].default is False
    for kw in ("threshold", "min_duration", "merge_gap"):
        assert params[kw].kind is inspect.Parameter.KEYWORD_ONLY, kw


def test_streaming_state_has_frames_processed(mlx_vad):
    found = []
    models = importlib.import_module("mlx_audio.vad.models")
    for info in pkgutil.walk_packages(models.__path__, models.__name__ + "."):
        try:
            mod = importlib.import_module(info.name)
        except Exception:  # pragma: no cover
            continue
        cls = getattr(mod, "StreamingState", None)
        if cls is not None:
            found.append(cls)
    assert found, "StreamingState not found under mlx_audio.vad.models"
    assert any(
        "frames_processed" in getattr(c, "__dataclass_fields__", {})
        or hasattr(c, "frames_processed")
        or "frames_processed" in getattr(c, "__annotations__", {})
        for c in found
    )


def test_preset_low_is_chunk9_right_context4_and_all_presets_accepted(model):
    for preset in sorted(PRESETS):
        model.set_streaming_config(preset)
    model.set_streaming_config("low")
    cfg = model.config
    mods = cfg.modules_config
    assert (mods.chunk_len, mods.chunk_right_context) == (9, 4)


def test_unknown_preset_raises_value_error(model):
    with pytest.raises(ValueError):
        model.set_streaming_config("not-a-preset")


def test_feed_rejects_bad_rate_and_2d_input(model):
    import numpy as np

    model.set_streaming_config("low")
    state = model.init_streaming_state()
    with pytest.raises(ValueError):
        model.feed(np.zeros(1600, dtype=np.float32), state, sample_rate=8000)
    with pytest.raises(ValueError):
        model.feed(np.zeros((2, 1600), dtype=np.float32), state)


def test_config_slots_and_one_to_one_frame_rate(model):
    # model.config (NOT the per-call output.num_speakers).
    assert model.config.num_speakers == 8
    # The worker's own load-time guard (exact int 1, not 1.0 / True).
    assert diarization_model.one_row_per_frame(model.config)
    # hop 160 @ 16 kHz == 10 ms per output row.
    assert model._processor_config.hop_length == 160
    assert model.sample_rate == 16000


def test_speaker_probs_rows_equal_frames_processed(model):
    import numpy as np

    model.set_streaming_config("low")
    state = model.init_streaming_state()
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(16000 * 4) * 0.01).astype(np.float32)
    rows = 0
    for i in range(0, len(audio), 1600):
        out, state = model.feed(audio[i : i + 1600], state, sample_rate=16000)
        rows += int(out.speaker_probs.shape[0])
    assert state.frames_processed > 0
    # Output rows are 1:1 with native 10 ms frames: total rows == frames_processed,
    # which is what the worker's anchor + row_offset mapping relies on.
    assert rows == state.frames_processed
