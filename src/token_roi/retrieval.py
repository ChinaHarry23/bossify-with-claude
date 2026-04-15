"""Retrieval layer.

A hybrid search over:
    1. Compressed memory topic files  (data/memory/topics/)
    2. Raw event text                  (user prompts + assistant messages)

Two scoring components, combined late:

    score = alpha * cosine(emb(q), emb(d)) + (1 - alpha) * bm25ish(q, d)

`alpha` defaults to 0.6. Embeddings are favored because local models produce
shallow-but-consistent semantic rankings, while our BM25-ish scorer handles
exact-match situations embeddings fumble (variable names, filenames, SHAs).

Embedding backends, in order of preference:
    1. sentence-transformers (fast, accurate, offline once downloaded)
    2. local ollama (`/api/embeddings`) if reachable
    3. hash-based fallback (deterministic, useless for semantics but better
       than nothing — we still have BM25)

The fallback path is not a placeholder: it ensures the skill works on a
fresh machine with zero ML deps installed. The `score` command reports which
backend was used.

Every retrieval emits a RETRIEVAL_QUERY + RETRIEVAL_RESULT pair into the
event log so attribution can later ask "did this retrieval pay off?"
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .events import EventType, make_event
from .storage import EventStore


_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@dataclass
class IndexedDoc:
    id: str                  # stable id: memory_write_id | event_id | file_path
    kind: str                # 'memory' | 'event' | 'file'
    title: str
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class RetrievalResult:
    doc_id: str
    kind: str
    score: float
    embedding_score: float
    keyword_score: float
    title: str
    snippet: str
    meta: dict


# ---- embedding backends ----

class EmbeddingBackend:
    name: str = "null"
    dim: int = 0

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class HashEmbedding(EmbeddingBackend):
    """Deterministic hash-based pseudo-embedding.

    Each token contributes to a fixed set of dimensions via hashing. It is
    garbage for semantic similarity of novel phrasing, but stable and fast,
    and — crucially — it lets BM25 do all the actual lexical work through
    the hybrid sum. The alpha weighting in `RetrievalIndex` gives it minimal
    influence automatically (we set a lower alpha when this backend is used).
    """
    name = "hash"
    dim = 256

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text)
        if not tokens:
            return vec
        for tok in tokens:
            h = hashlib.md5(tok.encode("utf-8")).digest()
            # Distribute across 8 dims per token for coverage.
            for i in range(8):
                idx_bytes = h[i * 2:i * 2 + 2]
                idx = struct.unpack("<H", idx_bytes)[0] % self.dim
                vec[idx] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class SentenceTransformersBackend(EmbeddingBackend):
    name = "sentence-transformers"

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2"):
        from sentence_transformers import SentenceTransformer  # lazy
        self._model = SentenceTransformer(model_name)
        # sentence-transformers renamed the accessor; support both.
        self.dim = (
            getattr(self._model, "get_embedding_dimension", None)
            or self._model.get_sentence_embedding_dimension
        )()

    def embed(self, text: str) -> list[float]:
        import numpy as np  # lazy
        v = self._model.encode(text, normalize_embeddings=True)
        return np.asarray(v, dtype="float32").tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import numpy as np
        vs = self._model.encode(texts, normalize_embeddings=True, batch_size=32)
        return np.asarray(vs, dtype="float32").tolist()


class OllamaBackend(EmbeddingBackend):
    name = "ollama"

    def __init__(self, host: str = "http://localhost:11434", model: str = "nomic-embed-text"):
        self.host = host.rstrip("/")
        self.model = model
        self.dim = 768  # nomic-embed-text default

    def embed(self, text: str) -> list[float]:
        import urllib.request
        req = urllib.request.Request(
            f"{self.host}/api/embeddings",
            data=json.dumps({"model": self.model, "prompt": text}).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
        vec = body.get("embedding") or []
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def choose_embedding_backend(preferred: str | None = None) -> EmbeddingBackend:
    """Try backends in order; return the first one that constructs successfully."""
    order = [preferred] if preferred else []
    order += ["sentence-transformers", "ollama", "hash"]
    errors: list[str] = []
    for name in order:
        if not name:
            continue
        try:
            if name == "sentence-transformers":
                return SentenceTransformersBackend()
            if name == "ollama":
                b = OllamaBackend()
                b.embed("health check")
                return b
            if name == "hash":
                return HashEmbedding()
        except Exception as e:  # noqa: BLE001 — intentional fallback chain
            errors.append(f"{name}: {e}")
            continue
    raise RuntimeError(
        "no embedding backend available. Errors: " + "; ".join(errors)
    )


# ---- BM25-ish scorer ----

class BM25Index:
    """BM25 over tokenized docs. Rebuilt on every index change; corpus is small."""

    k1: float = 1.5
    b: float = 0.75

    def __init__(self):
        self._docs: list[list[str]] = []
        self._doc_ids: list[str] = []
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0

    def build(self, docs: list[IndexedDoc]) -> None:
        self._docs = [tokenize(d.text) for d in docs]
        self._doc_ids = [d.id for d in docs]
        self._df = {}
        for tokens in self._docs:
            for t in set(tokens):
                self._df[t] = self._df.get(t, 0) + 1
        lens = [len(d) for d in self._docs]
        self._avgdl = (sum(lens) / len(lens)) if lens else 0.0

    def score(self, query: str) -> dict[str, float]:
        if not self._docs:
            return {}
        q_tokens = tokenize(query)
        N = len(self._docs)
        scores: dict[str, float] = {}
        for doc_id, tokens in zip(self._doc_ids, self._docs):
            dl = len(tokens) or 1
            tf: dict[str, int] = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            for qt in q_tokens:
                if qt not in tf:
                    continue
                df = self._df.get(qt, 0)
                # +0.5 smoothing keeps IDF positive for terms in every doc.
                idf = math.log(1 + (N - df + 0.5) / (df + 0.5))
                num = tf[qt] * (self.k1 + 1)
                den = tf[qt] + self.k1 * (1 - self.b + self.b * (dl / (self._avgdl or 1)))
                s += idf * (num / den)
            scores[doc_id] = s
        return scores


# ---- retrieval index ----

class RetrievalIndex:
    """Hybrid retrieval over IndexedDocs.

    Usage:
        idx = RetrievalIndex(root, backend=choose_embedding_backend())
        idx.ingest(docs)
        results = idx.query("auth middleware", top_k=5)
    """

    def __init__(
        self,
        root: Path | str,
        *,
        backend: EmbeddingBackend | None = None,
        store: EventStore | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.embeddings_path = self.root / "embeddings" / "vectors.jsonl"
        self.embeddings_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "indexes" / "index.json"
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        self.backend = backend or choose_embedding_backend()
        self._bm25 = BM25Index()
        self._docs: dict[str, IndexedDoc] = {}
        self._embeddings: dict[str, list[float]] = {}
        self._store = store
        self._load_cached()

    # ---- ingest ----

    def ingest(self, docs: Iterable[IndexedDoc], *, replace: bool = False) -> int:
        new_docs = list(docs)
        if replace:
            self._docs.clear()
            self._embeddings.clear()
        for d in new_docs:
            self._docs[d.id] = d
        # Only compute embeddings for docs we don't already have (by id+hash).
        need = [d for d in new_docs if d.id not in self._embeddings
                or self._docs[d.id].text != d.text]
        if need:
            vecs = self.backend.embed_batch([d.text for d in need])
            for d, v in zip(need, vecs):
                self._embeddings[d.id] = v
        self._bm25.build(list(self._docs.values()))
        self._persist()
        return len(new_docs)

    # ---- query ----

    def query(
        self,
        query: str,
        *,
        top_k: int = 5,
        alpha: float | None = None,
        session_id: str | None = None,
        kinds: tuple[str, ...] | None = None,
    ) -> list[RetrievalResult]:
        """Run a hybrid search. Emits RETRIEVAL_QUERY + RETRIEVAL_RESULT events."""
        if not self._docs:
            self._emit_query(session_id, query, [])
            return []

        if alpha is None:
            # Hash backend contributes less — weight BM25 higher in that case.
            alpha = 0.25 if self.backend.name == "hash" else 0.6

        q_emb = self.backend.embed(query)
        bm25 = self._bm25.score(query)

        # Normalize BM25 to 0..1 for fair blending.
        bm25_max = max(bm25.values()) if bm25 else 1.0
        bm25_max = bm25_max or 1.0

        scored: list[RetrievalResult] = []
        for doc_id, d in self._docs.items():
            if kinds and d.kind not in kinds:
                continue
            doc_emb = self._embeddings.get(doc_id)
            emb_score = _cosine(q_emb, doc_emb) if doc_emb else 0.0
            kw_score = bm25.get(doc_id, 0.0) / bm25_max
            final = alpha * emb_score + (1 - alpha) * kw_score
            scored.append(RetrievalResult(
                doc_id=doc_id,
                kind=d.kind,
                score=final,
                embedding_score=emb_score,
                keyword_score=kw_score,
                title=d.title,
                snippet=_snippet(d.text, query),
                meta=d.meta,
            ))

        scored.sort(key=lambda r: r.score, reverse=True)
        top = scored[:top_k]
        self._emit_query(session_id, query, top)
        return top

    # ---- persistence ----

    def _persist(self) -> None:
        with self.embeddings_path.open("w", encoding="utf-8") as f:
            for doc_id, vec in self._embeddings.items():
                f.write(json.dumps({"id": doc_id, "v": vec}) + "\n")
        meta = {
            "backend": self.backend.name,
            "dim": self.backend.dim,
            "docs": [
                {"id": d.id, "kind": d.kind, "title": d.title,
                 "text": d.text, "meta": d.meta}
                for d in self._docs.values()
            ],
        }
        self.index_path.write_text(json.dumps(meta), encoding="utf-8")

    def _load_cached(self) -> None:
        if self.embeddings_path.exists():
            with self.embeddings_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    self._embeddings[obj["id"]] = obj["v"]
        if self.index_path.exists():
            meta = json.loads(self.index_path.read_text(encoding="utf-8"))
            # If a different backend trained the cached embeddings, drop them —
            # mixing cosine distances across embedding spaces is nonsense.
            if meta.get("backend") != self.backend.name or meta.get("dim") != self.backend.dim:
                self._embeddings.clear()
            for d in meta.get("docs", []):
                self._docs[d["id"]] = IndexedDoc(
                    id=d["id"], kind=d["kind"], title=d["title"],
                    text=d["text"], meta=d.get("meta") or {},
                )
            self._bm25.build(list(self._docs.values()))

    # ---- event emission ----

    def _emit_query(
        self, session_id: str | None, query: str, results: list[RetrievalResult]
    ) -> None:
        if self._store is None or session_id is None:
            return
        # Emit a QUERY event first, then RESULT with its parent pointing to query.
        q_ev = self._store.append(make_event(
            session_id=session_id,
            seq=self._store._next_seq(session_id),
            type=EventType.RETRIEVAL_QUERY,
            payload={"query": query, "backend": self.backend.name},
        ))
        self._store.append(make_event(
            session_id=session_id,
            seq=self._store._next_seq(session_id),
            type=EventType.RETRIEVAL_RESULT,
            payload={
                "query": query,
                "hits": [
                    {
                        "memory_write_id": r.meta.get("memory_write_id"),
                        "doc_id": r.doc_id,
                        "kind": r.kind,
                        "score": r.score,
                        "title": r.title,
                    } for r in results
                ],
            },
            parent_ids=(q_ev.id,),
        ))


# ---- helpers ----

def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _snippet(text: str, query: str, width: int = 200) -> str:
    q_tokens = set(tokenize(query))
    # Find first position where any query token matches.
    lowered = text.lower()
    pos = -1
    for t in q_tokens:
        i = lowered.find(t)
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        return (text[:width] + "...") if len(text) > width else text
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ") + suffix


# ---- builders ----

def build_docs_from_memory(memory_layer) -> list[IndexedDoc]:
    """Construct IndexedDocs from the compressed memory layer.

    Each topic file becomes one doc. The memory_write_id is the write event
    that most recently produced that topic — necessary for ROI attribution.
    """
    from .memory import MemoryLayer  # local import to avoid cycle
    assert isinstance(memory_layer, MemoryLayer)
    docs: list[IndexedDoc] = []
    for e in memory_layer.iter_entries():
        # We cannot know the memory_write_id from the file alone; callers
        # who need it should join against the memory_writes table by path.
        text = f"{e.name}\n{e.description}\n\n{e.body}"
        docs.append(IndexedDoc(
            id=f"memory::{e.path.name if e.path else e.name}",
            kind="memory",
            title=e.name,
            text=text,
            meta={
                "path": str(e.path) if e.path else None,
                "type": e.type,
                "source_events": e.source_events,
            },
        ))
    return docs


def build_docs_from_events(db) -> list[IndexedDoc]:
    """Construct IndexedDocs from raw events (prompts + assistant messages).

    These allow retrieval to hit the ground truth directly when memory is
    sparse or stale.
    """
    import sqlite3 as _sq  # type hint only
    docs: list[IndexedDoc] = []
    rows = db._conn.execute(
        """SELECT id, type, payload_json, session_id, ts FROM events
           WHERE type IN ('user_prompt', 'assistant_message')"""
    ).fetchall()
    for r in rows:
        payload = json.loads(r["payload_json"])
        text = (payload.get("text") or "")[:4000]  # cap to keep index small
        if not text.strip():
            continue
        docs.append(IndexedDoc(
            id=f"event::{r['id']}",
            kind="event",
            title=f"{r['type']} ({r['session_id'][:8]})",
            text=text,
            meta={"session_id": r["session_id"], "ts": r["ts"], "type": r["type"]},
        ))
    return docs
