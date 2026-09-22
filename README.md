# multiagents

**Your Claude session leads. Cheap DeepSeek workers do the typing.**

A Claude Code plugin that turns your normal session (for example Opus on a Claude Max/Pro
subscription) into a **team lead**. The lead plans the work, writes precise task specs, reviews
the code and the running UI, and sends feedback. **Workers** are headless Claude Code processes
running DeepSeek on Fireworks (US-hosted). They implement, run builds and tests, run *your own*
review agents, and fix what the lead points out.

Your subscription tokens go to thinking and verification. The bulk work is billed per token at
DeepSeek prices: V4.1 Flash costs $0.22 / $0.007 / $0.66 per 1M input / cached input / output
tokens.

```
you ──► lead (your Claude session)
          │  plan → task spec (.multiagents/tasks/T001-…/task.md)
          ▼
        coder (DeepSeek V4.1 Flash) ── implements, tests, commits on branch ma/T001-…
          ▼
        reviewer (V4.1 Flash) ──────── builds, tests, runs your .claude/agents reviewers, fixes
          ▼
        lead review ── diff + visual/UI check (simulator / browser)
          │  accept → merge          │  changes → feedback-N.md (+ screenshots)
          ▼                          ▼
        next task                  fixer (Flash, resumes the coder's session) ──► review again
```

## Requirements

