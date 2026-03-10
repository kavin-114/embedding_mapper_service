"""Embedding service — loads the sentence-transformer model once and exposes encode()."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.config import Settings


class EmbeddingService:
    """Thin wrapper around sentence-transformers.

    The model is loaded lazily on first call so that import-time stays fast
    (important for tests that mock this layer).
    """

    def __init__(self, settings: "Settings") -> None:
        self._model_name = settings.embedding_model
        self._model = None

    def _load_model(self):
        """Load the sentence-transformer model into memory.

        Called once on first encode().  Subsequent calls are no-ops.
        """
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of texts into embedding vectors.

        Args:
            texts: List of strings to embed.

        Returns:
            List of float vectors, one per input text.
        """
        self._load_model()
        embeddings: np.ndarray = self._model.encode(texts, convert_to_numpy=True)
        return embeddings.tolist()
