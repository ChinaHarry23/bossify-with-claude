# Schemas

## Event (JSONL line)

Every line in `data/raw_events/*.jsonl` decodes to:

```json
{
  "id":              "deterministic-hash-24-char",
  "session_id":      "hex16",
  "seq":             42,
  "ts":              1713225631.412,
  "type":            "assistant_message",
  "payload":         {"text": "..."},
  "parent_ids":      ["id_of_user_prompt"],
  "tokens_in":       0,
  "tokens_out":      418,
  "cached_tokens":   0,
  "cache_creation_tokens": 0,
  "model":           "claude-sonnet-4-6",
  "latency_ms":      1820
}
```

### Event types + required payload keys

| type                 | required payload                         | emitter                        |
|----------------------|------------------------------------------|--------------------------------|
| `session_start`      | `session_id`                             | storage                        |
| `session_end`        | `session_id`                             | storage                        |
| `user_prompt`        | `text`                                   | hooks / SDK wrapper            |
| `assistant_message`  | `text`                                   | hooks / SDK wrapper            |
| `pre_tool_use`       | `tool_name`, `input`                     | hooks / SDK wrapper            |
| `post_tool_use`      | `tool_name`, `success`                   | hooks                          |
| `tool_error`         | `tool_name`, `error`                     | hooks                          |
| `file_read`          | `path`                                   | hooks (promoted from post_tool)|
| `file_write`         | `path`                                   | hooks (promoted from post_tool)|
| `memory_read`        | `path`                                   | memory layer / hooks           |
| `memory_write`       | `path`, `kind`                           | memory layer / hooks           |
| `memory_delete`      | `path`                                   | memory layer                   |
| `retrieval_query`    | `query`                                  | retrieval index                |
| `retrieval_result`   | `query`, `hits`                          | retrieval index                |
| `compression_run`    | `summary`                                | compression engine             |
| `outcome`            | `kind`                                   | external (CI, tests, user)     |

### `memory_write.kind`

Signals *why* the write happened:
- `user`, `feedback`, `project`, `reference` — see SKILL.md memory types
- `index` — MEMORY.md itself
- `agent_edit` — promoted from a file-write tool call into the memory dir

### `retrieval_result.hits`

Array of:
```json
{"memory_write_id": "...", "doc_id": "...", "kind": "memory|event", "score": 0.72, "title": "..."}
```

## SQLite DDL

See `src/token_roi/db.py::DDL`. Five tables:

- `events`                 — 1:1 mirror of JSONL
- `memory_writes`          — materialized view: per-write retrieval counters
- `retrievals`             — materialized view: per-query hits + used_downstream
- `attributions`           — per-prompt cost / value terms
- `roi_scores`             — per-scope classification + derivation_json

All derivation is stored as JSON text in `roi_scores.derivation_json` —
`cli.py explain` dumps it back out verbatim.