- Claude Code (the desktop app's Code tab or the CLI), logged in however you like.
- `git` and `python3` (3.9 or newer, standard library only).
- A [Fireworks](https://fireworks.ai) API key with access to the DeepSeek models.

## Install

In Claude Code:

```
/plugin marketplace add <github-user>/multiagents
/plugin install multiagents@multiagents
```

From a terminal, the same thing is:

```bash
claude plugin marketplace add <github-user>/multiagents
```

```bash
claude plugin install multiagents@multiagents
```

Plugins installed with the CLI also load in the desktop app's Code tab. To install from a local
clone, pass the folder path instead of `<github-user>/multiagents`.

Store your Fireworks key. On macOS use the Keychain. The command prompts for the key, so it never
lands in shell history:

```bash
security add-generic-password -s fireworks-api -a "$USER" -w
```

On other systems, or as an alternative: `export FIREWORKS_API_KEY=…` in your shell profile, or
`~/.multiagents/fireworks.key` with `chmod 600`.

Then run `/multiagents:setup` in Claude Code, or `multiagents doctor` in its terminal. It checks
the key, each configured model, and credential isolation (see *Security*).

## Use

```
/multiagents:lead add a settings screen with a dark-mode toggle
```

The lead follows the protocol in [`skills/lead/SKILL.md`](skills/lead/SKILL.md):

1. **Plan.** Optionally send a *scout* worker to map an unfamiliar area.
2. **Spec.** Write a precise `task.md` for each task: files, requirements, out of scope,
   verification commands, review agents to run, and UI notes.
3. **Coder.** Implements the task on branch `ma/T00N-<slug>`, runs the tests, and commits.
4. **Reviewer.** Runs the build and tests plus your review agents (from `.claude/agents/` and
   `~/.claude/agents/`), and fixes what it finds.
5. **Lead review.** The lead reads the diff and checks the UI itself in the iOS Simulator or the
   browser.
6. **Feedback loop.** The lead writes `feedback-N.md` with numbered, checkable points and
   screenshots. The fixer addresses them, and the loop repeats (at most 3 rounds per task).
7. **Accept.** `multiagents accept` merges the task branch, and the lead moves on to the next
   task.

The lead writes code itself only for review instrumentation or trivial fixes, and tells you when
it does.

### CLI

The lead drives this, but you can use it too.

```
multiagents doctor [--quick]           check setup (key, models, isolation self-test)
multiagents models [filter]            models your key can use (default filter: deepseek)
multiagents new <slug> --title "..."   create .multiagents/tasks/T00N-<slug>/task.md
multiagents run <role> T00N            role: coder | reviewer | fixer | scout
        [--feedback feedback-N.md] [--model flash|<id>] [--fresh] [--timeout MIN]
multiagents feedback T00N              create the next feedback-N.md from the template
multiagents shot T00N <name> --url URL [--width W --height H] [--dark]
                                       screenshot a web page (headless Chrome) into the task's shots/
multiagents shot T00N <name> --ios     screenshot the booted iOS Simulator into shots/
multiagents status [T00N]              rounds, verdicts, time, estimated cost
multiagents diff T00N [--stat] [paths] changes vs the task's base commit
multiagents accept T00N [--squash]     merge the task branch into its base branch
multiagents reject T00N                abandon (the branch is kept)
multiagents selftest                   re-run the credential leak self-test
```

Task files live in `.multiagents/` in your repo. This folder is added to `.git/info/exclude`
automatically and is never committed. Each round leaves a report (`NN-<role>-report.md`), a
readable log (`NN-<role>.log`), and the raw transcript (`NN-<role>.jsonl`).

## Configuration

Settings are merged in this order, later winning: built-in defaults, then
`~/.multiagents/config.json`, then `<repo>/.claude/multiagents.json` (you can commit this one),
then environment variables.

```json
{
  "models": {
    "coder": "accounts/fireworks/models/deepseek-v4p1-flash",
    "reviewer": "accounts/fireworks/models/deepseek-v4p1-flash"
  },
  "prices": { "accounts/fireworks/models/<other-model>": { "input": 0.0, "cached_input": 0.0, "output": 0.0 } },
  "allow": ["Bash(./scripts/test.sh*)"],
  "deny": [],
  "permission_mode": "acceptEdits",
  "max_turns": { "coder": 150, "reviewer": 120 },
  "timeout_minutes": 60
}
```

- **Models:** override per role with `MULTIAGENTS_CODER_MODEL`, `MULTIAGENTS_REVIEWER_MODEL`,
  and so on, or per run with `--model`. Use `multiagents models` to see what your key can use.
- **Worker permissions:** workers run in `acceptEdits` mode with an allowlist of common read, git,
  build and test commands (npm/pnpm/yarn, pytest, go, cargo, swift/xcodebuild, make, gradle…). A
  deny list blocks push, reset, rebase, checkout, stash, clean, sudo and similar. Anything else is
  denied, not prompted, and listed in the run summary, so you can add it to `allow`.
- **Costs:** the summary estimates cost from `prices`. Flash is built in. Add prices for other
  models (see your Fireworks dashboard).

## Security: keeping your Claude login away from Fireworks

A worker is `claude -p` pointed at another API endpoint. Doing that naively is dangerous. While
testing inside the Claude desktop app, we saw a child `claude -p` inherit the host session's
environment and send the **subscription OAuth token** to the custom `ANTHROPIC_BASE_URL`. It did
this even with `ANTHROPIC_API_KEY` or `apiKeyHelper` set.

multiagents uses two independent layers of protection, plus a check:

1. **Clean environment.** Every `ANTHROPIC_*` and `CLAUDE*` variable inherited from the host is
   removed before a worker starts.
2. **Separate config dir.** Workers get their own `CLAUDE_CONFIG_DIR`
   (`~/.multiagents/worker-home`), so your stored login is never found. Your `agents/`, `skills/`
   and `CLAUDE.md` are symlinked in, so workers can still use them. Your MCP servers and plugins
   are not. The Fireworks key is supplied only via `apiKeyHelper`, so it is not in the worker's
   environment.
3. **Leak self-test.** Before the first real run on each Claude Code build and environment shape,
   the CLI starts a local capture server and points an isolated worker at it with a random canary
   key. It then checks the credentials of every request. If anything other than the canary is
   sent (an `sk-ant` token or the OAuth beta header), workers refuse to run. With both layers
   disabled on purpose, this test fails as it should. With either layer on, it passes.

Workers also get no MCP servers (`--strict-mcp-config`) and send no telemetry
(`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`).

**What a worker can do:** edit files in the repo, and run allowlisted commands on the task branch.
It cannot push or rewrite history. Build tools can run arbitrary code, so treat a worker like a
junior developer with a shell on a branch. For stronger isolation, run inside a VM or container.

## What a run looks like

The first end-to-end test was a small tip-calculator web app: a logic module with tests, then a
UI page. The lead was Opus, and every worker ran V4.1 Flash. The reviewer used the user's own
`spec-compliance-reviewer` agent.

| Task | Round | Model | Time | Worker cost |
|---|---|---|---|---|
| T001 logic + 28 tests | coder | V4.1 Flash | 47 s | $0.010 |
| T002 UI page + view tests | coder | V4.1 Flash | 1 m 52 s | $0.024 |
| | lead visual review (browser, light/dark, 360 px) | Opus | – | subscription |
| | fixer (3 visual points, one screenshot) | V4.1 Flash | 1 m 15 s | $0.015 |
| | reviewer + review agent | V4.1 Flash | 2 m 48 s | $0.042 |
| T004 read-only questions | scout | V4.1 Flash | 1 m 23 s | $0.016 |

Each role caught something the others missed:
- The **lead's visual review** found that the selected tip button became unreadable on hover (and
  stayed that way on touch devices).
- The **reviewer** found a crash on absurdly long amounts, fixed it, and added a test.
- The **scout** found a floating-point rounding edge case ($50.00 at 18.99% gave 949 cents, not
  950) that the spec itself had baked in.

## Limitations

- One worker per repository at a time, because workers share the working tree. Parallel tasks via
  git worktrees are a natural next step.
- Workers can't see your Claude conversation. Everything they need must be in the spec or the
  feedback. This is deliberate: it keeps specs honest.
- Every role runs DeepSeek V4.1 Flash by default. It accepts images, so workers can open the lead's
  screenshots. If you configure a text-only model for the fixer, describe visual issues in words only.
- Claude Code reports cost using Anthropic prices for unknown models. multiagents ignores that and
  uses `prices` instead.

## License

MIT
