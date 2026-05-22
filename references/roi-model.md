# ROI scoring model

## Definitions

For a single prompt event `P`:

- `cost_tokens(P)` — sum of `effective_cost_tokens` on every ASSISTANT_MESSAGE
  event in the turn rooted at `P`. (A turn ends at the next USER_PROMPT.)
  `effective_cost_tokens = tokens_in + tokens_out + cache_creation_tokens +
  cached_tokens / 10`. Cache reads are 10x discounted to reflect Anthropic's
  pricing — otherwise a cache-heavy Claude Code session's denominator
  drowns out every value signal.
- `durable_bytes(P)` — sum of `payload.bytes` across MEMORY_WRITE events
  in the turn.
- `retrieval_count(P)` — count of RETRIEVAL_RESULT events in ANY session
  whose `hits` include a `memory_write_id` produced by `P`. Cross-session,
  cross-time.
- `reuse_score(P) = log1p(retrieval_count(P)) / log1p(1) = ln(1 + n) / ln(2)`
- `outcome_score(P)` — weighted sum of OUTCOME events in the turn, using
  `attribution.OUTCOME_WEIGHTS`.

### Retrospective proxies

When live signals are absent (the common case for imported data), these
proxies contribute to the score so the classification still differentiates:

- `file_write_bytes(P)` — sum of `payload.bytes` across FILE_WRITE events
  in the turn. Counts as a weaker durable-value signal.
- `tool_success_rate(P) = tool_successes / tool_calls` — fraction of
  POST_TOOL_USE events with `success=true`. Counts as a soft outcome
  signal when ≥ 3 tool calls happened.

Proxies don't replace real signals — they're a floor. When a turn has a
real memory write, its byte count is compared against the file-write proxy
via `max()`; the stronger term wins.

## Scoring formula

```
# --- primary terms ---
v_durable_mem   = log1p(durable_bytes)      / log1p(2000)
v_reuse         = min(reuse_score, log1p(30)/log1p(1))
v_outcome_real  = max(0, outcome_score)
v_negative      = -min(0, outcome_score)

# --- retrospective proxies (zero when the corresponding signal is absent) ---
v_durable_files = 0.8 * log1p(file_write_bytes) / log1p(20000)

                  ┌─ +0.5   if tool_calls ≥ 3 and success_rate ≥ 0.85
v_outcome_proxy = ┤
                  └─ -0.3   if tool_calls ≥ 3 and success_rate <  0.60

v_durable       = max(v_durable_mem, v_durable_files)
v_outcome       = v_outcome_real + max(0, v_outcome_proxy)
v_negative     += max(0, -v_outcome_proxy)

# --- cost ---
cost_unit       = max(0.25, cost_tokens / 150_000)

numerator       = 0.4*v_durable + 0.35*v_reuse + 0.25*v_outcome
denominator     = cost_unit + v_negative
score           = numerator / denominator
```

### Why this shape

- `effective_cost_tokens` approximates Anthropic's pricing by discounting
  cache reads 10x. Without this, a 30M-token-of-flow Claude Code session
  would have `cost_unit ≈ 200`, dwarfing any numerator.
- `cost_tokens / 150_000` calibrates "one cost unit" to a typical agentic
  turn. A 150K-effective-token turn at max value → score ~1.0 (HIGH_VALUE);
  a 1.5M-token turn at max value → ~0.1 (LOW_VALUE). That matches the
  intuition that a single turn costing 10 reasonable-turns' worth of
  tokens had better produce 10 turns' worth of value.
- `log1p` on durable bytes and file writes prevents a megabyte dump from
  dominating. A 2KB memory write or 20KB file write saturates its term.
- `log1p` on reuse caps the term so a viral memory entry that gets hit
  1000 times doesn't distort aggregate session scores.
- Negative outcomes (`tests_failed`, `revert`, `user_rejected`, low tool
  success rate) enter the denominator so they widen the good/bad gap
  without flipping the score's sign.
- The file-write proxy is 0.8x-weighted because writing code is durable
  output but memory writes are additionally **indexed for retrieval** and
  therefore have higher compounding potential. A real memory write still
  wins head-to-head at equal byte count.

### Classification thresholds

```
WASTED:     score < 0.05
            OR (outcome_score < -0.5 AND reuse_score == 0 AND durable_bytes == 0
                AND file_write_bytes == 0)
LOW_VALUE:  0.05 <= score < 0.25
TRANSIENT:  0.25 <= score < 0.6
HIGH_VALUE: score >= 0.6 AND (durable_bytes > 0 OR reuse_score > 1
                              OR outcome_score > 0.5
                              OR file_write_bytes > 0
                              OR (tool_calls >= 3 AND success_rate >= 0.9))
```

The `HIGH_VALUE` gate requires at least *one* observable form of durable
value — either a real signal (memory write, reuse, explicit outcome) or a
strong proxy (file writes, sustained tool success). Otherwise a very
cheap prompt producing a short answer with zero follow-through could
slip into HIGH_VALUE by math alone.

## Outcome weights

From `attribution.OUTCOME_WEIGHTS`:

| kind             | weight |
|------------------|-------:|
| `tests_passed`   | +1.0   |
| `tests_failed`   | -0.7   |
| `commit_created` | +0.6   |
| `pr_merged`      | +1.2   |
| `revert`         | -1.0   |
| `build_ok`       | +0.3   |
| `build_failed`   | -0.5   |
| `user_accepted`  | +0.8   |
| `user_rejected`  | -1.0   |

Tune these by editing the constant — every downstream score is recomputed
on the next `token-roi score` run.

## Scope hierarchy

- **prompt** — one row per USER_PROMPT event
- **session** — synthesized Attribution = sum of per-prompt terms, scored
  with the same formula
- **memory_write** — hits-dominated (no cost term, because cost is already
  attributed to the originating prompt)
- **tool_chain** — reserved for future use

## Reproducibility

Given the same events, same weights, same thresholds, the scorer produces
bit-identical output. No randomness, no machine-dependence. The derivation
payload is stable enough to diff across runs — useful for CI regression
tests when tuning weights.
