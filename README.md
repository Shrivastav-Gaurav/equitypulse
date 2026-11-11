# EquityPulse

Personal Indian-equity trade planner: on-demand scans return up to 5 trade plans (buy price,
target, stop, quantity, horizon); a monitor emails sell alerts for holdings. Agents handle
language-shaped work (scan prompts, filings, rationales); deterministic Python handles
everything money-shaped.

- PRD: https://claude.ai/artifact/5cMzfzS9JsqWjkSShr8PpU (private)
- Build rules: `CLAUDE.md` · Current phase: `docs/phase0_spec.md` · Tasks: `docs/phase0_tasks.md`

## Setup (Windows)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv sync
copy .env.example .env   # then fill in the keys
uv run pytest
```

Not investment advice; personal use only.
