# Architecture

## Module map

```
src/token_roi/
├── events.py         ground-truth schema + deterministic ids
├── storage.py        append-only JSONL + file layout
├── db.py             SQLite analytics index (derived from storage)
├── memory.py         MEMORY.md + topics/ filesystem layout
├── retrieval.py      hybrid search (embeddings + BM25)
├── attribution.py    prompt → memory → retrieval → outcome DAG walk
├── roi.py            classifier + scoring formula
├── compression.py    clustering + topic file builder
├── telemetry.py      optional OTEL export
├── hooks.py          Claude Code hook dispatch + installer
├── sdk_wrapper.py    Anthropic SDK instrumentation (Mode B)
├── replay.py         deterministic session replay
├── cli.py            the user-facing command surface
└── dashboard/        FastAPI UI
```

## Data flow

1. **Capture** — Mode A (hooks) or Mode B (SDK wrapper) emits events to
   `storage.EventStore.append()`. This is the only write path to raw events.
2. **Index** — `cli.py ingest` or the implicit rebuild in `score` drains
   JSONL into SQLite via `db.AnalyticsDB.rebuild_from()`.
3. **Attribute** — `attribution.AttributionGraph.attribute_session()` walks
   the event DAG for each session, produces per-prompt attributions, and
   persists them.
4. **Classify** — `roi.ROIClassifier` consumes attributions, emits ROI
   classes, persists them with their full derivation.
5. **Compress** — `compression.CompressionEngine` clusters prompts and
   writes `MEMORY.md` + `topics/*.md`. Each write is itself an event.
6. **Retrieve** — `retrieval.RetrievalIndex` ingests compressed memory
   (and optionally raw events) and serves hybrid queries. Every query is
   logged as a RETRIEVAL_QUERY + RETRIEVAL_RESULT pair.
7. **Replay / dashboard** — `replay.Replayer` regenerates the timeline
   purely from `raw_events/` for auditing; the dashboard joins the DB
   tables for a live view.

## Ground truth vs derived state

| Layer                 | Source of truth? | How to rebuild |
|-----------------------|------------------|----------------|
| `raw_events/*.jsonl`  | yes              | cannot rebuild — this IS the truth |
| `analytics/roi.db`    | no               | `token-roi ingest` |
| `memory/MEMORY.md`    | no               | `token-roi compress` |
| `memory/topics/*.md`  | no               | `token-roi compress` |
| `retrieval/*`         | no               | `token-roi index-memory` |

If the DB, the memory files, and the retrieval index all disappear, the
skill can rebuild every number it reports from `raw_events/` alone. That is
the audit property.

## Failure modes and recovery

- **Crash mid-append.** Last JSONL line may be truncated. `storage._read_jsonl`
  detects, logs, and skips; every prior line is still valid.
- **Multiple writers.** `fcntl.flock` guarantees one writer at a time per
  session file. Safe to run hooks + SDK wrapper concurrently.
- **DB corruption.** Delete `analytics/roi.db` and run `token-roi ingest`.
  Zero data loss.
- **MEMORY.md drift.** If the agent hand-edits topic files, the compression
  engine's next run will overwrite them. Intended — memory is a cache.
- **Embedding backend change.** `retrieval.py` detects backend mismatch in
  the cached index and refuses to blend cosine distances across spaces.

## Extension points

- **Outcome collectors.** Emit OUTCOME events via the store from any CI
  webhook, test runner, or editor hook. `attribution.OUTCOME_WEIGHTS` is
  the single policy knob.
- **Summarization.** Inject a `summarize_fn(cluster) -> str` into
  `CompressionEngine` if you want LLM-based topic bodies. Default is
  extractive and LLM-free.
- **Backend.** Add a new `EmbeddingBackend` subclass and extend
  `choose_embedding_backend` with an early entry.
