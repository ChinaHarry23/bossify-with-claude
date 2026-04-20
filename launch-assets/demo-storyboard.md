# Demo Video Storyboard — 75 seconds

Goal: one watchable clip that makes someone open the repo before it ends.
Every second earns its place.

- Length: 75 seconds (target); max 90
- Format: screen recording, no voiceover, captions only (~85% of social
  video plays are muted)
- Resolution: 1920×1080 @ 30fps
- Exports:
  - `full.mp4` — YouTube + HN comments
  - `hero.gif` — cold-open through dashboard reveal only (first ~15 sec),
    < 10MB, embedded at top of README

## Beat-by-beat

### 00:00–00:04 — Cold Open (the pain)
Black screen, white text, typewriter fade-in:
```
Your team shipped less this sprint.
The Claude bill was $9,412.
Nobody knows why.
```
No logo. No intro. Just the sting.

### 00:04–00:09 — The Promise
Centered text over the bossify logo:
```
bossify — audit your AI coding spend
100% local · reads Claude Code sessions · names names
```

### 00:09–00:15 — The Command
Terminal, dark theme (Dracula or similar — NOT default macOS white).
Font ≥ 18pt for mobile legibility.

Type with realistic pauses:
```
$ token-roi import claude-code
$ token-roi judge --model glm-4.7-flash
```
Progress bar briefly shown:
  `Judging 847 prompts · LM Studio · local`

Caption: **"Everything runs on your machine. No cloud."**

### 00:15–00:22 — Dashboard Reveal
Browser opens `http://127.0.0.1:8787`. Overview tab. Pan across:
- Total spend: $9,412
- Cost per KB shipped: $4.71
- ROI distribution donut (mostly orange/red)
- Top waste patterns box

Caption: **"One dashboard. Four tabs. Every number explainable."**

### 00:22–00:35 — The Leaderboard Reveal (the payoff)
Click "People" tab. Per-employee cards, sorted by waste.

Slow zoom on top card. Name blurred to `████ ████`:
```
Senior Engineer · Platform
USD burned:  $2,847
Sessions:    34
WASTED:      61%   LOW_VALUE: 22%
Top pattern: planning loops with no diff
```

Caption, bold, bottom third: **"The dashboard names names."**

Hold on the card for 2 full seconds — this is the frame that gets shared.

### 00:35–00:50 — Drill-down
Click the card. Session list expands. Hover the worst:
```
refactor auth middleware
$847 · 2.3M tokens · 0 commits · WASTED · score 0.12
```
Click it. Verdict card:
```
wasteful_patterns:
  • 23 tool calls produced no diff
  • 4,000-word plan, never approved
  • MEMORY.md grew 6KB, never retrieved

verdict:
  "Long rambling planning session with no durable
   output. Tokens are gone."
```

Caption: **"Every verdict cites evidence. No vibes."**

### 00:50–01:02 — The Auditability Flex
Terminal:
```
$ token-roi explain --kind session --id refactor_auth_42
```
Output scrolls: raw events, memory writes, retrieval hits, LLM reasoning.
Let it scroll 3 seconds — the density sells it.

Caption:
**"Every number traces back to the raw events."**
**"Append-only JSONL. Nothing invented."**

### 01:02–01:12 — Team + Local Proof
Split-screen montage, 2 sec per shot:
- `employees.json` being edited
- LM Studio running locally (show the app window)
- Projects tab with model-mix pills (Opus/Sonnet/Haiku)
- Chinese locale: dashboard in 中文 for half a second

Caption: **"Solo or team. English or 中文. Yours."**

### 01:12–01:15 — CTA
Black screen:
```
github.com/chinaharry/bossify-with-claude
★ if your AI bill hurts
```

## Production notes

- Record with **OBS** (free) or **ScreenStudio** (~$89, Mac). ScreenStudio's
  auto-zoom on clicks is half of why Arc/Raycast demos feel cinematic —
  worth the one-time cost for this single asset.
- Fake-but-realistic `data/employees.json` (Alex Chen, Jordan Smith, etc.).
  Do NOT use real coworkers unless they consent.
- Pre-pick one "hero session" for the drill-down so you don't fumble live.
- Browser: no bookmarks bar, no notifications, generic blank new tab.
- Terminal: minimal prompt (consider starship), no hostname, no venv clutter.
- Caption font: Inter Bold, SF Pro Display Bold, or Söhne. Never Arial.
- Colors: match the dashboard — red for pain points, green for
  "100% local" proof.
- Music: Epidemic Sound "Tense Tech" or skip. Silence + typing works for
  a technical audience.

## Assets to prep before recording

1. Fake `data/employees.json` with 4–6 names
2. Claude Code history with at least one genuinely WASTED session
3. Dashboard pre-loaded, running, browser cleaned
4. Terminal configured with large font, clean prompt
5. Pre-picked hero session for the drill-down

## Derived deliverables

From this one recording you get:
- `hero.gif` for README top
- Full YouTube video
- 4–5 still screenshots for README, X thread, HN comments
- 15-sec vertical cut for Bilibili / 小红书 / TikTok
- Quote frames (the "names names" moment) for X thread replies
