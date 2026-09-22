---
name: setup
description: Set up or check the multiagents team. Covers the Fireworks API key, DeepSeek model availability, the credential-isolation self-test, and per-project worker permissions. Use when the user wants to install, configure or verify multiagents, or when a worker run fails with a setup error.
---

# multiagents setup

1. Run `multiagents doctor`. If `multiagents` is not on PATH, use
   `${CLAUDE_SKILL_DIR}/../../bin/multiagents`. Report each line to the user in plain words.

2. **API key missing.** Never ask the user to paste the key into chat. Tell them to run this in
   their own terminal (it prompts for the key, so it stays out of shell history):

   ```bash
   security add-generic-password -s fireworks-api -a "$USER" -w
   ```

   Not on macOS? They can export `FIREWORKS_API_KEY` in their shell profile, or save the key to
   `~/.multiagents/fireworks.key` with `chmod 600`. Run `doctor` again afterwards.

3. **A model FAILs.** Run `multiagents models` to see which DeepSeek models the key can use. Then
   set the models in `~/.multiagents/config.json` (all projects) or `.claude/multiagents.json`
   (this project):

   ```json
   { "models": { "coder": "accounts/fireworks/models/<id>", "reviewer": "accounts/fireworks/models/<id>" } }
   ```

   Optionally add `"prices": { "<model id>": { "input": 0.22, "cached_input": 0.007, "output": 0.66 } }`
   (USD per 1M tokens) so runs report a cost estimate.

4. **Isolation FAILs.** Stop. Don't run workers. On this Claude Code build a worker could send the
   user's Claude credentials to Fireworks. Explain this to the user and suggest re-running
   `multiagents selftest` after a Claude Code update.

5. **Project permissions (optional).** Workers run with a built-in allowlist of common read, git,
   build and test commands. A command outside it is denied and shows up in the run summary. For a
   project that needs more (a custom script, a specific toolchain), propose a
   `.claude/multiagents.json` and show it to the user before writing it:

   ```json
   { "allow": ["Bash(./scripts/test.sh*)"], "deny": [] }
   ```

   Other keys: `permission_mode`, `max_turns`, `timeout_minutes`, `idle_timeout_minutes`,
   `use_branches`, `branch_prefix`, `worker_env`. The defaults are in `worker/multiagents.py`
   (`DEFAULT_CONFIG`).
