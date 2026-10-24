# Reflect

Codex plugins that let a coding agent learn from its conversations and turn
lessons into reviewed, project-level guidance.

## Reflect (`plugins/reflect`)

Captures feedback and proposes durable guidance (e.g. `AGENTS.md` edits) through
a normal PR in the project you're working in.

- **Capture**: a `UserPromptSubmit` hook saves messages starting with
  `remember` / `please remember`. With `REFLECT_OPENROUTER_API_KEY` set, other
  messages get best-effort redaction of common credential formats and are
  classified by TypeSafe's Jev model via OpenRouter; likely lessons
  (score ≥ 0.8) are saved too. Only candidates are stored, never full
  conversations.
- **Reminder**: a `SessionStart` hook tells the agent how many captures are
  unreviewed, so the user is prompted to reflect.
- **Reflect**: ask the agent to "reflect", "reflect on past conversations",
  "preview reflection", or "review conflicts". See the skill's `SKILL.md`.
- **Daily report (optional)**: a cron job writes `.codex/reflect/report.md`.

Captures live in each project's `.codex/reflect/` (main checkout, shared by its
worktrees). That folder ignores itself in Git.

### Install

Requires Python 3.9+, Git, macOS/Linux, and Codex.

```bash
codex plugin marketplace add synergev/reflect --ref v0.1.2
```

```bash
codex plugin add reflect@reflect
```

Then start a new session, and review and trust the plugin's two hooks in
`/hooks`. Update later with `codex plugin marketplace upgrade` (or re-add with a
newer `--ref`).

Optional:

- **Detection**: create a dedicated OpenRouter key and set
  `REFLECT_OPENROUTER_API_KEY` in the environment or the project's gitignored
  `.env` (a committed `.env` is ignored). Requests ask OpenRouter for Zero Data
  Retention routing, so they fail rather than reach an endpoint that retains
  prompts.
- **Daily report**:
  ```
  0 8 * * * cd <main-checkout> && codex exec --sandbox workspace-write 'Use $reflect to write the daily report.'
  ```

### Security and privacy

- Once installed, the hooks run in every project you open with Codex. Without a
  key, nothing leaves your machine.
- With detection on, your messages (after best-effort credential redaction) are
  sent to OpenRouter. Redaction does not catch every secret format or personal
  data. Leave detection off in projects where that matters.
- Lessons land in `AGENTS.md`, which steers every agent in the repository.
  Review them like code: a lesson can come from content pasted into a chat.
- Reflect ignores a `.env` or `.codex/reflect/` folder committed to a
  repository, so a cloned project can't supply its own key or fake feedback.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

### Tests

```bash
cd plugins/reflect/skills/reflect && python3 -B -m unittest discover -s scripts -p 'test_*.py' -v
```

## License

MIT. See `LICENSE`.
