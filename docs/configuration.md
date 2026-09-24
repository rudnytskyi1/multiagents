# Configuration reference

## Files and precedence

Later sources win:

1. Built-in defaults (`DEFAULT_CONFIG` in `worker/multiagents.py`)
2. The selected **provider block** (`providers.<name>`)
3. `~/.multiagents/config.json` — user config (trusted)
4. `<repo>/.claude/multiagents.json` — project config (**untrusted**, filtered)
5. Environment variables

`MULTIAGENTS_HOME` relocates the state directory (default `~/.multiagents`), which holds
`config.json`, key files, the leak-self-test cache, and the workers' Claude config dir.

## Provider selection

Priority: `MULTIAGENTS_PROVIDER` env → project `"provider"` → user `"provider"` → `fireworks`.

```bash
multiagents provider            # show active + available
multiagents provider deepseek   # set the user-wide default
```

## Keys accepted from a project config

A repo's `.claude/multiagents.json` may come from anyone, so only these keys are honored from
it (everything else is ignored with a warning):

`provider`, `models`, `aliases`, `prices`, `allow`, `deny`, `permission_mode` (anything except
`bypassPermissions`), `max_turns`, `timeout_minutes`, `idle_timeout_minutes`, `use_branches`,
`worktrees`, `branch_prefix`, `worktree_link` (from a project config, only paths that stay inside
the repo after resolving symlinks are linked).

Endpoint and key plumbing (`base_url`, `models_url`, `keychain_service`, `key_file`, `key_env`,
`api`, `max_output_tokens`, `extra_body`),
`claude_bin`, `worker_home`, `worker_env` and `providers` definitions are **user-config only**.
Inside the user config, endpoint/key plumbing is only honored inside `providers.<name>` — a
top-level `base_url` would silently pair one vendor's key with another vendor's endpoint when
the provider switches, so top-level copies are ignored with a warning.

## All settings

