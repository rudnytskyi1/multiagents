# Security model

## Threat model

multiagents deliberately runs `claude -p` against a **non-Anthropic endpoint**. Three things must
never happen:

1. Your Claude **subscription/OAuth credentials** reach the third-party endpoint.
2. Your **provider API keys** (or any other secret on the machine) reach the worker model —
   a cheap third-party model executing shell commands on repo content it does not control.
3. A **cloned repository** influences where your keys are sent or what binaries run.

The threat that started it all: while building this tool we observed a child `claude -p` inside
the Claude desktop app inherit the host session's environment and send the **subscription OAuth
token** to a custom `ANTHROPIC_BASE_URL` — even when `ANTHROPIC_API_KEY` or `apiKeyHelper` was
set. Never point Claude Code at a third-party endpoint by just exporting `ANTHROPIC_BASE_URL`.

## Layer 1 — clean environment

`worker_env()` builds the worker's environment from scratch semantics:

- every inherited `ANTHROPIC_*` and `CLAUDE*` variable is dropped (the desktop app transports
  auth and session context through them);
- every provider's `key_env` is dropped — including custom providers — plus anything matching
  `*_API_KEY` / `*_AUTH_TOKEN`;
- a key found in the environment is first **staged into a 0600 file** and served to the worker
  via `apiKeyHelper`, so no worker process ever holds a key in env;
- what remains is PATH/HOME/locale plus the variables the CLI sets itself (base URL, model
  pins, telemetry off, Bash timeout caps).

## Layer 2 — separate Claude config dir

Workers run with `CLAUDE_CONFIG_DIR=~/.multiagents/worker-home`. Claude Code therefore finds no
stored login, no user MCP servers, no plugins. The user's `agents/`, `skills/` and `CLAUDE.md`
are symlinked in on purpose (workers are supposed to use your review agents). `--strict-mcp-config`
removes MCP entirely; telemetry and auto-updates are disabled.

## Layer 3 — the leak self-test

Before the first real run, the CLI starts a local HTTP capture server, points a fully-isolated
worker at it with a **random canary key**, and inspects every request:

- fail if an Anthropic credential pattern (`sk-ant`, `oat01`, `oauth_token`) appears in **any
  header or the request body**;
- fail if the OAuth beta header is present;
- fail if the canary key itself is missing (the worker authenticated with something else);
- fail if no request arrived at all.

A pass is cached per **credential surface**: the hash covers the Claude binary and version, the
worker env's variable names plus the values of credential-relevant ones, the endpoint/key
plumbing (`base_url`, keychain service, key file, key env, worker home, worker env overrides),
and the existence/mtime of the host's login stores — so logging in, switching providers,
changing endpoints or updating Claude Code all force a re-probe. The cache is pruned to the 24
newest entries. `multiagents selftest` re-runs it on demand; a FAIL refuses all worker runs.

The self-test is a **canary, not a proof**: it exercises the exact process shape real runs use,
against a local server, and catches regressions in Claude Code's credential handling. It cannot
prove the absence of every conceivable leak — that is why layers 1 and 2 exist independently.

## Layer 4 — untrusted project config

`.claude/multiagents.json` inside a repo is treated as attacker-controlled (you clone things).
Only operationally-safe keys are honored from it (see
[configuration.md](configuration.md#keys-accepted-from-a-project-config)); endpoints, key
sources, binaries and worker env can only come from `~/.multiagents/config.json`. Ignored keys
produce a loud warning. `bypassPermissions` cannot be set from a project file. Within the user
config, endpoint/key plumbing is only accepted inside `providers.<name>`, so switching providers
can never mix vendor A's key with vendor B's endpoint.

## Layer 5 — the OpenAI bridge keeps the key out of the worker

For providers that only speak the OpenAI API (`"api": "openai"`, e.g. `hive`), the worker never
receives the provider key at all, not even through `apiKeyHelper`:

- the CLI reads the key and hands it to a translation bridge running **inside the CLI process**,
  listening on `127.0.0.1` only, for the length of one run;
- the worker's `ANTHROPIC_BASE_URL` points at the bridge and its `apiKeyHelper` prints a
  **random one-run token** (from a 0600 file under `~/.multiagents/run/`, deleted when the run
  ends; workers are denied reading that folder anyway);
- the bridge checks the token (constant-time compare) and builds every upstream request from
  scratch: no header the worker sends is forwarded, so no stray credential can ride along;
- the bridge log records status, timing, token counts and the provider's error messages —
  not the conversation, never the key: if a provider quotes the key in an error message, the
  bridge blanks it out before logging the message or passing it on to the worker.

The leak self-test still runs as usual: it tests what Claude Code sends, which does not change
with the bridge.

## Worker permission model

Workers run in `acceptEdits` with an allow/deny list (details in
[configuration.md](configuration.md#worker-permissions-defaults)). Highlights:

- network egress by shell is limited to `curl` against localhost (WebSearch denied; WebFetch not
  granted); interpreters can of course open sockets — see limits below;
- `printenv`/`env`/`ps`/`/proc` are denied, so the *casual* paths to other processes' secrets
  are closed;
- the Read tool and path-first readers are denied on credential paths (`~/.claude`
  credentials/settings, `~/.codex` auth/config, `~/.ssh`, `~/.aws`, key files, bridge tokens, …);
- git is restricted to branch-local, non-history-rewriting operations by rule, and workers are
  instructed accordingly;
- everything else is denied-by-default in headless mode and surfaced to the lead.

## Honest limits — read this

- Permission rules are **prefix matches**. Pattern-first tools (`grep`, `jq`, `sed`) and
  allowlisted interpreters (`python3`, `node`) and build tools can reach arbitrary paths and run
  arbitrary code, including network access. This is inherent to letting workers build and test
  real projects.
- Therefore: treat a worker like a **junior developer with a shell on a branch**. The lead's
  review before `accept` is the real security boundary for code entering your repo, and the
  layers above are about keeping *credentials* out of reach, not about containing a determined
  malicious model.
- For hard isolation, run the whole thing inside a VM or container. That composes cleanly:
  nothing in multiagents assumes it owns the machine.
- Data residency: with the `deepseek` provider your prompts, code excerpts and worker traffic go
  to servers in China under DeepSeek's terms; with `fireworks` or `hive`, to that company's US
  infrastructure under its terms. Choose per project.

## Key handling summary

| Where a key may live | How the worker gets it | Notes |
|---|---|---|
| `FIREWORKS_API_KEY` / `DEEPSEEK_API_KEY` / `HIVE_API_KEY` env | staged to `~/.multiagents/<provider>.env.key` (0600); helper `cat`s that | the variable itself never reaches workers |
| macOS Keychain (`fireworks-api` / `deepseek-api` / `hive-api`) | `apiKeyHelper` runs `security find-generic-password -w` | recommended on macOS |
| `~/.multiagents/<provider>.key` (0600) | `apiKeyHelper` runs `cat` | Linux / Windows default |
| any of the above, `"api": "openai"` provider | not at all: the CLI reads it for the bridge; the worker gets a one-run bridge token | see layer 5 |

The first source found wins, in the order of the table. Where the CLI needs the key itself
(`doctor`, `models`, the bridge) it reads it in-process, without a shell — so nothing in the
current folder (the repo) can stand in for `cat` — and it refuses a key containing spaces, line
breaks or non-ASCII characters, without printing it.

Keys are never printed, never passed as argv, never placed in worker env, and the lead skill
forbids echoing them. The `doctor`/`models` commands hold a key in memory only long enough to
call the provider's API directly; `run` holds it in memory for the run only with a bridged
provider.
