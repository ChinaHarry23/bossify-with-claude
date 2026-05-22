# Show HN Launch Post

## Title options (pick one — title is ~70% of the battle on HN)

- **A) Show HN: Bossify – A local LLM that grades your team's Claude Code sessions**  ← recommended
- B) Show HN: Bossify – Find out who on your team is burning AI coding tokens
- C) Show HN: I built a dashboard that roasts my team's Claude Code spending
- D) Show HN: Bossify – Treat AI coding tokens as capital allocation

Reasoning: (A) is concrete, names the tech, implies team/judgment without clickbait. "Show HN: Name" posts outperform "I built" posts for dev tools.

## Body (<250 words, technical, end with invite)

```
Hi HN,

I kept noticing that "AI-accelerated" sprints at my company were shipping less
than the old ones, while the token bills looked like Series A rounds. Nobody
could point at where the money was going — just that Claude Code sessions felt
productive in the moment.

Bossify reads your Claude Code session history from ~/.claude/projects/ and
asks a local LLM (LM Studio / Ollama) to judge every prompt: was this worth
the tokens it cost? Output is a dashboard with per-person and per-project ROI,
a waste leaderboard, and drill-down to the exact prompts that burned tokens
for nothing.

The judge prompt is deliberately adversarial. Scores anchor around 0.5, not
0.8. HIGH_VALUE is rare by design. Every verdict has to cite concrete
wasteful_patterns ("23 tool calls produced no diff", "MEMORY.md grew 6KB,
never retrieved again"). Pure-math ROI is the starting point; the LLM's
read of the actual content overrides it.

Everything is local-first. No cloud, no telemetry. Raw events are append-only
JSONL so every number traces back to the events that produced it —
`token-roi explain --kind session --id <X>` dumps the full derivation.

Works on one person's history, or point it at a team-wide mount with an
employees.json mapping. Bilingual UI (EN / 简体中文).

Repo: https://github.com/chinaharry/bossify-with-claude

Curious what anti-patterns other people have noticed in their own
agentic-coding usage — the rubric is still evolving and I'd like to make
it harsher.
```

## Timing

- **Day**: Tuesday or Wednesday
- **Time**: 08:30–09:30 ET
- Avoid Friday and weekends. Morning posts catch the US wake-up wave and have
  daylight to build on /newest before saturating.

## First-hour hygiene

- Do NOT ask friends to upvote in a burst — HN detects this and flags posts.
- Instead, tell 3–5 people who will genuinely **comment** with substance.
- Comments > upvotes for front-page ranking.
- Be in the thread for the first 4 hours replying to every comment —
  fast, specific, not defensive.
- If it lands on the front page, post the HN link into your X thread
  ~2h later for compounding traffic.

## Anti-patterns to avoid

- Don't edit the title after submission (resets ranking signals).
- Don't reply with marketing speak. Be the engineer, not the PM.
- Don't get defensive about criticism — HN rewards "good point, I'll fix it".
- Don't link to a landing page. Link to the repo. HN trusts GitHub over
  marketing sites.
