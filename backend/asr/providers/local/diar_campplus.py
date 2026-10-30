"""[4] Speaker diarization with CAM++ embeddings (funasr) and funasr's clustering backend.

Speech (from VAD) is cut into 1.5 s windows with a 0.75 s shift (``sv_chunk``), each window gets a
192-d CAM++ embedding, ``ClusterBackend`` clusters them (spectral clustering below 2048 windows,
k-means when the speaker count is known, UMAP + HDBSCAN otherwise; clusters closer than 0.78 are
merged) and ``postprocess`` merges / smooths the labelled windows into speaker turns. This is the
same chain funasr's own ``spk_model="cam++"`` pipeline runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from asr.providers.local.models import model_dir

SAMPLE_RATE = 16_000
EMBED_BATCH = 64


@dataclass(frozen=True)
class Turn:
    start: float  # seconds
    end: float
    label: int  # 0-based, in order of first appearance


def order_labels(turns: Sequence[Turn]) -> list[Turn]:
    """Renumber labels by first appearance so SPEAKER_00 is whoever speaks first."""
    mapping: dict[int, int] = {}
    for t in sorted(turns, key=lambda t: t.start):
        mapping.setdefault(t.label, len(mapping))
    return [Turn(t.start, t.end, mapping[t.label]) for t in sorted(turns, key=lambda t: t.start)]


MIN_CLUSTER_SHARE = 0.03  # clusters with < 3 % of the speech windows are folded into others
MIN_CLUSTER_WINDOWS = 20  # ... and, on longer recordings, with fewer than 20 windows (15 s) ...
MIN_CLUSTER_FLOOR_SHARE = 0.05  # ... but that floor never exceeds 5 % of the windows


def fold_small_clusters(
    embeddings: np.ndarray,
    labels: np.ndarray,
    *,
    min_share: float = MIN_CLUSTER_SHARE,
    min_windows: int = MIN_CLUSTER_WINDOWS,
    floor_share: float = MIN_CLUSTER_FLOOR_SHARE,
) -> np.ndarray:
    """Reassign windows of tiny clusters (laughter, overlap, crosstalk) to the nearest kept cluster.

    A cluster is kept when it holds at least ``max(min_share * N, min(min_windows, floor_share *
    N))`` windows (the largest cluster is always kept). The absolute floor of ``min_windows``
    applies to long recordings only: on a 3-minute call (N ≈ 240) it would fold a speaker with
    10 s of speech, so it is capped at ``floor_share`` of the windows (12 here). Nearest =
    highest cosine similarity to a kept centroid.
    Negative labels (HDBSCAN's ``-1`` outliers) are never a cluster: those windows are always
    reassigned, and if every window is an outlier they all become cluster 0.
    """
    labels = np.asarray(labels).copy()
    valid = labels >= 0
    if not valid.any():
        return np.zeros_like(labels)
    ids, counts = np.unique(labels[valid], return_counts=True)
    n = len(labels)
    threshold = max(min_share * n, min(min_windows, floor_share * n))
    keep = [i for i, c in zip(ids, counts, strict=True) if c >= threshold] or [
        ids[np.argmax(counts)]
    ]
    if len(keep) == len(ids) and valid.all():
        return labels
    unit = embeddings / np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-8)
    centroids = np.stack([unit[labels == k].mean(0) for k in keep])
    centroids /= np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-8)
    small = ~np.isin(labels, keep)
    labels[small] = np.asarray(keep)[np.argmax(unit[small] @ centroids.T, axis=1)]
    return labels


def reassign_outliers(embeddings: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """Only the outlier part of ``fold_small_clusters``: every real cluster is kept."""
    return fold_small_clusters(embeddings, labels, min_share=0.0, min_windows=0, floor_share=0.0)


class CamPlusDiarizer:
    key = "campplus"

    def __init__(self, cache_dir: Path, *, device: str = "cpu") -> None:
        from funasr import AutoModel
        from funasr.models.campplus.cluster_backend import ClusterBackend

        self._model = AutoModel(
            model=str(model_dir(cache_dir, self.key)),
            device=device,
            disable_update=True,
            disable_pbar=True,
            log_level="ERROR",
        )
        self._cluster = ClusterBackend()

    def embed(self, windows: Sequence[np.ndarray]) -> np.ndarray:
        import torch

        out: list[torch.Tensor] = []
        for i in range(0, len(windows), EMBED_BATCH):
            batch = [w.astype(np.float32) for w in windows[i : i + EMBED_BATCH]]
            res = self._model.generate(input=batch, fs=SAMPLE_RATE, batch_size=len(batch))
            # funasr returns one result per batch holding a (batch, 192) embedding matrix.
            out.extend(
                r["spk_embedding"].reshape(-1, r["spk_embedding"].shape[-1]).float().cpu()
                for r in res
            )
        emb = torch.cat(out, dim=0).numpy() if out else np.zeros((0, 192), np.float32)
        if len(emb) != len(windows):
            raise RuntimeError(f"CAM++ returned {len(emb)} embeddings for {len(windows)} windows")
        return emb

    def diarize(
        self,
        audio: np.ndarray,
        speech: Sequence[tuple[float, float]],
        *,
        num_speakers: int | None = None,
    ) -> list[Turn]:
        import torch
        from funasr.models.campplus.utils import postprocess, sv_chunk

        vad_segments = [
            [s, e, audio[int(s * SAMPLE_RATE) : int(e * SAMPLE_RATE)]]
            for s, e in speech
            if e - s > 0.05
        ]
        if not vad_segments:
            return []
        windows = sv_chunk(vad_segments)
        embeddings = self.embed([w[2] for w in windows])
        if len(embeddings) == 0:
            return []
        params = {"oracle_num": num_speakers} if num_speakers else {}
        labels = np.asarray(self._cluster(torch.from_numpy(embeddings), **params))
        if not num_speakers:
            labels = fold_small_clusters(embeddings, labels)
        elif (labels < 0).any():
            labels = reassign_outliers(embeddings, labels)
        merged = postprocess(windows, None, labels, embeddings)
        return order_labels([Turn(float(s), float(e), int(k)) for s, e, k in merged])
