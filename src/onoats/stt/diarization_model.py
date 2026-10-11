"""Single owner of the diarization model's identity and local-cache probing.

Leaf module: no ``onoats`` imports, and ``huggingface_hub`` is imported lazily
inside the functions, so importing this pulls in nothing heavy. Shared by the
startup preflight (``runtime._preflight_diarization``) and the worker's loader
(``diarization_feeder.load_mlx_diarizer``) so the two cannot disagree on which
repo is the model or which files make its cache complete. Also owns
:func:`one_row_per_frame`, the frame-rate predicate (output subsampling AND
processor hop) shared by the worker's load-time guard and the release-time
contract test.

Neither function touches the network: ``try_to_load_from_cache`` returns a path
str when a file is cached and ``None`` (or a sentinel for a known-missing file)
otherwise. Deliberately NOT ``snapshot_download(local_files_only=True)``, which
raises ``IncompleteSnapshotError`` on a fully cached repo.
"""

from __future__ import annotations

REPO_ID = "mlx-community/Nemotron-3-Diarization"
#: The files ``mlx_audio``'s loader reads (Phase 0); all must be cached.
WEIGHT_FILES = ("config.json", "model.safetensors")
DOWNLOAD_HINT = f"hf download {REPO_ID}"
#: Input samples per output row the timeline assumes (10 ms at 16 kHz). A copy
#: of ``diarization_timeline.FRAME_SAMPLES`` (this leaf cannot import it),
#: pinned equal by ``tests/test_diarization_model.py``.
ROW_HOP_SAMPLES = 160


def row_hop_length(config: object) -> object:
    """The model's processor hop (``config.processor_config.hop_length``), or ``None``.

    The public ``ModelConfig`` field; the model's own ``_processor_config`` is
    the same object.
    """
    return getattr(getattr(config, "processor_config", None), "hop_length", None)


def one_row_per_frame(config: object) -> bool:
    """True only if the model emits ONE output row per 10 ms input frame.

    A row spans ``hop_length * output_subsampling_factor`` input samples, so
    both factors must be pinned: ``output_subsampling_factor`` exactly 1 and
    the processor hop exactly :data:`ROW_HOP_SAMPLES`. The single predicate
    for both the worker's load-time guard and the release-time contract test.
    Fails closed: mlx-audio's ``ModelConfig`` always carries both as ``int``
    (defaults 1 and 160), so a missing field, a float (``1.0``), a bool
    (``True == 1``) or a str is an unsupported model, never a verified value.
    """
    factor = getattr(config, "output_subsampling_factor", None)
    hop = row_hop_length(config)
    return (
        type(factor) is int
        and factor == 1
        and type(hop) is int
        and hop == ROW_HOP_SAMPLES
    )


def _cached_paths(repo_id: str) -> list[str] | None:
    """Cached path of every weight file in order; ``None`` at the first miss."""
    from huggingface_hub import try_to_load_from_cache

    paths: list[str] = []
    for filename in WEIGHT_FILES:
        path = try_to_load_from_cache(repo_id, filename)
        if not isinstance(path, str):
            return None
        paths.append(path)
    return paths


def weights_cached(repo_id: str = REPO_ID) -> bool:
    """Non-downloading local-cache probe for every weight file."""
    return _cached_paths(repo_id) is not None


def cached_snapshot_dir(repo_id: str = REPO_ID) -> str:
    """Absolute path of the locally cached model snapshot (no network).

    ``mlx_audio.vad.load`` given a bare repo id first tests it as a path
    relative to the CWD (a directory of that name would shadow the model) and
    otherwise calls ``snapshot_download``, i.e. checks the hub on every start.
    Loading from the resolved cache directory avoids both. Requires every
    weight file (the same check as :func:`weights_cached`); a miss raises
    ``RuntimeError`` naming the download command.
    """
    import os

    paths = _cached_paths(repo_id)
    if paths is None:
        raise RuntimeError(
            f"diarization weights ({repo_id}) are not in the local "
            "Hugging Face cache; run: hf download " + repo_id
        )
    # abspath, not resolve(): the files inside a snapshot are symlinks into
    # ``blobs/``, and resolving would walk out of the snapshot directory.
    return os.path.dirname(os.path.abspath(paths[0]))


def provenance(repo_id: str = REPO_ID) -> dict[str, str]:
    """Header provenance for the locally cached model: ``{"model", "version"}``.

    ``version`` is the Hugging Face snapshot revision (the cached snapshot
    directory's name), read from the local cache only - no network and no
    ``mlx_audio`` import. If the cache cannot be resolved the key is omitted
    rather than invented; the startup preflight normally guarantees it exists.
    """
    import os

    info = {"model": repo_id}
    try:
        info["version"] = os.path.basename(cached_snapshot_dir(repo_id))
    except RuntimeError:
        pass
    return info
