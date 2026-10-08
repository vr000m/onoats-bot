"""``onoats.stt.diarization_model``: the single owner of the diarization model
identity and its local-cache probe, shared by the startup preflight and the
worker's loader."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from onoats.processors import diarization_feeder
from onoats.stt import diarization_model as dm


def _cache(monkeypatch, cached: dict[str, str]) -> list[tuple[str, str]]:
    probed: list[tuple[str, str]] = []

    def _probe(repo_id, filename):
        probed.append((repo_id, filename))
        return cached.get(filename)

    monkeypatch.setattr("huggingface_hub.try_to_load_from_cache", _probe)
    return probed


def test_leaf_module_imports_no_onoats_or_mlx():
    src = Path(dm.__file__).read_text()
    for node in ast.walk(ast.parse(src)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, "relative import in a leaf module"
            names = [node.module or ""]
        for n in names:
            assert not n.startswith("onoats"), n
            assert "mlx" not in n, n


def test_import_pulls_nothing_heavy():
    code = (
        "import sys, onoats.stt.diarization_model;"
        "bad = [m for m in ('huggingface_hub', 'mlx_audio', 'pipecat', 'numpy')"
        " if m in sys.modules];"
        "assert not bad, bad"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "cached",
    [{}, {"config.json": "/c/snap/config.json"}, {"model.safetensors": "/c/s/m"}],
)
def test_partial_cache_is_a_miss_for_both_preflight_and_loader(monkeypatch, cached):
    """The loader's snapshot lookup requires every weight file, exactly like
    the preflight probe, so the two cannot disagree on "cached"."""
    _cache(monkeypatch, cached)
    assert dm.weights_cached() is False
    with pytest.raises(RuntimeError, match=f"hf download {dm.REPO_ID}"):
        dm.cached_snapshot_dir()


def test_loader_fails_on_partial_cache_before_importing_mlx_audio(monkeypatch):
    _cache(monkeypatch, {"config.json": "/c/snap/config.json"})
    # A None entry makes ``import mlx_audio.vad`` raise ImportError, so a
    # RuntimeError proves the cache check ran first.
    monkeypatch.setitem(sys.modules, "mlx_audio", None)
    monkeypatch.setitem(sys.modules, "mlx_audio.vad", None)
    with pytest.raises(RuntimeError, match="hf download"):
        diarization_feeder.load_mlx_diarizer()


def test_full_cache_probes_every_file_and_returns_unresolved_snapshot_dir(
    monkeypatch, tmp_path
):
    blob = tmp_path / "blobs" / "abc"
    blob.parent.mkdir()
    blob.write_text("{}")
    snap = tmp_path / "snapshots" / "rev"
    snap.mkdir(parents=True)
    (snap / "config.json").symlink_to(blob)
    probed = _cache(
        monkeypatch,
        {f: str(snap / f) for f in dm.WEIGHT_FILES},
    )
    assert dm.weights_cached() is True
    # abspath, not resolve(): the symlink must not walk out into blobs/.
    assert dm.cached_snapshot_dir() == str(snap)
    assert [f for _, f in probed] == list(dm.WEIGHT_FILES) * 2
    assert {r for r, _ in probed} == {dm.REPO_ID}
