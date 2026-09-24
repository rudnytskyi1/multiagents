# Troubleshooting

Start with `multiagents doctor` — most problems show up as a FAIL line there. Below, symptom →
cause → fix.

## Setup

**`api key FAIL — not found`**
No key for the *active* provider (each has its own). Store it per
[getting-started.md](getting-started.md#2-store-an-api-key), or check which provider is active
with `multiagents provider`.

**`endpoint FAIL` / `model FAIL`**
Wrong key, no access to the model, or the provider is down. `multiagents models --all` shows
what the key can actually use; put a valid id into `models` ([configuration.md](configuration.md)).
On DeepSeek remember `deepseek-chat`/`deepseek-reasoner` no longer exist.

**`isolation FAIL`**
The leak self-test saw an Anthropic credential (or not the canary) in the probe request. Do not
run workers. Re-try `multiagents selftest` after updating Claude Code; if it keeps failing,
file an issue with the printed detail — this is the tool doing its job.

**`multiagents: python3 >= 3.9 is required`**
Install a newer python3 (the wrapper checks before parsing the CLI).

**`claude` not found**
Install Claude Code's CLI, or set `MULTIAGENTS_CLAUDE_BIN=/path/to/claude` (inside Claude Code
sessions the bundled binary is found automatically).

## Running

**`a worker is already running on T00N`**
One worker per task. Wait, or if the pid is truly dead and the message persists, delete
`.multiagents/tasks/T00N-*/worker.lock` (a garbage/empty lock is treated as stale
automatically). Different tasks run in parallel — this lock is per task.

**`uncommitted changes` complaints**
`accept` needs the MAIN tree clean (it merges there) and refuses if the task's worktree has
uncommitted work. Starting a task never requires a clean tree — but the worker starts from the
last commit, so commit anything the task must build on.

**`HEAD is detached … check out a named branch`**
`accept`/`reject` need to know where to merge back. `git switch <branch>` first.

**Run summary shows `denied (add to "allow" …)`**
The worker needed a command outside the allowlist. If it is safe and project-specific, add it
to `"allow"` in `.claude/multiagents.json` and re-run the role.

**`outcome: timeout` / `idle timeout`**
The run hit `timeout_minutes`, or was silent longer than the idle cap (≥ max Bash timeout + 5
min — a single build cannot trip it, but a hung tool can). Raise `timeout_minutes` for slow
suites, or split the task.

**`outcome: error during execution` with `api_error` in the log**
Provider-side failures; the log's `[retry]` lines show attempts. Off-peak DeepSeek and busy
Fireworks hours can be slow; re-running the role resumes nothing but costs little.

