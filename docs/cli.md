# CLI reference

General: every command must run inside a git repository except `doctor`, `provider`, `models`,
`selftest`, `install-codex` (which use the repo's config when run inside one). Errors print
`multiagents: <message>` and exit `2`; `run` exits `0` only when the worker session succeeded.
Workers refuse to run team commands (`run`, `accept`, `reject`, `new`, `feedback`, `shot`).

## `multiagents doctor [--quick] [--force]`

Checks, in order: the Claude Code binary; the active provider; the API key source; the
provider's model-listing endpoint; each configured model (`--quick` only checks it is listed,
otherwise sends a 1-message probe); the leak self-test (`--force` ignores its cache); the repo,
config files and effective worker settings. Exit 0 only if everything passed.

## `multiagents provider [name]`

Without a name: shows the active provider and the available ones. With a name: sets the
user-wide default in `~/.multiagents/config.json`.

## `multiagents models [filter] [--all]`

Lists model ids your key can use on the active provider (`filter` is a case-insensitive
substring, default `deepseek`; `--all` lists everything).

## `multiagents new <slug> [--title "..."]`

Creates `.multiagents/tasks/T00N-<slug>/` (next free number) with a `task.md` template and an
empty `shots/`. Fill the spec before running a worker.

## `multiagents run <role> <task> [options]`

Runs one worker to completion (blocking; minutes). Roles: `coder`, `reviewer`, `fixer`, `scout`.

| Option | Meaning |
|---|---|
| `--feedback <file>` | required for `fixer`: the lead's feedback file (resolved against the task dir, then cwd) |
| `--fresh` | fixer: start a new session instead of resuming the coder's |
| `--model <alias-or-id>` | override the role's model for this run |
| `--max-turns N` | override the role's turn cap |
| `--timeout MIN` | override `timeout_minutes` |
| `--permission-mode <mode>` | `acceptEdits` (default), `bypassPermissions`, `dontAsk`, `manual` |
| `--allow-dirty` | first run of a task: tolerate uncommitted changes in the tree |

The first run of a task snapshots `base_commit`/`base_branch` and creates the task's own
**git worktree** under `.multiagents/worktrees/` on branch `ma/T00N-…` (a named branch is
required; the main tree may stay dirty — the worker starts from the last commit). Independent
tasks can therefore run **in parallel**: one worker per task, any number of tasks. Scouts skip
all of this and run read-only in the main tree. NOTE: a worktree is a clean checkout — nothing
git-ignored (node_modules, .venv, Pods, .env) is in it; put bootstrap commands into the spec's
Verification, or share paths via `"worktree_link"` in config. The summary printed at the end:
outcome (`success` / `timeout` / `idle timeout` / `interrupted` / `error during execution` /
`exit N`), the report's own `Status`/`Verdict`, turns, duration, token counts, estimated cost,
new commits and diff stat vs base, uncommitted files, denied commands, tool errors, API
retries — then the report **digest** (verdict + decision-relevant sections; scouts and failed
rounds print the full report; the full text always stays on disk). Task id may be given as
`T001`, `t1` or `1`.

## `multiagents feedback <task>`

Creates the next `feedback-N.md` (max existing N + 1; never overwrites) from the template.

## `multiagents shot <task> <name> (--url URL | --ios) [options]`

Saves a PNG into the task's `shots/` folder and prints its path.

- `--url URL` — headless Chrome/Chromium/Edge; `--width`/`--height` (default 1280×900),
  `--dark` emulates dark mode. Scheme-less URLs get `http://`. Widths < 500 are captured via an
  iframe wrapper (headless Chrome's minimum layout width) and cropped back — pages that forbid
  framing (X-Frame-Options/CSP) come out blank below 500px; retry at ≥ 500.
- `--ios` — `xcrun simctl` screenshot of the booted simulator (`--device` to pick one).

## `multiagents digest <task> [--all]`

Zero-model review packet for the lead: for the last two rounds (or all with `--all`), the
report's verdict plus only its decision-relevant sections (problems, risks, notes, questions,
verification, review agents), then the diff stat vs base and the task's worker cost. Read this
before opening any full report.

## `multiagents sync <task>`

Merges the task's base branch into its worktree, so a long-lived parallel task absorbs what was
accepted after it started. On conflicts the worktree is left mid-merge with the conflicted
files listed — resolve there (yourself, or a fixer round told to resolve the merge), commit,
then `accept`.

## `multiagents status [task] [-v]`

Tasks with status and, per round: role, model, outcome, worker status, duration, estimated cost,
plus the per-task total. Without a task id shows the last 3 rounds each (`-v` for all). Rounds
whose process died without closing are shown as `interrupted`.

## `multiagents diff <task> [--stat] [--last] [-- <paths>…]`

`git diff` of the task against its `base_commit` (works from any branch by diffing the task
branch). `--stat` for the summary form; `--last` shows only the LAST round's commits (and
refuses if that round committed nothing) — the cheap re-review after a fixer round. Uncommitted
changes sitting in the task worktree are flagged on stderr but not shown.

## `multiagents accept <task> [--squash]`

Refuses while that task's worker is running, if the task worktree has uncommitted changes, or
if the main tree is dirty or not on `base_branch`. Merges the task branch into `base_branch`
(`--no-ff` with a `Merge T00N: <title>` message, or `--squash`), removes the task's worktree,
and marks the task `accepted`. A conflicting merge is aborted cleanly with a pointer to
`multiagents sync`.

## `multiagents reject <task>`

Removes the task's worktree (work stays on the `ma/` branch for reference) and marks the task
`rejected`. Legacy inline tasks are returned to `base_branch` first.

## `multiagents selftest`

Forces a fresh leak self-test (see [security.md](security.md)) and prints PASS/FAIL with detail.

## `multiagents install-codex [--dir DIR]`

Copies the bundled Codex skill into `~/.agents/skills/multiagents-lead` (or `DIR`), links the
CLI into `~/.local/bin`, and prints PATH guidance. See [codex.md](codex.md).
