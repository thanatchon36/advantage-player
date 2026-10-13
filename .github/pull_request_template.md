## What and why

## Risk impact

<!-- Does this touch sizing, caps, discipline rules (SPEC §7), the lifecycle gates or the live switch? If yes, which test fails without it? -->

## Checklist

- [ ] `uv run ruff check . && uv run mypy src tests && uv run pytest -q` passes
- [ ] Discipline rules still each have a failing-if-violated test
- [ ] SPEC ambiguity resolved conservatively and recorded in `PROGRESS.md`
- [ ] README updated (commands, config, go-live checklist)