| Key | Default | Scope | Meaning |
|---|---|---|---|
| `provider` | `"fireworks"` | user, project, env | active provider name |
| `providers.<name>.label` | — | user | display name shown by `doctor`/`provider` |
| `providers.<name>.api` | `"anthropic"` (`"openai"` on hive) | user | which API the endpoint speaks; `"openai"` routes workers through the local bridge ([providers.md](providers.md#the-bridge)) |
| `providers.<name>.base_url` | per provider | user | Anthropic API: the endpoint without `/v1`; OpenAI API: the URL `/chat/completions` hangs off |
| `providers.<name>.models_url` | `<base_url>/v1/models` (OpenAI API: `<base_url>/models`) | user | model-listing endpoint (Bearer auth); `""` = none |
| `providers.<name>.keychain_service` | `fireworks-api` / `deepseek-api` / `hive-api` | user | macOS Keychain service name |
| `providers.<name>.key_file` | `~/.multiagents/<name>.key` | user | fallback key file (chmod 600) |
| `providers.<name>.key_env` | `FIREWORKS_API_KEY` / `DEEPSEEK_API_KEY` / `HIVE_API_KEY` | user | env var checked first (staged to a 0600 file; never passed to workers) |
| `providers.<name>.max_output_tokens` | none | user | OpenAI API only: cap on the `max_tokens` Claude Code asks for |
| `providers.<name>.extra_body` | `{}` | user | OpenAI API only: fields merged into every chat-completions request |
| `providers.<name>.models` | flash for every role | user | role → model id (`coder`, `reviewer`, `fixer`, `scout`, `background`, optional `fallback`) |
| `providers.<name>.aliases` | `flash` (+ `pro` on deepseek) | user | short names usable anywhere a model id is |
| `providers.<name>.prices` | built-ins prefilled | user | `{model: {input, cached_input, output}}` USD per 1M tokens |
| `models` / `aliases` / `prices` | — | user, project | cross-provider overrides of the same fields |
| `permission_mode` | `acceptEdits` | user, project | worker permission mode |
| `allow` / `deny` | `[]` | user, project | extra permission rules appended to the built-in lists |
| `max_turns` | coder 150, fixer 100, reviewer 120, scout 60 | user, project | per-role agentic turn cap |
| `timeout_minutes` | 60 | user, project | hard wall-clock cap per run |
| `idle_timeout_minutes` | 15 | user, project | silence cap; effective value is always ≥ max Bash timeout + 5 min |
| `use_branches` | `true` | user, project | create `ma/T00N-<slug>` per task |
| `worktrees` | `true` | user, project | give each task its own git worktree (parallel tasks; the main tree is never switched). Off = legacy single-worker checkout mode |
| `worktree_link` | `[]` | user, project | git-ignored paths linked from the main tree into each new worktree (e.g. `["node_modules", ".env"]`) so workers build without re-bootstrapping; linked paths are SHARED between parallel tasks. Symlinks (junctions / hard links on Windows without symlink rights), never copies; links git would not ignore by itself are added to `.git/info/exclude` as exact paths — shared by all worktrees, so `git status` in the main tree stops showing those paths too. Paths with `..` or `.git` are refused; a path that cannot be linked is reported and skipped. From a project config, only paths that resolve inside the repo |
| `branch_prefix` | `"ma/"` | user, project | task branch prefix |
| `worker_env` | `{}` | user | extra env vars for workers |
| `worker_home` | `~/.multiagents/worker-home` | user | the workers' `CLAUDE_CONFIG_DIR` |
| `claude_bin` | auto | user | Claude Code executable override |

## Environment variables

| Variable | Meaning |
|---|---|
| `MULTIAGENTS_PROVIDER` | provider for this invocation |
| `MULTIAGENTS_CODER_MODEL` (also `…_REVIEWER_`, `…_FIXER_`, `…_SCOUT_`) | model override per role |
| `MULTIAGENTS_CLAUDE_BIN` | Claude Code executable override (highest priority) |
| `MULTIAGENTS_HOME` | state directory (default `~/.multiagents`) |
| `FIREWORKS_API_KEY` / `DEEPSEEK_API_KEY` / `HIVE_API_KEY` / custom `key_env` | API key source (checked before Keychain and key file) |
| `MULTIAGENTS_BRIDGE_DUMP` | debugging: a folder where the OpenAI bridge saves every translated request (conversation content, no key) |
| `MULTIAGENTS_ALLOW_SANDBOX` | disable the Codex-sandbox check ([codex.md](codex.md)) |

## How the worker finds Claude Code

First match wins: `MULTIAGENTS_CLAUDE_BIN` → `claude_bin` in user config →
`CLAUDE_CODE_EXECPATH` (set inside Claude Code sessions; points at the desktop app's bundled
binary) → `claude` on PATH.

## Worker permissions (defaults)

Allowed without prompting (`acceptEdits` mode; prefix rules — see the file for the exact list):
file inspection (`ls`, `cat`, `grep`, `rg`, …), `mkdir`/`touch`, `curl` **against localhost
only**, safe git (`status`/`diff`/`log`/`show`/`add`/`commit`/`restore`/…), and the common
build/test toolchains: npm/pnpm/yarn/bun, node, python/pytest/ruff/mypy/uv/poetry, go, cargo,
swift/xcodebuild/xcrun, make/cmake/gradle/mvn, dotnet, bundle/rspec/rake.

Denied: `git push` / `reset` / `rebase` / `checkout` / `switch` / `merge` / `stash` / `clean` /
`config` / `commit --amend` and branch deletion, `sudo`, `rm -rf /`-style, `multiagents` itself,
`printenv`/`env`/`ps`, `/proc` reads, WebSearch, and reads of credential paths (`~/.claude`
credentials/settings, `~/.claude.json`, `~/.codex/auth.json` / `.credentials.json` /
`config.toml` — also under `$CODEX_HOME` — `~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.netrc`,
`~/.gnupg`, key and bridge-token files under `~/.multiagents`) via the Read tool and path-first
shell readers.
Scouts additionally lose `git add/commit/rm/mv/restore`, `mkdir`, `touch` and all editing tools.

A denied command does not stop the run — the worker is told to report it, the run summary lists
it, and the lead can add it to `allow`.

## The workers' Claude config dir

`~/.multiagents/worker-home` is created on first use. Your `~/.claude/agents`,
`~/.claude/skills` and `~/.claude/CLAUDE.md` are symlinked into it (if they exist), so workers
can use your agents and skills; your login, MCP servers and plugins stay out. Project-level
`.claude/agents` load naturally from the repo. Delete the directory to reset it.
