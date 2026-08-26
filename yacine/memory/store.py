"""Memory System — persistent, local, searchable memory with embeddings.

Per the Y.A.C.I.N.E spec the agent must remember previous projects, coding
preferences, frequently used frameworks, completed tasks, failures and
successful fixes, and support vector search.

Design:
  - Memories are stored in a local JSONL file (data/memory.jsonl).
  - An embedder turns text into a fixed-dimension vector. By default we use a
    lightweight hashed bag-of-words embedder (no external deps). If
    sentence-transformers is installed, set settings to use it for real
    semantic vectors. Either way the storage/search interface is identical.
  - Similarity search = cosine similarity, with a keyword boost.

This keeps the system fully offline and dependency-free by default.
"""
from __future__ import annotations

import json
import os
import math
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..core.config import get_settings

EMBED_DIM = 512
_STOP = set("the a an and or of to in on for with as is are be by from at it this that "
            "we you they i he she we our your their not no but if then else when while "
            "def class import return function var let const new do end begin use using".split())


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", text.lower()) if t not in _STOP]


class HashedEmbedder:
    """Deterministic bag-of-words -> hashed vector embedder (offline default)."""

    @staticmethod
    def embed(text: str) -> List[float]:
        vec = [0.0] * EMBED_DIM
        for tok in _tokenize(text):
            h = hash(tok) % EMBED_DIM
            vec[h] += 1.0
        # L2 normalise
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


@dataclass
class Memory:
    id: str
    category: str          # "preference" | "project" | "framework" | "fix" | "failure" | "task" | "fact"
    content: str
    embedding: List[float] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "agent"

    def to_dict(self) -> dict:
        return asdict(self)


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class MemoryStore:
    def __init__(self, data_dir: Optional[Path] = None, embedder=None,
                 logger=None) -> None:
        self.settings = get_settings()
        self.path = Path(data_dir or self.settings.data_dir) / "memory.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder or HashedEmbedder()
        self.logger = logger
        self._lock = threading.Lock()
        self._memories: List[Memory] = []
        self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        with self._lock:
            self._memories = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._memories.append(Memory(**json.loads(line)))
                except Exception:
                    continue

    def _save(self) -> None:
        """Rewrite the whole store ATOMICALLY.

        Opening the real file in "w" truncates it first, so a crash — or a
        concurrent reader, and the API server handles requests on a thread
        pool — during the write window leaves an empty or half-written store.
        Writing to a sibling temp file and renaming makes the swap atomic:
        readers see either the old file or the new one, never a torn one.
        """
        with self._lock:
            self._write_all_locked()

    def _write_all_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for m in self._memories:
                f.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def _append(self, mem: "Memory") -> None:
        """Append one record instead of rewriting the file.

        add() previously re-serialised every memory on every call, which is
        quadratic as the store grows and multiplies the truncate window above
        by the number of writes.
        """
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(mem.to_dict(), ensure_ascii=False) + "\n")

    # -- API --------------------------------------------------------------
    def add(self, content: str, category: str = "fact", tags: Optional[List[str]] = None,
            source: str = "agent") -> Memory:
        mid = f"mem_{uuid.uuid4().hex[:10]}_{int(datetime.now().timestamp())}"
        vec = self.embedder.embed(content)
        mem = Memory(id=mid, category=category, content=content,
                     embedding=vec, tags=tags or [], source=source)
        with self._lock:
            self._memories.append(mem)
        self._append(mem)
        if self.logger:
            self.logger.info("memory", f"+ [{category}] {content[:80]}")
        return mem

    def remember(self, request: str, what: str) -> None:
        """LLM-friendly: remember something about the user/project."""
        self.add(what, category="preference", source="user")

    def search(self, query: str, limit: int = 5, category: Optional[str] = None) -> List[Memory]:
        qvec = self.embedder.embed(query)
        results = []
        qtokens = set(_tokenize(query))
        with self._lock:
            for m in self._memories:
                if category and m.category != category:
                    continue
                sim = _cosine(qvec, m.embedding)
                # keyword boost
                mtokens = set(_tokenize(m.content))
                overlap = len(qtokens & mtokens)
                score = sim + 0.15 * overlap
                results.append((score, m))
        results.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in results[:limit]]

    def recall(self, query: str, limit: int = 5) -> str:
        mems = self.search(query, limit=limit)
        if not mems:
            return "(no relevant memory)"
        return "\n".join(f"- {m.category}: {m.content}" for m in mems)

    def all(self) -> List[Memory]:
        with self._lock:
            return list(self._memories)

    def forget(self, memory_id: str) -> bool:
        with self._lock:
            before = len(self._memories)
            self._memories = [m for m in self._memories if m.id != memory_id]
            removed = len(self._memories) != before
        if removed:
            self._save()
        return removed

    def clear(self) -> None:
        with self._lock:
            self._memories = []
        self._save()
