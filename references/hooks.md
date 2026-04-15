# Claude Code hook integration

Claude Code runs registered shell commands at specific lifecycle points.
This skill ships Python hook scripts that dispatch into
`token_roi.hooks.on_*` and append typed events to the store.

## Supported hook events

| hook               | script                       | token_roi handler          |
|--------------------|------------------------------|----------------------------|
| `UserPromptSubmit` | `hooks/user_prompt_submit.py`| `on_user_prompt_submit`    |
| `PreToolUse`       | `hooks/pre_tool_use.py`      | `on_pre_tool_use`          |
| `PostToolUse`      | `hooks/post_tool_use.py`     | `on_post_tool_use`         |
| `Stop`             | `hooks/stop.py`              | `on_stop`                  |
| `SessionStart`     | `hooks/session_start.py`     | `on_session_start`         |
| `SessionEnd`       | `hooks/session_end.py`       | `on_session_end`           |

## Installation

```bash
token-roi install-hooks --settings ~/.claude/settings.json
```

This is idempotent:
- On first run, backs up the existing `settings.json` to `settings.json.bak`.
- Appends one entry per hook type with `"matcher": "*"`.
- Skips any hook entry already pointing at a token_roi script.

To uninstall, remove the matching entries from `settings.json`, or restore
the `.bak` backup.

## Hook payload shape

Claude Code passes each hook a JSON object on stdin. The useful keys we
consume (all other keys are ignored):

```json
{
  "session_id":   "...",
  "prompt":       "...",                 // UserPromptSubmit
  "tool_name":    "Read",                // Pre/PostToolUse
  "tool_input":   { "file_path": "..." }, // Pre/PostToolUse
  "tool_response":{ "...": "..." },      // PostToolUse
  "success":      true,                  // PostToolUse
  "response":     "...",                 // Stop
  "usage": {                             // Stop (when the harness reports it)
    "input_tokens": 8120,
    "output_tokens": 450,
    "cache_read_input_tokens": 2000,
    "cache_creation_input_tokens": 120
  },
  "model":        "claude-sonnet-4-6",
  "latency_ms":   1820
}
```

The SDK alternative populates equivalent events from the Anthropic API
response rather than the hook payload — see `sdk_wrapper.py`.

## Failure semantics

Hook scripts **must never fail loudly**. `_shim.safe_run` catches every
exception, logs it to stderr, and exits 0 so the user's Claude Code
session keeps running. A crashed hook drops one event; the pipeline tolerates
drops because downstream analysis only needs the events that were written.

## Debugging a hook

1. Pipe a sample payload into the script manually:
   ```bash
   echo '{"session_id":"test","prompt":"hi"}' \
       | python hooks/user_prompt_submit.py --data-dir ./data --debug
   ```
2. Check `data/raw_events/$(date +%Y-%m-%d)/session_test.jsonl` — the
   event should appear.
3. Set `TOKEN_ROI_DEBUG=1` before launching Claude Code for verbose stderr.

## Promoted events

`on_post_tool_use` promotes certain tool calls into typed events so queries
don't need to parse payloads:

| tool               | promoted to         |
|--------------------|---------------------|
| `Read`, `NotebookRead`     | `file_read` / `memory_read`  |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | `file_write` / `memory_write` |

A write whose `file_path` lives under `data/memory/` becomes `memory_write`
with `kind="agent_edit"`. This is how the skill captures memory activity
performed by the agent's own tool calls.
