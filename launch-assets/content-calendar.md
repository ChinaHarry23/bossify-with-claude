# Post-Launch Content Calendar — Keeping Stars Flowing

Launch spikes. What sustains growth is a steady drip of content that
keeps the repo in people's feeds for 3–6 months.

Target cadence: **one public artifact per week** for 12 weeks. Each
one links back to the repo.

## Week-by-week

**Week 1 — Launch week**
See `hn-reddit-timing.md`.

**Week 2 — The data post**
"We analyzed 10,000 public Claude Code sessions. Here's what wastes
the most tokens." Scrape public Claude Code session transcripts from
GitHub gists / blog posts where people paste them, run Bossify, publish
the aggregate stats. This is the piece that gets a second HN cycle
and cements the "authority on AI coding ROI" position.

**Week 3 — The anti-pattern series (Part 1)**
Blog post: "The 5 most expensive mistakes in Claude Code sessions."
Use your own scored data. Each anti-pattern gets a name, an example,
and the cost. Makes the terms ("planning loop", "memory orphan",
"retry hell") stick.

**Week 4 — The feature drop**
Ship something visible: e.g. Cursor session support, a Slack
integration, or a "pre-flight prompt linter" that warns before a
session turns into a black hole. Post-it on X with a GIF.

**Week 5 — Guest content**
Guest post or podcast. Latent Space (swyx), Changelog, or a Chinese
podcast like 42章经. The pitch: "we measured what AI coding actually
costs and it's not what you think."

**Week 6 — The anti-pattern series (Part 2)**
"What makes a Claude Code session HIGH_VALUE — the 11% that justify
the other 89%." Flip the frame from waste to value. Same data,
positive angle, different audience slice reshares it.

**Week 7 — Benchmark / comparison**
"Claude Code vs Cursor vs Aider: which has the best ROI per token?"
Controversial on purpose. Methodology transparent. Even if the answer
is "they're close", the headline spreads.

**Week 8 — The team case study**
Get one real team to let you publish their anonymized Bossify report.
"How a 12-engineer team cut their AI bill 34% in 3 weeks." This is
the first B2B-flavored piece and seeds inbound for the SaaS version
if you build one.

**Week 9 — The feature drop (take 2)**
Ship a second visible feature. Could be: GitHub PR comment bot
(posts ROI verdict on every AI-generated PR), a webhook for Slack
weekly digests, or pre-flight budget guardrails.

**Week 10 — The manifesto**
Long-form essay: "Vibe-coding is capital allocation." Opinion piece
positioning Bossify as the start of a category. Publish on your own
blog + cross-post everywhere. This is the piece that gets quoted
in "state of AI coding" reports.

**Week 11 — Community moment**
Launch something community-facing. A public leaderboard for
open-source Claude Code session logs, or a "worst session of the
week" submission. Controversy + virality.

**Week 12 — 90-day retrospective**
"What happened in 90 days: star growth, lessons learned, what's next."
Includes a roadmap. Transparency builds trust. This piece is what
gets you your next wave of contributors.

## Evergreen mechanics

Set up once, benefits forever:

- **Star History chart** in the README — auto-updates, visual social
  proof.
- **GitHub Releases with rich notes** — every release becomes a
  post-worthy artifact. Write release notes like press releases.
- **A "Who's using Bossify" section** in the README — start collecting
  logos as soon as anyone uses it. Logos outperform stars for
  B2B-adjacent tools.
- **An email capture** on a minimal landing page — converts GitHub
  visitors into a list you own. A Substack works; don't overthink it.
- **Answer "how do I measure AI coding ROI" questions** on Stack
  Overflow, r/ExperiencedDevs, HN comments, Quora. Play the long
  SEO game.

## What to track

Weekly: stars, unique cloners (Insights → Traffic), issues opened by
non-you accounts, PR submissions, Discord/Twitter mentions.

The leading indicator for sustained growth is **non-author issues**.
If issues stay flat after launch, the top-of-funnel is fine but the
product isn't sticking. Iterate on activation (first-run experience,
time-to-first-verdict).
