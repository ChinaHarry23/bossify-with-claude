# Worked examples

## Example 1: HIGH_VALUE prompt

User prompts: *"summarize the auth rewrite so far"*. The agent:
- burns 4000 tokens on the response
- writes two topic files totaling ~1800 bytes
- triggers no outcome events directly

Two weeks later, five other sessions retrieve the `auth-rewrite.md` topic.

Terms:
- `cost_tokens`   = 4000
- `durable_bytes` = 1800
- `retrieval_count` = 5
- `outcome_score` = 0
- `reuse_score` = log1p(5)/log1p(1) ≈ 2.58

Computation:
- `v_durable` = log1p(1800)/log1p(2000) ≈ 0.988
- `v_reuse`   = min(2.58, log1p(30)/log1p(1)) = 2.58
- `v_outcome` = 0
- `numerator` = 0.4*0.988 + 0.35*2.58 + 0.25*0 = 1.298
- `cost_unit` = 4000/1000 = 4.0
- `score` = 1.298 / 4.0 = 0.325

→ **TRANSIENT_VALUE**. The score is moderated by the 4K cost and saturated
durable term; would need more reuse to cross 0.6.

After another 25 retrievals (30 total), `v_reuse` still caps at ~3.4, but the
gate for HIGH_VALUE stays at `score ≥ 0.6`. A cheap, durable, heavily-reused
prompt will get there; a 4K-token prompt with 5 retrievals won't — and that
is deliberate.

## Example 2: WASTED prompt

User: *"try again with verbose logging"*. The agent re-runs a tool, it fails
again, nothing is written to memory, the build stays red.

Terms:
- `cost_tokens`   = 1200
- `durable_bytes` = 0
- `retrieval_count` = 0
- `outcome_score` = -0.5 (build_failed)

Gate: `outcome_score < -0.5 AND reuse_score == 0 AND durable_bytes == 0`
→ False (outcome is exactly -0.5, not strictly <).

Compute:
- `v_durable` = 0, `v_reuse` = 0, `v_outcome` = 0
- `numerator` = 0
- `cost_unit` = 1.2, `v_negative` = 0.5
- `denominator` = 1.7
- `score` = 0 / 1.7 = 0 → **WASTED**.

## Example 3: orphan memory

The agent writes `topic_debug_session.md` after a 10K-token debugging thread.
The file is 4KB. No one retrieves it in the next 7 days.

memory_write scoring:
- `retrieval_hits = 0`
- `age_days ≈ 7`
- Classifier: hits==0, age > 3 days → **WASTED**

The originating prompt still gets credit for the durable write on its first
scoring pass (positive `durable_bytes`). But without retrieval, the file's
own score is WASTED, and `orphan_memory.sql` surfaces it for cleanup.

## Example 4: cross-session reuse

Session A writes `memory/topics/auth-rewrite.md`. Session B (days later)
queries `"how did we handle token rotation in auth"`.

Event chain:
1. Session A USER_PROMPT → ASSISTANT_MESSAGE → MEMORY_WRITE(auth-rewrite.md)
2. Session B RETRIEVAL_QUERY("how did we handle token rotation in auth")
   → RETRIEVAL_RESULT whose `hits` includes
     `memory_write_id = <MEMORY_WRITE id from Session A>`

Attribution in Session B increments `memory_writes.retrieval_hits` for the
Session A write, updates `last_retrieved`, and the next `score` pass bumps
Session A's prompt's `retrieval_count` and `reuse_score`. This is how the
skill captures *deferred* value — the payoff can land weeks later.

## Example 5: explaining a score

```
$ token-roi score --session abcd1234
sessions=1 prompt_attributions=4 prompts_scored=4 ...
  HIGH_VALUE        1
  TRANSIENT_VALUE   2
  LOW_VALUE         0
  WASTED            1

$ token-roi explain --kind prompt --id <prompt_event_id>
class:   TRANSIENT_VALUE
score:   0.325
derivation:
{
  "formula": "numerator / denominator",
  "numerator": {
    "weights": {"durable": 0.4, "reuse": 0.35, "outcome": 0.25},
    "v_durable": 0.988,
    "v_reuse": 2.58,
    "v_outcome": 0.0
  },
  "denominator": {"cost_unit": 4.0, "v_negative": 0.0},
  "inputs": {
    "cost_tokens": 4000,
    "durable_bytes": 1800,
    "retrieval_count": 5,
    "reuse_score": 2.58,
    "outcome_score": 0
  },
  "contributions": {
    "cost_events": ["evt_abc..."],
    "memory_writes": ["evt_def...", "evt_ghi..."],
    "retrieval_hits": ["evt_jkl...", ...],
    "outcomes": []
  }
}
```

Every id listed is replayable:

```
$ token-roi replay --session abcd1234 --from evt_abc... --show-payload
```
