"""Single owner of the diarization model's identity and local-cache probing.

Leaf module: no ``onoats`` imports, and ``huggingface_hub`` is imported lazily
inside the functions, so importing this pulls in nothing heavy. Shared by the
startup preflight (``runtime._preflight_diarization``) and the worker's loader
(``diarization_feeder.load_mlx_diarizer``) so the two cannot disagree on which
repo is the model or which files make its cache complete.

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
