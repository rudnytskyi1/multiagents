# Using multiagents from OpenAI Codex

The lead protocol also ships as a Codex **skill** (Codex deprecated custom prompts in favor of
skills in 2026). The worker side is unchanged: workers always run on the Claude Code engine,
so the `claude` CLI must be installed and a provider key configured regardless of which
assistant plays the lead.

## Install

From a Claude Code session with the plugin enabled (or from a clone:
`<clone>/bin/multiagents install-codex`):

```bash
multiagents install-codex
```

This:

- copies `codex/skills/multiagents-lead/` → `~/.agents/skills/multiagents-lead/` (Codex's
  personal skills directory; use `--dir` to target `.agents/skills` inside one repo instead);
- links `~/.local/bin/multiagents` → the CLI, and tells you if `~/.local/bin` is not on PATH
  or another `multiagents` shadows it;
- warns when installed from the Claude plugin cache — that path changes on plugin updates, so
  re-run `install-codex` after updating the plugin.

Codex picks up new skills on the next session.

## Use

In a new Codex session, inside a git repo:

```
$multiagents-lead add a settings screen with a dark-mode toggle
```

(or pick it from `/skills`; Codex may also trigger it implicitly when you ask to build
something "with the team"). The protocol is the same as in Claude Code: spec → coder →
reviewer → lead review → feedback/fixer → accept.

## Codex-specific notes

- **Network approvals:** Codex sandboxes shell commands; `multiagents run …` needs network (the
  worker talks to the model API). When Codex asks to approve network access for the command,
  that is expected. With `codex exec`, use a sandbox mode that allows it.
- **Blocking runs:** unlike Claude Code's background Bash, the Codex lead runs
  `multiagents run …` synchronously and reads the summary when it finishes.
- **Visual review:** the Codex skill tells the lead to use `multiagents shot` for screenshots
  and its own harness abilities for looking at the app; there is no iOS-Simulator integration
  assumption.
- **Keys:** by default Codex does **not** strip `*_API_KEY` variables from subprocess env
  (`shell_environment_policy.ignore_default_excludes` defaults to true). multiagents strips
  them from *worker* processes itself, but if you keep provider keys in your shell profile,
  consider Codex's `shell_environment_policy` to hide them from other commands too.
- Codex's own model/config (`~/.codex/config.toml`) is untouched — multiagents only adds the
  skill and the PATH link.
