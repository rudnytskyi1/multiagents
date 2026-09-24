# Using multiagents from OpenAI Codex

The repository is also a **Codex plugin** (`.codex-plugin/plugin.json`) and its own Codex
**marketplace** (`.agents/plugins/marketplace.json`). Codex gets its own versions of the two
skills, in `codex/skills/`:

| Codex | Claude Code | What |
|---|---|---|
| `$multiagents:lead <task>` | `/multiagents:lead <task>` | team mode: plan → spec → workers → review → accept |
| `$multiagents:setup` | `/multiagents:setup` | check keys, providers, models, isolation |

The worker side does not change: workers always run on the Claude Code engine, so the `claude`
CLI must be installed and a provider key configured whichever assistant is the lead.

## Install

```bash
multiagents install-codex
```

Run it from a Claude Code session with the plugin enabled, or from a clone as
`<clone>/bin/multiagents install-codex`. It drives Codex's own plugin CLI:

1. finds `codex` — on PATH, or the copy inside the desktop app
   (`/Applications/ChatGPT.app/Contents/Resources/codex` or `Codex.app`); `--codex PATH` overrides;
2. registers the marketplace (`codex plugin marketplace add …`) — from a git clone it registers
   that folder; otherwise (e.g. from the Claude Code plugin cache) the GitHub repo. If a
   `multiagents` marketplace already exists, its source is kept (a Git one is refreshed with
   `codex plugin marketplace upgrade`); `--source` replaces it with a folder (a path starting
   with `/`, `.` or `~`), `owner/repo[@ref]` or a git URL, and the old source comes back if the
   new one fails. The `codex` commands run from your home folder, so the repo you happen to be
   in cannot redefine the marketplace through its own `.codex/config.toml`;
3. installs the plugin (`codex plugin add multiagents@multiagents`);
4. deletes what versions up to 0.3.0 installed: the standalone
   `~/.agents/skills/multiagents-lead` (it would duplicate the plugin's skill) and a
   `~/.local/bin/multiagents` link or shim whose target is gone. A still-working link is left
   alone (you may use it in your shell). If you installed with the old `--dir`, delete
   `<dir>/multiagents-lead` yourself.

The same commands by hand:

```bash
codex plugin marketplace add rudnytskyi1/multiagents
```

```bash
codex plugin add multiagents@multiagents
```

Codex picks up plugins in **new** threads.

**Updating:** run `multiagents install-codex` again — it keeps the source you registered.
By hand: for a local clone, `git pull` in the clone, then `codex plugin add
multiagents@multiagents` (it re-copies the plugin even at the same version); for a GitHub
source, `codex plugin marketplace upgrade multiagents` first. Codex may refresh Git
marketplaces on its own, but a local clone is only re-read when you run one of these.

## Use

In a new Codex thread, inside a git repo on a named branch:

```
$multiagents:lead add a settings screen with a dark-mode toggle
```

`lead` is explicit-only (`allow_implicit_invocation: false`), like `disable-model-invocation`
in Claude Code: Codex never starts team mode on its own. `setup` may be picked implicitly when a
setup question comes up. The protocol is the same as in Claude Code: spec → coder → reviewer →
lead review → feedback/fixer → accept.

## The Codex sandbox

Codex runs shell commands in a sandbox: no network, no Keychain, nothing writable outside the
workspace, and `.git` read-only. Workers need all four, so:

- The lead skill tells Codex to run `run`, `doctor`, `selftest`, `models`, `accept`, `reject`,
  `sync`, `shot` and `provider <name>` **escalated** from the start, suggesting a per-subcommand
  prefix rule (`[".../bin/multiagents", "run"]`), so you approve each kind of command once.
  The path contains the plugin version, so after an update Codex asks once more.
- If one of those runs sandboxed anyway, the CLI detects it (`CODEX_SANDBOX` on macOS,
  `CODEX_SANDBOX_NETWORK_DISABLED` on any platform) and refuses with a message telling Codex to
  re-run it escalated, instead of failing confusingly (a hidden Keychain otherwise looks like
  "no API key found").
- `new`, `feedback`, `status`, `digest` and `diff` work inside the workspace-write sandbox.
  `.multiagents/` is added to `.git/info/exclude` by the next command that runs outside it.
  In read-only mode, the commands that write (`new`, `feedback`) say so and ask for escalation.
- The sandbox hides other processes, so `status` shows a worker as running whenever its process
  may still be alive (it cannot tell from inside).
- Escape hatch for custom sandbox setups: `MULTIAGENTS_ALLOW_SANDBOX=1` disables the check.

Approving an escalated `multiagents run` means the CLI runs outside the sandbox, as it does in
Claude Code; the workers are still restricted by multiagents itself (allowlist, credential-path
denies — including `~/.codex/auth.json` — and a clean environment without your Claude or
ChatGPT logins).

## Codex-specific notes

- **Which CLI:** the skills call the CLI bundled in the installed plugin
  (`~/.codex/plugins/cache/multiagents/multiagents/<version>/bin/multiagents`), not whatever
  `multiagents` is on PATH, so the protocol and the CLI always match.
- **Long runs:** a `run` blocks for minutes; the skill tells Codex to give it a timeout above
  `timeout_minutes` or keep it as a background session, so it isn't killed midway (a killed run
  shows up as `interrupted`).
- **Sub-agents:** the skill tells Codex not to explore with `spawn_agent` — sub-agents bill your
  plan; scouts are the cheap way to explore.
- **Visual review:** Codex's in-app browser if it has one, `multiagents shot --url` /
  `--ios` otherwise.
- **Keys in env:** by default Codex does **not** strip `*_API_KEY` variables from subprocess env
  (`shell_environment_policy.ignore_default_excludes` defaults to true). multiagents strips
  them from *worker* processes itself, but if you keep provider keys in your shell profile,
  consider Codex's `shell_environment_policy` to hide them from other commands too.
- **Codex's config:** `install-codex` changes `~/.codex/config.toml` only through
  `codex plugin …` (a `[marketplaces.multiagents]` source and
  `[plugins."multiagents@multiagents"]`). `codex plugin remove multiagents@multiagents` and
  `codex plugin marketplace remove multiagents` undo it.
