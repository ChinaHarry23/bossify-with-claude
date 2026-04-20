# README Hero Rewrite + Screenshot Spec

See `NEW_README_HERO.md` for the drop-in replacement block.
This file documents the reasoning and the screenshot spec.

## Why the current hero underperforms

Current first screen: title, tagline, five badges, five rhetorical
questions, then a positioning line. By the time someone reaches
"point it at your team's ~/.claude/projects" they've already decided
whether to keep reading. Projects that hit 1000+ stars/day follow a
consistent pattern:

- shadcn/ui — component screenshot, one-line positioning, install, done
- Bun — logo, one-line hook, benchmark GIF, install
- ollama — logo, one line, terminal demo, install
- Aider — demo GIF first, then tagline

Pattern: **hero visual → one-line hook → install command, all above
the fold.**

## What changes

1. Tagline cut to "The boss dashboard for your AI coding spend."
   Grep-able. Two-second read. The clever version stays as flavor
   deeper in the README.
2. Demo GIF before badges. Current README has zero images — biggest
   single upgrade.
3. Install command above the fold. Kills the "is this a library or an
   app" ambiguity instantly.
4. Moved Chinese subtitle out of the primary tagline position — kept
   as title flair, English leads.
5. Rhetorical-question block becomes an inline pull-quote, not the
   hero. Punchline material — hits harder after the visual.
6. Removed top-of-README author attribution. Front-loaded credit reads
   as a personal side project, not a tool. Attribution stays in MIT
   footer.
7. Added a second screenshot (People tab) right after the pull quote —
   the "names names" payoff frame.

## Screenshot spec

All 1920×1080 PNG under `docs/assets/`. Dark mode if available.
Fake names throughout: Alex Chen, Jordan Smith, Priya Patel,
Wei Zhang, Sam Okafor.

| File | What | Where used |
|---|---|---|
| `hero.gif` | 15 sec loop: terminal → judge running → dashboard reveal → zoom to People tab. < 10MB. | README top, X thread, HN comments |
| `people-tab.png` | Full People tab, 5–6 cards sorted by waste | Right under opening pull quote |
| `overview-tab.png` | Overview tab: KPIs, ROI donut, waste patterns, burner leaderboard | "Dashboard" section |
| `verdict-card.png` | Zoomed single WASTED verdict, wasteful_patterns + verdict readable | Most tweetable image. X thread tweet 2, "How Scoring Works" section |
| `projects-tab.png` | Projects tab, model-mix pills visible | Supports cost-per-KB claim |
| `explain-terminal.png` *(optional)* | `token-roi explain` terminal output | Trust-builder for auditability section |

## Additional README polish

- Move **Demo** section ("The Black Hole" scenario) up — currently
  buried on line 200. Should be second screenful.
- Kill the 🆕 update banner and 🔒 local-first banner once they're
  integrated into the hero.
- Pillars table becomes a 4-column feature grid after the second
  screenshot.
- Replace "Acknowledgement" at bottom with a Star History chart
  (https://star-history.com/). Becomes social proof once stars climb.
- Add "Who's using it" section as soon as you have 2-3 real users.
  Outperforms any feature claim for driving stars.

## Pre-launch README checklist

- [ ] hero.gif < 10MB, in docs/assets/
- [ ] 5 PNG screenshots captured
- [ ] Hero block replaced with NEW_README_HERO.md content
- [ ] Demo section moved up
- [ ] Install command in a code block above the fold
- [ ] README checked on mobile GitHub view (~half HN traffic is mobile)
- [ ] All names in screenshots are clearly fake
- [ ] No internal Jira/Linear links accidentally in screenshots
- [ ] Chinese README (`README.zh.md`) mirrors the same hero structure
