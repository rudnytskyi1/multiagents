---
name: setup
description: Set up or check the multiagents team from Codex. Covers providers (Fireworks / Hive / DeepSeek direct), API keys, model availability, the credential-isolation self-test, per-project worker permissions, and updating the plugin. Use when the user wants to install, configure or verify multiagents, switch a project's provider, or when a multiagents command fails with a setup error.
---

# multiagents setup

The CLI ships inside this plugin: `<folder of this SKILL.md>/../../../bin/multiagents` (on
Windows `bin\multiagents.cmd`). Use its absolute path, written below as `multiagents`. Its
commands need the network, the Keychain and `~/.multiagents`, which the Codex sandbox blocks:
run them with `sandbox_permissions: "require_escalated"`.

1. Run `multiagents doctor`. Report each line to the user in plain words.

2. **Claude Code engine.** Workers are headless Claude Code processes, so the `claude` CLI must
   be installed even though the lead is Codex (`doctor` checks it). Workers never use the user's
   Claude or ChatGPT login — only the provider key.

3. **Providers.** Three are built in; `multiagents provider` shows which is active.
   - `fireworks` (default) — DeepSeek models served by Fireworks AI on US servers.
   - `hive` — DeepSeek V4.1 Flash served by Hive (thehive.ai) on US servers; the cheapest per
     token. Hive only offers an OpenAI-style API, so each run starts a small local bridge that
     translates for the worker (automatic; nothing to set up beyond the key).
   - `deepseek` — DeepSeek's own api.deepseek.com, but requests go to servers in China; make
     sure the user is comfortable with that for the project's code (they typically use it for
     personal projects only).
   Per project: `{"provider": "hive"}` in `.claude/multiagents.json`. User-wide default:
   `multiagents provider hive`. One-off: env `MULTIAGENTS_PROVIDER=hive`.

4. **API key missing.** Never ask the user to paste the key into chat. Each provider has its own
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
   405 there means the Hive organization is out of credit. If `doctor` says the key is missing although the user stored
   it, check that the command really ran escalated — the sandbox hides the Keychain.

5. **A model FAILs.** Run `multiagents models` to see what the key can use, then set models in
   `~/.multiagents/config.json` (all projects) or `.claude/multiagents.json` (this project):

   ```json
   { "models": { "coder": "<id>", "reviewer": "<id>" } }
   ```

   Optionally add `"prices": { "<model id>": { "input": 0.3, "cached_input": 0.006, "output": 1.2 } }`
   (USD per 1M tokens) so runs report a cost estimate. Fireworks Flash, Hive's Flash and
   DeepSeek's own models are prefilled (DeepSeek's at peak rates; off-peak is half).

6. **Isolation FAILs.** Stop. Don't run workers: on this Claude Code build a worker could send
   the user's Claude credentials to the provider. Suggest re-running `multiagents selftest`
   after a Claude Code update.

7. **Project settings (optional).** A repo's `.claude/multiagents.json` may set only:
   `provider`, `models`, `aliases`, `prices`, `allow`, `deny`, `permission_mode` (not
   bypassPermissions), `max_turns`, `timeout_minutes`, `idle_timeout_minutes`, `use_branches`,
   `worktrees`, `worktree_link` (paths inside the repo only), `branch_prefix`. Anything else (endpoints, key sources,
   binaries, worker env) is ignored from project files by design — it belongs in
   `~/.multiagents/config.json`. Propose additions to `"allow"` when a worker's summary shows
   denied commands the project genuinely needs, and show the user before writing:

   ```json
   { "allow": ["Bash(./scripts/test.sh*)"] }
   ```

8. **Updating the plugin.** `multiagents install-codex` (escalated) keeps the registered
   marketplace source, refreshes it (a local clone needs a `git pull` first) and reinstalls the
   plugin; then the user starts a new thread.
