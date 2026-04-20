# Bossify Launch Readiness Checklist

Everything needed before the Show HN link goes live. Grouped by
blocking vs. nice-to-have. Every item takes minutes, not days.

## 🚨 Blockers — don't launch without these

### Repo hygiene
- [x] `LICENSE` file actually exists (added — the README claimed MIT
      but no file existed)
- [x] `.github/ISSUE_TEMPLATE/` with bug + feature templates
- [x] `.github/FUNDING.yml`
- [ ] Remove committed `.DS_Store` and add to `.gitignore`
- [ ] GitHub repo has: description, homepage URL, topics
      (`claude`, `ai-coding`, `local-llm`, `llm-ops`, `roi`,
      `developer-tools`, `agentic-coding`), proper About section
- [ ] GitHub repo "social preview" image set (Settings → Social preview
      → upload the `verdict-card.png` or a custom 1280×640)

### Visual assets (see `demo-storyboard.md` and `readme-hero-rewrite.md`)
- [ ] `docs/assets/hero.gif` — 15s loop < 10MB
- [ ] `docs/assets/people-tab.png`
- [ ] `docs/assets/overview-tab.png`
- [ ] `docs/assets/verdict-card.png`
- [ ] `docs/assets/projects-tab.png`

### README
- [ ] Hero section replaced with `NEW_README_HERO.md` content
- [ ] Demo section ("Scenario 1: The Black Hole") moved up to second
      screenful
- [ ] Mobile GitHub view checked — no layout breaks
- [ ] `README.zh.md` mirrors the new structure

### Product
- [ ] `pip install -e '.[dashboard,embeddings]'` works on a clean
      Python 3.11 + 3.12 environment (test both)
- [ ] `token-roi init` → `import` → `score` → `judge` → `dashboard`
      works end-to-end on a fresh `~/.claude/projects/` directory
- [ ] LM Studio default endpoint works out of the box
- [ ] Ollama endpoint documented and tested (`--endpoint
      http://localhost:11434/v1`)
- [ ] First-run error messages are human-readable (no raw tracebacks
      for common cases: LM Studio not running, no model loaded, empty
      Claude Code history)
- [ ] Dashboard renders without JS errors in Chrome, Safari, Firefox

## ⚠️ Strongly recommended — ship if time allows

- [ ] CI: GitHub Actions running `pytest` on pushes. Green badge in
      README. Failing CI on launch day is a bad look.
- [ ] A `CONTRIBUTING.md` — even a short one. Signals the project is
      ready for outside help.
- [ ] A `CHANGELOG.md` or at least good release notes for v0.1.0
- [ ] GitHub Release for v0.1.0 tagged — release page is what HN
      visitors land on after "view all releases"
- [ ] `CODE_OF_CONDUCT.md` — one-page Contributor Covenant template
- [ ] A quick `docs/INSTALL_TROUBLESHOOTING.md` with the 5 most
      likely first-run failures and fixes

## 🎨 Nice-to-have — optional but high-leverage

- [ ] A `landing` page (GitHub Pages or Vercel) at
      `bossify.dev` or similar. Hero section, embedded demo video,
      big "Star on GitHub" button. Converts search traffic.
- [ ] Demo data shipped in the repo so people can run the dashboard
      without having Claude Code history — huge for demos and for
      people who just want to see what it looks like
- [ ] Docker compose: `docker compose up` gives you dashboard +
      Ollama + a pre-loaded demo dataset. Massively reduces
      activation friction.
- [ ] A Vercel / Railway / Fly.io deployed **public demo** with the
      fake team data, read-only. "Try it live" button from README.
      This alone can double star conversion.
- [ ] A `homebrew` tap: `brew install chinaharry/tap/bossify`.
      Signals maturity.
- [ ] Pre-built PyPI package: `pip install token-roi` without
      the git clone step. Drops activation to 30 seconds.

## Pre-launch day-of ritual

Morning of launch:
- [ ] `git pull && pytest` — green
- [ ] Dashboard runs locally — no warnings
- [ ] Star count screenshotted (for post-launch "N stars in M hours"
      tweets)
- [ ] `launch-assets/show-hn.md` title + body pasted into a text file
      for fast submission
- [ ] Twitter thread drafted in Typefully or native X drafts
- [ ] LM Studio and Ollama both open on your machine (you'll be asked)
- [ ] Phone on do-not-disturb; clear your calendar for 4 hours after
      HN submission
- [ ] Water, snacks, comfortable chair — you'll be replying to
      comments for hours

## Post-launch checklist (first 24h)

- [ ] Reply to every HN comment within 20 minutes for first 4 hours
- [ ] Reply to every substantive X reply/QT
- [ ] Collect any good quotes from comments for future social proof
- [ ] Track stars hourly — if you hit >50/hour sustained, post a
      "first hour stats" update
- [ ] If issues are opened, triage within 2 hours. Fast response =
      good first impression = starred repo
- [ ] Take a breath. This compounds over weeks, not hours.