**`merging ma/T00N into <base> conflicts`** on accept
Another accepted task changed the same lines. Run `multiagents sync T00N` (merges the base into
the task's worktree), resolve there — yourself, or a fixer round whose feedback says to resolve
the merge — commit, and accept again. The main tree is never left conflicted.

**`worktree missing`** / worktree folder deleted by hand
The next `run` on that task re-creates it from the task branch (`git worktree prune` +
re-add happen automatically). Stray registrations clean up with `git worktree prune`.

**`the main tree is on '<branch>'`** on accept
`accept` merges into the task's recorded base branch; `git switch <base>` and re-run.

**Round shows `interrupted`**
The CLI was killed (Ctrl-C, app quit). Nothing is corrupted: the branch keeps whatever was
committed; run the role again.

**Fixer didn't resume the coder's session**
Resume only happens when the previous coder/fixer round used the *same model* and recorded a
session id. `--fresh` forces a new session on purpose.

**Worker "fixed" something outside the task / touched too much**
That is what `multiagents diff` and the lead review are for: `reject` the task, tighten the
spec's "Out of scope", and re-run. Branches keep main safe.

## Hive and other OpenAI-API providers

**`model FAIL … HTTP 405 (Hive answers 405 when the organization is paused …)`**
Hive pauses an organization with no credit left. Top up the balance at thehive.ai, then re-run
`multiagents doctor`.

**`model FAIL … HTTP 401`** — the key is wrong or not a V3 "Secret Key". Replace it:
`security delete-generic-password -s hive-api`, then store it again.

**`API retries` with 429 in the run summary** — Hive allows 5 requests/second by default;
Claude Code backs off and retries on its own. Parallel tasks share the limit; ask Hive for a
higher one if it slows you down.

**A run fails with `api_error` and the bridge is mentioned** — `NN-<role>-bridge.log` in the
task folder has one line per API call with the provider's status and message. To see exactly
what the provider received, re-run with `MULTIAGENTS_BRIDGE_DUMP=/some/folder`: every translated
request is saved there as JSON (your code and prompts — delete the folder afterwards; the key is
never in it).

**`max_tokens` rejected** — set `"max_output_tokens"` in the provider block of
`~/.multiagents/config.json` to the provider's limit.

## Screenshots

**Blank PNG at `--width < 500`**
Headless Chrome lays out at ≥ 500px, so narrow shots render the page inside an iframe — pages
sending `X-Frame-Options`/CSP `frame-ancestors` refuse to render there. Retake at `--width 500+`.

**`no Chrome/Chromium/Edge found`** — install one, or capture with your own tool into the
task's `shots/` folder (any PNG there is handed to the fixer).

**Image is 500px wide with margins (Linux)** — no `sips`/ImageMagick found for the crop;
install `imagemagick` or ignore the margins.

## Windows

**`WinError 1314` (a required privilege is not held) during setup/self-test**
Fixed in v0.2.1: creating symlinks on Windows needs Developer Mode, so the worker config dir
now falls back to directory junctions and then to auto-refreshed copies. Update the plugin
(`claude plugin update multiagents@multiagents`); enabling Developer Mode (Settings → System →
For developers) also makes real symlinks work, but is not required.

**`python3` not found**
Windows installs Python as `python` / the `py` launcher; there is no `python3` shim. The
launchers try `py -3` and `python` (`bin/multiagents.cmd`) or `python3`, `python`, `py -3` (the
Git Bash script), and reject the Microsoft Store stub. If none exist, install Python 3.9+ from
python.org and re-run.

**A failed round ran twice (Windows, before 0.4.0)**
`multiagents.cmd` used to re-run the whole command with `python` whenever it exited non-zero,
so a failed or timed-out worker round was immediately repeated (and paid for twice). Fixed in
0.4.0: the launcher picks one interpreter and runs once.

**Storing the key (no macOS Keychain)**
Save it to `%USERPROFILE%\.multiagents\fireworks.key` (or `deepseek.key` / `hive.key`) as a
single line, or set a user environment variable `FIREWORKS_API_KEY` / `DEEPSEEK_API_KEY` /
`HIVE_API_KEY`. The CLI stages env
keys into a file itself, so workers never see the variable.

**General Windows notes**
Claude Code on Windows runs shell commands through Git Bash — install Git for Windows if
`doctor` complains about the shell. Worker stop/timeout uses `taskkill` under the hood.
`shot --ios` is macOS-only; `shot --url` works with Chrome or Edge installed.

## Codex

**`install-codex`: "Codex CLI not found"**
The desktop app ships `codex` inside its bundle without putting it on PATH. The command also
looks in `/Applications/ChatGPT.app/Contents/Resources/codex` and `Codex.app`; elsewhere, pass
`--codex /path/to/codex`.

**`$multiagents:lead` is missing, or Codex shows two lead skills**
Start a new thread — Codex loads plugins per thread. A leftover `$multiagents-lead` (no colon)
is the standalone skill from ≤ 0.3.0; `install-codex` removes it, or delete
`~/.agents/skills/multiagents-lead` by hand. `codex plugin list --marketplace multiagents` shows
what is installed.

**"has to run outside the Codex sandbox"**
Expected when Codex tried a worker command sandboxed: approve the escalated re-run. To stop
being asked each time, accept the suggested prefix rule (e.g. `…/bin/multiagents run`).

**"no API key found" in Codex, but the key is in the Keychain**
The command ran inside the sandbox, which hides the Keychain (older versions of the CLI did
not detect this). Re-run it escalated.

**A new version is out, Codex still runs the old one**
For a local clone, `git pull` it first. Then run `multiagents install-codex` again (it keeps your
marketplace source and re-copies the plugin), and start a new thread.

## Reading the artifacts

- `NN-<role>.log` — timeline: every tool call, `[error]` tool failures, `[stderr]` from the
  worker process, `[retry]` API retries. `grep error` it first.
- `NN-<role>-report.md` — what the worker claims; the lead treats it as a claim.
- `NN-<role>.jsonl` — full protocol transcript; heavy, last resort.
- `NN-<role>-bridge.log` — OpenAI-API providers only: status, time and tokens per API call,
  and the provider's error messages.
- `task.json` — rounds with outcome, tokens, cost, session ids, head commits.

## Resetting

- Worker Claude state: `rm -rf ~/.multiagents/worker-home` (recreated on next run).
- Self-test cache: `rm ~/.multiagents/selftest.json` or `multiagents selftest`.
- A task: `multiagents reject T00N`, then delete its folder under `.multiagents/tasks/` and
  `git branch -D ma/T00N-…` if unwanted.
