# Getting started

## 1. Install the plugin

In any Claude Code session:

```
/plugin marketplace add rudnytskyi1/multiagents
/plugin install multiagents@multiagents
```

(From a local clone: `claude plugin marketplace add /path/to/multiagents`, then the same
install command. CLI installs also load in the desktop app's Code tab. Restart the session
after installing.)

## 2. Store an API key

Pick a provider ([providers.md](providers.md)) and store its key — the command prompts for the
key so it never lands in shell history:

```bash
security add-generic-password -s fireworks-api -a "$USER" -w     # Fireworks
```

```bash
security add-generic-password -s deepseek-api -a "$USER" -w      # DeepSeek direct
```

Not on macOS: `export FIREWORKS_API_KEY=…` (or `DEEPSEEK_API_KEY`) in your shell profile, or
write the key to `~/.multiagents/fireworks.key` / `deepseek.key` and `chmod 600` it. On
Windows: `%USERPROFILE%\.multiagents\fireworks.key`, or a user environment variable with the
same name (see the Windows section in [troubleshooting.md](troubleshooting.md#windows)).

## 3. Verify

```bash
multiagents doctor
```

Expect every line OK/PASS: binary, provider, key, endpoint, models, the credential-isolation
self-test, repo state. In Claude Code you can instead say `/multiagents:setup` and let the
model walk through it.

## 4. First task

Open a session in a **git repo on a named branch** (uncommitted changes are fine — workers
run in their own worktrees from the last commit and never touch your tree), pick your strongest
model as the lead, and type:

```
/multiagents:lead <what you want built or fixed>
```

What you will see, in order:

1. The lead posts a short plan and creates a task (`multiagents new …`), then fills
   `.multiagents/tasks/T001-…/task.md` with a precise spec.
2. `multiagents run coder T001` runs in the background for a few minutes. Its summary shows
   what the worker did, tokens, and an estimated cost (typically cents).
3. `multiagents run reviewer T001` re-verifies: build, tests, and your review agents from
   `.claude/agents/`.
4. The lead reviews the diff itself and, for UI work, runs the app (simulator / browser) and
   looks at it. Then either `accept` (merges `ma/T001-…` into your branch) or a
   `feedback-1.md` with numbered points and screenshots, fixed by
   `multiagents run fixer T001 --feedback feedback-1.md`.
5. You get a report: what was done, verdict, rounds, cost. Repeat for the next task.

You can interject at any time: "покажи статус" / "what's the status", "прими" / "accept it",
"отправь фидбек: …". The lead drives the CLI; you talk to the lead.

## 5. Per-project setup (optional)

Commit a `.claude/multiagents.json` to pin a provider or extend worker permissions:

```json
{
  "provider": "deepseek",
  "allow": ["Bash(./scripts/test.sh*)"]
}
```

Notes for real projects:

- Workers commit as the repo's git identity — set `git config user.name/email` (or global).
- For iOS projects, put the exact `xcodebuild … build`/`test` command into each task's
  Verification section once; workers reuse it.
- Watch the first runs' "denied" lines in summaries and grow `allow` deliberately.

## Uninstall / disable

```
claude plugin disable multiagents@multiagents     # keep, but off
claude plugin uninstall multiagents@multiagents   # remove
```

State to clean up if you want a full wipe: `~/.multiagents/` and any `.multiagents/` folders
inside repos (they are never committed).
