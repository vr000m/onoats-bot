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
    # The worker's own load-time guard (exact ints: subsampling 1, hop 160 @
    # 16 kHz == 10 ms per output row; not 1.0 / True / 160.0).
    assert diarization_model.one_row_per_frame(model.config)
    # The guard reads the public config field; pin that the model's feed()
    # really uses that same processor config (a private alias today).
    assert model._processor_config is model.config.processor_config
    assert model.sample_rate == 16000


#: Whole 160-sample frames fed by the row-rate check (4 s, well past the
#: ``low`` preset's lookahead, so most rows arrive before the final flush).
_ROW_CHECK_FRAMES = 400


def _assert_one_row_per_frame_fed(model, chunk_samples: int) -> None:
    """Feed whole frames exactly as the worker does and count the rows.

    Mirrors ``diarization_feeder._Worker._run_inner``: ``feed(chunk, state,
    sample_rate=16000)`` per chunk, each chunk's rows mapped from the
    ``frames_processed`` read BEFORE the call, then one
    ``feed(<empty>, state, final=True)`` to drain the lookahead. The worker
    maps row ``r`` to input samples ``[r*160, r*160+160)``, so ``N`` whole
    frames must yield exactly ``N`` rows - counted from the emitted
    ``speaker_probs``, NOT from ``frames_processed`` (the model owns both, so
    comparing them alone cannot catch a model that emits rows at another
    rate) - and every call's rows must start where the previous ones ended.
    """
    import numpy as np

    frame = diarization_model.ROW_HOP_SAMPLES
    model.set_streaming_config("low")
    state = model.init_streaming_state()
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal(_ROW_CHECK_FRAMES * frame) * 0.01).astype(np.float32)
    rows = 0
    chunks = [audio[i : i + chunk_samples] for i in range(0, len(audio), chunk_samples)]
    for chunk, final in [(c, False) for c in chunks] + [
        (np.zeros(0, dtype=np.float32), True)
    ]:
        idx = int(state.frames_processed)  # the worker's frame_index()
        out, state = model.feed(chunk, state, sample_rate=16000, final=final)
        assert idx == rows, "a feed's rows do not start where the last ones ended"
        rows += int(out.speaker_probs.shape[0])
    assert rows == _ROW_CHECK_FRAMES, (
        f"{rows} rows for {_ROW_CHECK_FRAMES} whole {frame}-sample frames"
    )
    assert int(state.frames_processed) == rows


@pytest.mark.parametrize("chunk_samples", [160, 1600], ids=["10ms", "100ms"])
def test_one_speaker_probs_row_per_160_samples_fed(model, chunk_samples):
    """The worker's whole timeline assumes one row per 10 ms of audio fed."""
    _assert_one_row_per_frame_fed(model, chunk_samples)


class _StubStreamingModel:
    """Weights-free stand-in: ``rows_per_frame`` rows per 160 samples, with a
    lookahead released on ``final`` and ``frames_processed`` bumped per row
    (so total rows == frames_processed whatever the rate)."""

    def __init__(self, rows_per_frame: int, lookahead_frames: int = 40) -> None:
        self.rows_per_frame = rows_per_frame
        self.lookahead = lookahead_frames

    def set_streaming_config(self, preset):
        assert preset == "low"

    def init_streaming_state(self):
        return types.SimpleNamespace(frames_processed=0, fed=0)

    def feed(self, audio, state, sample_rate=16000, *, final=False):
        import numpy as np

        state.fed += len(audio)
        frames = (
            state.fed // 160 if final else max(0, state.fed // 160 - self.lookahead)
        )
        due = frames * self.rows_per_frame - state.frames_processed
        state.frames_processed += due
        return types.SimpleNamespace(speaker_probs=np.zeros((due, 8))), state


def test_row_rate_check_rejects_a_model_emitting_rows_at_another_rate():
    """Regression: the check compared emitted rows only with the model's own
    ``frames_processed``, so a model emitting two rows per 160 samples (and
    counting both) passed while the worker laid every label at the wrong time.
    Weights-free, so it runs in CI: the stub passes the old equality yet fails
    the samples-fed count."""
    import numpy as np

    _assert_one_row_per_frame_fed(_StubStreamingModel(1), 160)
    doubled = _StubStreamingModel(2)
    # The old check (total rows == frames_processed) is blind to the rate.
    state = doubled.init_streaming_state()
    out, state = doubled.feed(np.zeros(16000, np.float32), state, final=True)
    assert out.speaker_probs.shape[0] == state.frames_processed == 200
    with pytest.raises(AssertionError, match="rows for 400 whole"):
        _assert_one_row_per_frame_fed(doubled, 160)
