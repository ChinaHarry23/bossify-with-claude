# Twitter / X Launch Thread

Post ~2h after the Show HN goes up, so you can quote-tweet the HN link
into the thread if it lands.

---

## Tweet 1 — The Hook (the only one most people will read)

> Your engineer spent 4 hours and 2M tokens to rename a variable.
>
> You have no way to prove it.
>
> I built Bossify — a local LLM that reads your team's Claude Code sessions
> and grades every prompt. HIGH_VALUE is rare by design. The dashboard
> names names.
>
> [attach: demo GIF — the single most important asset]

## Tweet 2

> The judge prompt is adversarial. Scores anchor at 0.5, not 0.8. Every
> verdict has to cite evidence:
>
> "23 tool calls produced no diff"
> "Assistant wrote 4,000 words of plan, user never approved"
> "MEMORY.md grew 6KB, never retrieved again"
>
> [attach: screenshot of a WASTED verdict card]

## Tweet 3

> Four classes:
>
> 🟢 HIGH_VALUE  — durable output reused later
> 🟡 TRANSIENT   — produced output, wasn't reused
> 🟠 LOW_VALUE   — long narration, little durable output
> 🔴 WASTED      — zero durable return. tokens are gone.

## Tweet 4

> 100% local. LM Studio or Ollama. No cloud, no telemetry.
>
> Raw events are append-only JSONL. Every ROI number has:
>
>    token-roi explain --kind session --id <X>
>
> which dumps the events, memory writes, retrieval hits, and LLM reasoning
> behind the score. No black box.

## Tweet 5

> Team mode: point it at a shared mount of everyone's ~/.claude/projects/,
> define employees.json, get per-person cards + a waste leaderboard.
>
> [attach: screenshot of the leaderboard, names blurred]

## Tweet 6 — The Close

> MIT license. Bilingual (EN / 中文). Tested on 8k+ LoC of real sessions.
>
> Star it if you want your AI coding bill to stop looking like a Series A:
>
> github.com/chinaharry/bossify-with-claude

---

## Tagging strategy

Reply-tag at most 1–2 per thread. The algorithm suppresses mass-tag tweets.

**English dev Twitter:**
- @simonw — writes about LLM tooling daily, often amplifies clever
  local-first tools
- @swyx — loves the "measure the AI spend" angle
- @karpathy — long shot, occasionally retweets opinionated dev tools
- @AnthropicAI — worth a polite @ in tweet 1 since you're built on their
  product

**中文 AI dev Twitter (underrated reach):**
- @op7418
- @dotey
- @goodside (bilingual)

## Post-launch amplification

- Day 2: quote-tweet tweet 1 with a "update: star count" screenshot if it
  spikes — social proof compounds
- Day 3: post the same thread on 即刻 and V2EX (adapted tone)
- Day 4: short Bilibili screen-record of the same demo
