---
name: setup
description: Set up or check the multiagents team. Covers providers (Fireworks / Hive / DeepSeek direct), API keys, model availability, the credential-isolation self-test, per-project worker permissions, and the Codex install. Use when the user wants to install, configure or verify multiagents, switch a project's provider, or when a worker run fails with a setup error.
---

# multiagents setup

1. Run `multiagents doctor`. If `multiagents` is not on PATH, use
   `${CLAUDE_SKILL_DIR}/../../bin/multiagents`. Report each line to the user in plain words.

2. **Providers.** Three are built in; `multiagents provider` shows which is active.
   - `fireworks` (default) — DeepSeek models served by Fireworks AI on US servers.
   - `hive` — DeepSeek V4.1 Flash served by Hive (thehive.ai) on US servers; the cheapest per
     token. Hive only offers an OpenAI-style API, so each run starts a small local bridge that
     translates for the worker (automatic; nothing to set up beyond the key).
   - `deepseek` — DeepSeek's own api.deepseek.com, but requests go to servers in China; make
     sure the user is comfortable with that for the project's code (they typically use it for
     personal projects only).
   Per project: `{"provider": "hive"}` in `.claude/multiagents.json`. User-wide default:
   `multiagents provider hive`. One-off: env `MULTIAGENTS_PROVIDER=hive`.

3. **API key missing.** Never ask the user to paste the key into chat. Each provider has its own
   key. Tell them to run the matching command in their own terminal (it prompts for the key, so
   it stays out of shell history):

   ```bash
   security add-generic-password -s fireworks-api -a "$USER" -w
   ```

   ```bash
   security add-generic-password -s deepseek-api -a "$USER" -w
   ```

   ```bash
   security add-generic-password -s hive-api -a "$USER" -w
   ```

   (For Hive: the V3 "Secret Key" from Service API Keys on thehive.ai.) Not on macOS?
   `export FIREWORKS_API_KEY=…` / `DEEPSEEK_API_KEY=…` / `HIVE_API_KEY=…` in the shell profile,
   or `~/.multiagents/<provider>.key` with `chmod 600`. Run `doctor` again afterwards.

   Hive specifics: it has no model list, so `doctor` checks the model with a real request; HTTP
   405 there means the Hive organization is out of credit.

4. **A model FAILs.** Run `multiagents models` to see what the key can use, then set models in
   `~/.multiagents/config.json` (all projects) or `.claude/multiagents.json` (this project):

   ```json
   { "models": { "coder": "<id>", "reviewer": "<id>" } }
   ```

   Optionally add `"prices": { "<model id>": { "input": 0.3, "cached_input": 0.006, "output": 1.2 } }`
   (USD per 1M tokens) so runs report a cost estimate. Fireworks Flash, Hive's Flash and
   DeepSeek's own models are prefilled (DeepSeek's at peak rates; off-peak is half).

5. **Isolation FAILs.** Stop. Don't run workers: on this Claude Code build a worker could send
   the user's Claude credentials to the provider. Suggest re-running `multiagents selftest`
   after a Claude Code update.

6. **Project settings (optional).** A repo's `.claude/multiagents.json` may set only:
   `provider`, `models`, `aliases`, `prices`, `allow`, `deny`, `permission_mode` (not
   bypassPermissions), `max_turns`, `timeout_minutes`, `idle_timeout_minutes`, `use_branches`, `worktrees`,
   `worktree_link` (paths inside the repo only), `branch_prefix`. Anything else (endpoints, key
   sources, binaries, worker env) is ignored from project files by design — it belongs in
   `~/.multiagents/config.json`. Propose additions to
   `"allow"` when a worker's summary shows denied commands the project genuinely needs, and show
   the user before writing:

   ```json
   { "allow": ["Bash(./scripts/test.sh*)"] }
   ```

7. **Codex.** To use the same team from OpenAI Codex, run `multiagents install-codex`: it
   registers this repo as a Codex plugin marketplace and installs the plugin through Codex's own
   `codex plugin` CLI (found on PATH or inside the ChatGPT/Codex desktop app). Running it again
   updates the plugin and keeps the registered source. In a new Codex
   thread the user then types `$multiagents:lead <task>`. Workers still run on the Claude Code
   engine, so the `claude` CLI must remain installed.
