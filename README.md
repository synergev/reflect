# agent-auto-evolve

Agent skills that let a coding agent learn from its conversations and turn
lessons into reviewed, project-level guidance.

## Reflect (`.agents/skills/reflect`)

A Codex skill that captures feedback and proposes durable guidance (e.g.
`AGENTS.md` edits) through a normal PR.

- **Capture**: a `UserPromptSubmit` hook saves messages starting with
  `remember` / `please remember`. With `TYPESAFE_API_KEY` set, other messages
  are redacted (keys, tokens, passwords, credentials) and classified by
  TypeSafe's Jev model; likely lessons (score ≥ 0.8) are saved too. Only
  candidates are stored, never full conversations.
- **Reminder**: a `SessionStart` hook tells the agent how many captures are
  unreviewed, so the user is prompted to reflect.
- **Reflect**: ask the agent to "reflect", "reflect on past conversations",
  "preview reflection", or "review conflicts". See `SKILL.md`.
- **Daily report (optional)**: a cron job writes `.codex/reflect/report.md`.

### Install into a project

Requires Python 3.9+, Git, macOS/Linux, and Codex with hooks.

1. Copy `.agents/skills/reflect/` into the project at the same path.
2. Merge the two hooks from `.codex/hooks.json` into the project's
   `.codex/hooks.json`, then review and trust them in Codex's `/hooks`.
3. Add `/.codex/reflect/` to the project's `.gitignore`. Captures and review
   decisions live there, in the main checkout, shared by all worktrees.
4. Optional: export `TYPESAFE_API_KEY` to enable detection. TypeSafe's data
   retention terms are not published; confirm them before enabling.
5. Optional daily report:
   ```
   0 8 * * * cd <main-checkout> && codex exec --sandbox workspace-write 'Use $reflect to write the daily report.'
   ```

### Tests

```bash
cd .agents/skills/reflect && python3 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

## License

MIT. See `LICENSE`.
