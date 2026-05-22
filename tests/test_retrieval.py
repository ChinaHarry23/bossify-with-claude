"""Retrieval tests. Exercise the hash backend (always available) so tests
are hermetic — no ML deps needed in CI."""
from __future__ import annotations

from token_roi.retrieval import HashEmbedding, IndexedDoc, RetrievalIndex


def test_hash_embedding_is_deterministic():
    a = HashEmbedding().embed("auth middleware")
    b = HashEmbedding().embed("auth middleware")
    assert a == b
    assert len(a) == 256


def test_hybrid_query_prefers_matching_doc(data_dir, store):
    idx = RetrievalIndex(data_dir / "retrieval", backend=HashEmbedding(), store=store)
    idx.ingest([
        IndexedDoc(id="d1", kind="memory", title="auth rewrite",
                   text="notes about auth middleware and token rotation"),
        IndexedDoc(id="d2", kind="memory", title="ci pipelines",
                   text="gitlab ci yaml tips"),
        IndexedDoc(id="d3", kind="memory", title="vector stores",
                   text="indexing embeddings with faiss"),
    ])
    sid = store.start_session()
    results = idx.query("auth middleware token", top_k=3, session_id=sid)
    assert results[0].doc_id == "d1"
    assert results[0].score > results[-1].score


def test_retrieval_emits_query_and_result_events(data_dir, store):
    idx = RetrievalIndex(data_dir / "retrieval", backend=HashEmbedding(), store=store)
    idx.ingest([IndexedDoc(id="d1", kind="memory", title="t", text="hello world")])
    sid = store.start_session()
    idx.query("hello", top_k=1, session_id=sid)
    from token_roi.events import EventType
    types = [e.type for e in store.iter_session(sid)]
    assert EventType.RETRIEVAL_QUERY in types
    assert EventType.RETRIEVAL_RESULT in types


def test_index_reload_across_processes(data_dir):
    """Persisted embeddings + docs should round-trip."""
    backend = HashEmbedding()
    root = data_dir / "retrieval"
    idx1 = RetrievalIndex(root, backend=backend)
    idx1.ingest([IndexedDoc(id="d1", kind="memory", title="t", text="alpha beta")])

    # Simulate a fresh process.
    idx2 = RetrievalIndex(root, backend=HashEmbedding())
    results = idx2.query("alpha", top_k=1)
    assert results and results[0].doc_id == "d1"
