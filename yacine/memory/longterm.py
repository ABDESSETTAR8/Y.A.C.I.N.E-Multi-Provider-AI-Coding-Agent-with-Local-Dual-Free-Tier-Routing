"""Long-term memory (Phase F3).

Extends the base MemoryStore with:
  - A pluggable embedder: defaults to the dependency-free HashedEmbedder, but
    automatically uses sentence-transformers when installed for real semantic
    vectors (config: set `embedder: sentence-transformers` or just have the
    package present).
  - Memory consolidation: a periodic job that summarizes old, similar memories
    into a single "consolidated" memory to bound growth (long-term memory
    improvement per the spec).

All offline-safe: nothing breaks if sentence-transformers is absent.
"""
from __future__ import annotations

import datetime
import importlib
from typing import List, Optional

from .store import MemoryStore, Memory, HashedEmbedder, EMBED_DIM


class STEmbedder:
    """Wrapper around sentence-transformers when available."""
    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        mod = importlib.import_module("sentence_transformers")
        self._model = mod.SentenceTransformer(model_name)

    def embed(self, text: str) -> List[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()


def make_embedder(prefer: str = "auto") -> object:
    """Return an embedder object with .embed(text)->list[float].

    prefer: 'auto' (ST if installed else hashed), 'st' (force sentence-transformers),
    'hashed' (force offline)."""
    if prefer == "hashed":
        return HashedEmbedder()
    if prefer in ("auto", "st"):
        try:
            return STEmbedder()
        except Exception:
            if prefer == "st":
                raise
            return HashedEmbedder()
    return HashedEmbedder()


class LongTermMemory(MemoryStore):
    """MemoryStore with optional real embeddings + consolidation."""

    def __init__(self, data_dir=None, embedder_pref: str = "auto",
                 logger=None, consolidation_every: int = 50) -> None:
        super().__init__(data_dir=data_dir, logger=logger)
        self.embedder = make_embedder(embedder_pref)
        self.embedder_pref = embedder_pref
        self._since_consolidation = 0
        self.consolidation_every = consolidation_every

    def add(self, content: str, category: str = "fact", tags=None, source: str = "agent") -> Memory:
        # Re-embed with the (possibly better) embedder.
        mem = super().add(content, category=category, tags=tags, source=source)
        # Overwrite the hashed embedding with the real one if different.
        try:
            mem.embedding = self.embedder.embed(content)
            self._save()
        except Exception:
            pass
        self._since_consolidation += 1
        if self._since_consolidation >= self.consolidation_every:
            self.consolidate()
        return mem

    def consolidate(self) -> int:
        """Merge near-duplicate 'fact' memories into consolidated summaries.

        Returns the number of memories removed."""
        facts = [m for m in self._memories if m.category == "fact"]
        removed = 0
        i = 0
        while i < len(facts):
            j = i + 1
            while j < len(facts):
                # crude similarity via shared tokens
                a = set(memory_tokens(facts[i].content))
                b = set(memory_tokens(facts[j].content))
                overlap = len(a & b) / max(1, len(a | b))
                if overlap > 0.6:
                    facts[i].content += " | " + facts[j].content
                    self._memories.remove(facts[j])
                    facts.pop(j)
                    removed += 1
                else:
                    j += 1
            i += 1
        if removed:
            self._save()
            if self.logger:
                self.logger.info("memory", f"Consolidated {removed} memories")
        self._since_consolidation = 0
        return removed


def memory_tokens(text: str) -> List[str]:
    import re
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2]
