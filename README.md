# multiagents

**Your expensive session leads. Cheap DeepSeek workers do the typing.**

Docs: [getting started](docs/getting-started.md) · [how it works](docs/how-it-works.md) ·
[configuration](docs/configuration.md) · [CLI](docs/cli.md) · [providers](docs/providers.md) ·
[security](docs/security.md) · [Codex](docs/codex.md) ·
[troubleshooting](docs/troubleshooting.md) · [по-русски](README.ru.md)

A plugin for Claude Code (with an OpenAI Codex install too) that turns your interactive session
into a **team lead**. The lead plans the work, writes precise task specs, reviews the code and
the running UI, and sends feedback. **Workers** are headless Claude Code processes running
DeepSeek models on the provider you choose per project:

| Provider | Endpoint | Where it runs | Typical use |
|---|---|---|---|
| `fireworks` (default) | api.fireworks.ai | US | work projects |
| `deepseek` | api.deepseek.com | China | personal projects |

Your subscription tokens go to thinking and verification. The bulk work is billed per token at
DeepSeek prices — e.g. V4.1 Flash: $0.22 / $0.007 / $0.66 per 1M input / cached / output tokens
on Fireworks, or $0.30 / $0.006 / $1.20 peak (half off-peak) on api.deepseek.com.

```
you ──► lead (your Claude Code / Codex session)
          │  plan → task spec (.multiagents/tasks/T001-…/task.md)
          ▼
        coder (DeepSeek) ──── implements, tests, commits on branch ma/T001-…
          ▼
        reviewer (DeepSeek) ─ builds, tests, runs your .claude/agents reviewers, fixes
          ▼
        lead review ── diff + visual/UI check (simulator / browser)
          │  accept → merge          │  changes → feedback-N.md (+ screenshots)
          ▼                          ▼
        next task                  fixer (resumes the coder's session) ──► review again
```

## Requirements

