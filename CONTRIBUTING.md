# Contributing to Bossify

Thanks for wanting to help. Bossify aims to be the most honest
measurement tool for agentic coding — every PR that makes verdicts
sharper, more explainable, or catches a real waste pattern is welcome.

## Good first contributions

- **New waste patterns** — if you've spotted a specific anti-pattern
  the judge misses, open an issue with a concrete example (session
  transcript or description of the behavior). New patterns live in
  `src/token_roi/roi.py`.
- **New locales** — the UI supports EN and 中文. Adding a new locale
  is mostly string-table work.
- **Importers for other tools** — Cursor session logs, Aider chat
  history, OpenAI Codex CLI. The importer contract is documented in
  `references/`.
- **Dashboard tweaks** — new charts, new drill-downs. FastAPI +
  ECharts.

## Development setup

```bash
git clone https://github.com/chinaharry/bossify-with-claude
cd bossify-with-claude
pip install -e '.[dev,dashboard,embeddings]'
pytest
```

## PR conventions

- One logical change per PR. Small PRs get reviewed faster.
- Include a test when you fix a bug or add a waste pattern.
- If you change the judge rubric, include a before/after example on
  a real session — judge changes should be defensible.
- Keep the adversarial tone. Bossify is deliberately harsh; don't
  soften verdicts to be nice.

## Philosophy

If you're tempted to add a feature that makes scores *higher* on
average, stop and ask: does this measure more real value, or does
it just feel kinder? Bossify anchors at 0.5 on purpose.
