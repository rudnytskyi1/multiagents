# How multiagents works

## The idea

Interactive frontier-model sessions are expensive; most engineering tokens are spent on
mechanical work (reading files, writing code, running tests, applying review feedback). multiagents
splits the two:

- The **lead** is your normal interactive session (Claude Code or Codex). It plans, writes task
  specs, reviews diffs and the running UI, and decides. It is told not to write production code.
- **Workers** are headless Claude Code processes (`claude -p`) whose API traffic goes to a cheap
  provider (DeepSeek on Fireworks, or api.deepseek.com). They implement, verify, review with your
  own agents, and fix.

The lead never shares a context window with workers. All coordination happens through **files in
the repo** — specs, reports, feedback — which keeps every hand-off explicit and auditable.

## Components

```
skills/lead/SKILL.md        the lead protocol (loaded by /multiagents:lead in Claude Code)
codex/skills/…/SKILL.md     the same protocol for Codex ($multiagents-lead)
bin/multiagents             sh wrapper (python3 >= 3.9 guard)
worker/multiagents.py       the whole CLI: task store, git, worker spawning, isolation
worker/prompts/*.md         system prompts for the roles (common + coder/reviewer/fixer/scout)
worker/templates/*.md       task.md and feedback-N.md skeletons
```

## Task lifecycle

Every task lives in `.multiagents/tasks/T00N-<slug>/` inside the target repo (the folder is added
to `.git/info/exclude`, so it never pollutes the repo's history):

```
task.md                the spec the lead writes — the worker's only source of truth
task.json              bookkeeping: id, title, status, base commit/branch, rounds[]
NN-<role>-report.md    the worker's report for round NN
NN-<role>.log          human-readable timeline (tool calls, errors, retries)
NN-<role>.jsonl        raw stream-json transcript (for deep debugging)
feedback-N.md          the lead's review feedback for fix round N
shots/*.png            screenshots attached to feedback (fixer models can see them)
```

States: `draft → in_progress/in_review/fixing/scouting → implemented/reviewed/fixed/scouted →
accepted | rejected`, with `error` whenever a round ends in anything but success and
`interrupted` stamped on rounds cut short by a signal.

### Git model

- The first `run` on a task records `base_commit`/`base_branch` and creates `ma/T00N-<slug>`
  from the current branch. It requires a clean tree (`--allow-dirty` overrides) and refuses a
  detached HEAD.
- Workers commit as `[T00N] <role>: <summary>` — no Claude attribution trailer, since the code
  is written by the worker model.
- `multiagents diff T00N` always compares against `base_commit`, from any branch.
- `multiagents accept` merges the task branch back into `base_branch` (`--no-ff`, or `--squash`);
  `reject` returns to `base_branch` and keeps the task branch for reference.
- One repo, one worker at a time: `.multiagents/worker.lock` (atomic create, stale-tolerant, and
  respected by `accept`/`reject` so nothing switches branches under a live worker).
- Scouts are the exception: read-only, no branch, no clean-tree requirement.

## Roles

| Role | Purpose | Writes code? | Extra restrictions |
|---|---|---|---|
| `coder` | implement the spec | yes | — |
| `reviewer` | independently verify: build, tests, run the repo's review agents, fix findings | yes (fixes) | — |
| `fixer` | address the lead's `feedback-N.md` | yes | resumes the coder's session by default |
| `scout` | answer questions about the codebase before planning | no | Edit/Write/NotebookEdit disabled, git writes denied, report captured from its final message |

### How a worker is assembled

For each `multiagents run <role> T00N` the CLI builds:

1. **System prompt** = `worker/prompts/common.md` + `worker/prompts/<role>.md`, with
   placeholders filled in (`{{task_id}}`, `{{branch}}`, `{{report_path}}`, role-aware
   `{{git_rules}}` / `{{report_rules}}`, …).
2. **User message** = the task spec, plus role-specific context: the implementation report for
   the reviewer; the spec + previous reports + the lead's feedback + screenshot paths for the
   fixer.
3. **Settings** (`--settings` JSON): `apiKeyHelper` (how the worker's own Claude Code process
   obtains the provider key), the permission allow/deny lists, and no attribution.
4. **Environment**: see [security.md](security.md) — stripped of every credential, pointed at
   the provider `base_url`, with all model-selection variables pinned to the role's model.

The fixer passes `--resume <coder session id>` when the model matches, so it keeps the coder's
full working context. Claude Code snapshots a conversation's system prompt at the first turn, so
a resumed fixer would replay the *coder's* rules — that is why the fixer's role instructions are
injected into the user message on resume.

### Reports

Workers end by writing `NN-<role>-report.md` (scouts: their final message is captured to it).
The first `Status:`/`Verdict:` line becomes the round's `worker_status` (`DONE`, `PARTIAL`,
`BLOCKED`, `PASS`, `PASS_WITH_NOTES`, `FAIL`) — the signal the lead branches on. If a worker
produced no report file, its final message is saved with an "auto-captured" note so nothing is
lost.

## The run loop (what the CLI does while a worker runs)

`_run_worker` spawns `claude -p --output-format stream-json` in its own process group and:

- streams events, writing the raw transcript and a readable log (`[Bash] npm test`,
  `[say] …`, `[error] …`, `[retry] …`);
- watches two clocks: a total `timeout_minutes` (default 60) and an idle limit that is always at
  least the worker's max Bash timeout + 5 min, because a worker is legitimately silent during
  one long build;
- on SIGINT/SIGTERM/SIGHUP kills the worker's process group and stamps the round
  `interrupted` before exiting;
- collects permission denials, token usage per model, and an **estimated cost** from your
  `prices` config (never from Claude Code's own cost fields, which assume Anthropic pricing);
- prints a summary the lead can act on: outcome, worker status, turns, duration, tokens, cost,
  new commits, diff stat vs base, uncommitted files, denied commands, tool errors, API retries —
  followed by the report itself.

Exit code 0 means the worker session ended in `success`; anything else (timeout, interrupt,
API failure, max turns) is nonzero.

## The lead protocol in one paragraph

Plan → (scout) → spec → coder → reviewer → **lead's own review** (diff + running the app) →
accept, or feedback → fixer → review again, at most 3 feedback rounds — then re-plan, fix a
small thing yourself, or ask the user. Full text: [`skills/lead/SKILL.md`](../skills/lead/SKILL.md).

## Cost model

Costs are estimated from `prices` (USD per 1M tokens: `input` = fresh input incl. cache writes,
`cached_input` = cache reads, `output`). Prices for the built-in models ship with the tool;
`multiagents status` sums per-round estimates per task. Unknown or partial price entries make
the estimate say "add model price to config" instead of guessing.