- Claude Code (desktop app or CLI) — it is both the lead's home and the workers' engine.
- `git` and `python3` 3.9+ (standard library only).
- An API key for [Fireworks](https://fireworks.ai) and/or
  [DeepSeek](https://platform.deepseek.com).

## Install

### Claude Code

```
/plugin marketplace add <github-user>/multiagents
/plugin install multiagents@multiagents
```

CLI equivalents: `claude plugin marketplace add <github-user>/multiagents` then
`claude plugin install multiagents@multiagents`. For a local clone, pass the folder path
instead. Plugins installed with the CLI also load in the desktop app's Code tab.

### Codex (optional)

```bash
multiagents install-codex
```

Run it from a Claude Code session with the plugin enabled, or directly from a clone as
`<clone>/bin/multiagents install-codex`. It installs the `$multiagents-lead` skill into
`~/.agents/skills` and links the CLI into `~/.local/bin` (make sure that is on your PATH).
Start a new Codex session and invoke `$multiagents-lead`. Workers still run on the Claude Code
engine, so keep the `claude` CLI installed; Codex may ask you to approve network access when a
worker runs — that is the worker talking to the model API.

### API keys

Store each provider's key (the command prompts for it, so it never lands in shell history):

```bash
security add-generic-password -s fireworks-api -a "$USER" -w
```

```bash
security add-generic-password -s deepseek-api -a "$USER" -w
```

Elsewhere: `export FIREWORKS_API_KEY=…` / `DEEPSEEK_API_KEY=…`, or
`~/.multiagents/<provider>.key` with `chmod 600`. (A key found in the environment is staged into
a 0600 file so workers never see it in their env.) Then run `/multiagents:setup` in Claude Code,
or `multiagents doctor` in a terminal.

## Choosing a provider

- Per project (committable): `.claude/multiagents.json` → `{"provider": "deepseek"}`
- User default: `multiagents provider deepseek`
- One run: `MULTIAGENTS_PROVIDER=deepseek multiagents …`

With `deepseek`, your prompts and code go to servers in China under DeepSeek's terms — pick it
per project deliberately. Model notes: on api.deepseek.com use `deepseek-flash` (V4.1-Flash:
vision, 1M context; the default for every role) or the `pro` alias (`deepseek-v4-pro`: stronger,
no vision). The legacy `deepseek-chat` / `deepseek-reasoner` ids were discontinued in July 2026.

## Use

```
/multiagents:lead add a settings screen with a dark-mode toggle
```

The lead follows [`skills/lead/SKILL.md`](skills/lead/SKILL.md): plan (optionally send a
read-only *scout* first), write a precise `task.md` per task, dispatch the coder, then the
reviewer (which runs your own review agents from `.claude/agents/`), review the diff and the
running UI itself, send numbered feedback with screenshots to the fixer, and only then
`accept` — at most 3 feedback rounds per task. It writes code itself only for review
instrumentation or trivial fixes, and says so.

### CLI

```
multiagents doctor [--quick]           setup check: key, provider, models, isolation self-test
multiagents provider [name]            show or set the active provider
multiagents models [filter]            models your key can use
multiagents new <slug> --title "..."   create .multiagents/tasks/T00N-<slug>/task.md
multiagents run <role> T00N            role: coder | reviewer | fixer | scout
        [--feedback feedback-N.md] [--model flash|<id>] [--fresh] [--timeout MIN]
multiagents feedback T00N              create the next feedback-N.md
multiagents shot T00N <name> --url URL [--width W --height H] [--dark]
                                       screenshot a page (headless Chrome) into the task's shots/
multiagents shot T00N <name> --ios     screenshot the booted iOS Simulator into shots/
multiagents status [T00N]              rounds, verdicts, time, estimated cost
multiagents diff T00N [--stat] [-- p]  changes vs the task's base commit
multiagents accept T00N [--squash]     merge the task branch into its base branch
multiagents reject T00N                abandon (the branch is kept)
multiagents selftest                   re-run the credential leak self-test
multiagents install-codex              install the lead skill into OpenAI Codex
```

Task files live in `.multiagents/` in your repo (auto-added to `.git/info/exclude`). Each round
leaves a report (`NN-<role>-report.md`), a readable log (`NN-<role>.log`) and the raw transcript
(`NN-<role>.jsonl`). Scouts are enforced read-only: no editing tools, git writes denied, report
captured from their final message.

## Configuration

Merge order (later wins): built-in defaults → the selected provider's block →
`~/.multiagents/config.json` → `<repo>/.claude/multiagents.json` → env vars.

```json
{
  "provider": "deepseek",
  "models": { "coder": "deepseek-flash", "reviewer": "deepseek-flash" },
  "prices": { "<model id>": { "input": 0.3, "cached_input": 0.006, "output": 1.2 } },
  "allow": ["Bash(./scripts/test.sh*)"],
  "deny": [],
  "permission_mode": "acceptEdits",
  "max_turns": { "coder": 150 },
  "timeout_minutes": 60
}
```

- **Trust boundary:** a repo's `.claude/multiagents.json` is untrusted input, so only these keys
  are honored from it: `provider`, `models`, `aliases`, `prices`, `allow`, `deny`,
  `permission_mode` (not bypassPermissions), `max_turns`, `timeout_minutes`,
  `idle_timeout_minutes`, `use_branches`, `branch_prefix`. Endpoints, key sources, the claude
  binary, worker home and worker env can only be set in `~/.multiagents/config.json` — a cloned
  repo must not be able to redirect your keys or run its own binary.
- **Models:** per role via config, `MULTIAGENTS_<ROLE>_MODEL`, or `--model` per run.
  `multiagents models` lists what your key can use.
- **Worker permissions:** workers run in `acceptEdits` with an allowlist of common read, git,
  build and test commands; denied commands are listed in the run summary so you can extend
  `allow`. `curl` is allowed only against localhost.
- **Custom providers:** add blocks under `"providers"` in `~/.multiagents/config.json` (fields:
  `base_url`, `models_url`, `keychain_service`, `key_file`, `key_env`, `models`, `aliases`,
  `prices`); any Anthropic-compatible endpoint works.

## Security

A worker is `claude -p` pointed at another API endpoint. Done naively, that is dangerous: inside
the Claude desktop app we observed a child `claude -p` inherit the host session's environment and
send the **subscription OAuth token** to the custom `ANTHROPIC_BASE_URL`, even with
`ANTHROPIC_API_KEY` set. multiagents layers these defenses:

1. **Clean environment.** Every inherited `ANTHROPIC_*` / `CLAUDE*` variable is stripped, and so
   is every provider's key variable (the key travels only via `apiKeyHelper`).
2. **Separate config dir.** Workers get their own `CLAUDE_CONFIG_DIR`
   (`~/.multiagents/worker-home`) — your stored login is never found. Your `agents/`, `skills/`
   and `CLAUDE.md` are symlinked in; your MCP servers and plugins are not.
3. **Leak self-test.** Before the first real run per Claude Code build *and* per credential
   surface (env values, endpoints, key plumbing, login-store state — all hashed into the cache
   key), a worker is pointed at a local capture server with a canary key; if an Anthropic
   credential pattern shows up in any header or the body, or the canary is missing, workers
   refuse to run.
4. **Untrusted project config** (see the trust boundary above), a per-repo worker lock,
   read-denies on credential paths (`~/.claude`, `~/.ssh`, `~/.aws`, …), and no MCP servers or
   telemetry in workers.

**Honest limits:** the deny list blocks the plain spellings of `git push`, history rewrites and
direct credential reads (the Read tool and path-first readers like `cat`/`head`), and workers
are instructed accordingly — but pattern-first tools (`grep`, `jq`) and allowlisted interpreters
(`node`, `python3`) can reach arbitrary paths and run arbitrary code by design. Treat a worker
like a junior developer with a shell on a branch: the lead's review before `accept` is the real
boundary, and for hard isolation run inside a VM or container.

## What a run looks like

From the first end-to-end test (a small web app; lead = Opus, workers = V4.1 Flash on
Fireworks, review agent = the user's own `spec-compliance-reviewer`):

| Task | Round | Time | Worker cost |
|---|---|---|---|
| T001 logic + 28 tests | coder | 47 s | $0.010 |
| T002 UI page | coder | 1 m 52 s | $0.024 |
| | lead visual review (browser, dark mode, 360 px) | – | subscription |
| | fixer (3 visual points, one screenshot) | 1 m 15 s | $0.015 |
| | reviewer + review agent | 2 m 48 s | $0.042 |
| T004 read-only questions | scout | 1 m 23 s | $0.016 |

Each role caught something the others missed: the lead's visual pass found an unreadable
hover state, the reviewer found and fixed a crash on absurd input, the scout found a rounding
edge case baked into the spec itself.

## Limitations

- One worker per repository at a time (workers share the working tree; the lock enforces it).
- Workers can't see your conversation — everything must be in the spec or feedback. This is
  deliberate: it keeps specs honest.
- Start tasks from a named branch (detached HEAD is refused so accept/reject know where to
  merge).
- macOS and Linux; Windows is untested. `shot --ios` needs macOS + Xcode tools.
- Claude Code reports costs at Anthropic prices for unknown models; multiagents ignores that and
  uses your `prices` config.

## License

MIT
