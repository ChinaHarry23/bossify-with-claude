# Launch Assets — Index

Everything you need to take Bossify from "good repo" to "trending on GitHub."

## Read in this order

1. **launch-checklist.md** — what must be done before the Show HN link goes live
2. **readme-hero-rewrite.md** — why the current hero underperforms and the spec for the fix
3. **NEW_README_HERO.md** — drop-in replacement for the top of README.md
4. **demo-storyboard.md** — 75-second demo video, beat-by-beat
5. **show-hn.md** — Show HN title, body, timing, and first-hour hygiene
6. **twitter-thread.md** — 6-tweet launch thread with tag strategy
7. **hn-reddit-timing.md** — when to launch and how to sequence the week
8. **content-calendar.md** — 12 weeks of post-launch content to sustain growth

## The honest forecast

- **1000 stars/day every day** is not a realistic steady state for a niche
  dev tool. Projects in that tier (ollama, shadcn, Bun) have months of
  momentum + broad utility.
- **1000+ on launch day, then 50–200/day tail** is very achievable for
  Bossify if the assets above ship.
- Realistic target for a clean launch week: **2–5k stars**, plateau at
  50–150/day for 2–4 weeks, then settle. Push past that only with (a)
  a killer feature every 2–3 weeks or (b) a team/SaaS version covered
  by TechCrunch / The Information.

## What's already done in this repo (this session)

- Added missing `LICENSE` file (MIT, matching README claim)
- Added `.github/ISSUE_TEMPLATE/` (bug + feature)
- Added `.github/FUNDING.yml`
- Added `.github/workflows/tests.yml` (CI on push/PR, 3.11 + 3.12)
- Added `CONTRIBUTING.md`
- Removed committed `.DS_Store` (already in .gitignore)
- All 8 launch docs above

## What still needs a human

- Visual assets: `hero.gif` + 4 PNG screenshots (see demo-storyboard.md)
- Replace README hero with `NEW_README_HERO.md` contents
- Set GitHub repo description, topics, and social preview image
- Clean end-to-end install test on a fresh machine
- Pick the launch date (see hn-reddit-timing.md for trigger events)
