#!/usr/bin/env python3
"""multiagents: run cheap DeepSeek workers (via Fireworks) for a Claude Code lead.

The lead is your normal Claude Code session (for example Opus on a Claude subscription).
It plans, writes task specs and reviews. This CLI runs the workers that write, test and fix
code. Each worker is a headless `claude -p` process isolated from the lead's credentials:

  * every ANTHROPIC_* / CLAUDE* variable inherited from the host session is stripped
    (the desktop app passes its subscription auth to child processes through them),
  * the worker gets its own CLAUDE_CONFIG_DIR, so the lead's stored login is never found,
  * the Fireworks key reaches Claude Code only through apiKeyHelper,
  * before the first real run on a given Claude Code build, a leak self-test points a worker
    at a local capture server and refuses to continue unless only the Fireworks key was sent.

Only the Python standard library is used (3.9+).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "worker" / "prompts"
TEMPLATES = ROOT / "worker" / "templates"
STATE_HOME = Path(os.environ.get("MULTIAGENTS_HOME", str(Path.home() / ".multiagents"))).expanduser()
USER_CONFIG = STATE_HOME / "config.json"
PROJECT_CONFIG = Path(".claude") / "multiagents.json"
TEAM_DIR = ".multiagents"
ROLES = ("coder", "reviewer", "fixer", "scout")

FLASH = "accounts/fireworks/models/deepseek-v4p1-flash"

DEFAULT_CONFIG = {
    "base_url": "https://api.fireworks.ai/inference",
    "keychain_service": "fireworks-api",
    "key_file": str(STATE_HOME / "fireworks.key"),
    "models": {"coder": FLASH, "fixer": FLASH, "reviewer": FLASH, "scout": FLASH, "background": FLASH},
    "aliases": {"flash": FLASH},
    # USD per 1M tokens. Add entries for other models to get cost estimates.
    "prices": {FLASH: {"input": 0.22, "cached_input": 0.007, "output": 0.66}},
    "permission_mode": "acceptEdits",
    "allow": [],
    "deny": [],
    "max_turns": {"coder": 150, "fixer": 100, "reviewer": 120, "scout": 60},
    "timeout_minutes": 60,
    "idle_timeout_minutes": 15,
    "use_branches": True,
    "branch_prefix": "ma/",
    "worker_env": {},
}

# Tools a worker may run without a prompt (anything else is denied in headless mode and
# reported back, so the lead can extend "allow" in .claude/multiagents.json).
DEFAULT_ALLOW = [
    "Bash(ls *)", "Bash(ls)", "Bash(pwd)", "Bash(cat *)", "Bash(head *)", "Bash(tail *)",
    "Bash(wc *)", "Bash(grep *)", "Bash(rg *)", "Bash(tree *)", "Bash(diff *)", "Bash(sort *)",
    "Bash(uniq *)", "Bash(cut *)", "Bash(sed -n *)", "Bash(jq *)", "Bash(echo *)", "Bash(printf *)",
    "Bash(which *)", "Bash(file *)", "Bash(stat *)", "Bash(mkdir *)", "Bash(touch *)", "Bash(date*)",
    "Bash(sleep *)", "Bash(ps *)", "Bash(lsof *)", "Bash(curl *)",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)", "Bash(git blame*)",
    "Bash(git ls-files*)", "Bash(git rev-parse*)", "Bash(git grep *)", "Bash(git branch --show-current)",
    "Bash(git add *)", "Bash(git commit *)", "Bash(git restore *)", "Bash(git rm *)", "Bash(git mv *)",
    "Bash(npm test*)", "Bash(npm run *)", "Bash(npm ci*)", "Bash(npm install*)", "Bash(npx tsc*)",
    "Bash(npx jest*)", "Bash(npx vitest*)", "Bash(npx eslint*)", "Bash(npx prettier*)",
    "Bash(pnpm *)", "Bash(yarn *)", "Bash(bun test*)", "Bash(bun run *)", "Bash(node *)",
    "Bash(python *)", "Bash(python3 *)", "Bash(pytest*)", "Bash(ruff *)", "Bash(mypy *)",
    "Bash(uv run *)", "Bash(poetry run *)",
    "Bash(go test*)", "Bash(go build*)", "Bash(go vet*)", "Bash(gofmt *)",
    "Bash(cargo test*)", "Bash(cargo build*)", "Bash(cargo check*)", "Bash(cargo clippy*)", "Bash(cargo fmt*)",
    "Bash(swift build*)", "Bash(swift test*)", "Bash(swift package *)", "Bash(swiftc *)",
    "Bash(xcodebuild *)", "Bash(xcrun *)", "Bash(swiftlint*)", "Bash(swiftformat*)",
    "Bash(make)", "Bash(make *)", "Bash(cmake *)", "Bash(./gradlew *)", "Bash(gradle *)", "Bash(mvn *)",
    "Bash(dotnet build*)", "Bash(dotnet test*)", "Bash(bundle exec *)", "Bash(rspec*)", "Bash(rake *)",
]
DEFAULT_DENY = [
    "Bash(git push*)", "Bash(git reset*)", "Bash(git rebase*)", "Bash(git checkout *)",
    "Bash(git switch *)", "Bash(git merge*)", "Bash(git branch -d*)", "Bash(git branch -D*)",
    "Bash(git clean*)", "Bash(git stash*)", "Bash(git filter-branch*)", "Bash(git remote *)",
    "Bash(git config *)", "Bash(sudo *)", "Bash(rm -rf /*)", "Bash(rm -rf ~*)", "Bash(multiagents *)",
    "WebSearch",
]

# Variables kept even though they match the stripped prefixes.
KEEP_ENV = {"CLAUDE_CODE_GIT_BASH_PATH"}
STRIP_PREFIXES = ("ANTHROPIC_", "CLAUDE")
NOISE = re.compile(r"^\[claude-code:unrecognized_model\]|no stdin data received")


# ----------------------------------------------------------------------------- helpers

class Fail(Exception):
    pass


def die(msg: str) -> None:
    raise Fail(msg)


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def short_model(m: str) -> str:
    return m.rsplit("/", 1)[-1]


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        die(f"{path}: invalid JSON ({e})")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def repo_root() -> Path:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        die("not inside a git repository (workers need git to track and review their changes)")
    return Path(r.stdout.strip())


def load_config(repo: Path | None) -> dict:
    cfg = deep_merge(DEFAULT_CONFIG, read_json(USER_CONFIG, {}) or {})
    if repo is not None:
        cfg = deep_merge(cfg, read_json(repo / PROJECT_CONFIG, {}) or {})
    for role in ROLES:
        env_model = os.environ.get(f"MULTIAGENTS_{role.upper()}_MODEL")
        if env_model:
            cfg["models"][role] = env_model
    return cfg


def resolve_model(cfg: dict, name: str) -> str:
    return cfg["aliases"].get(name, name)


def render(text: str, **vals: str) -> str:
    for k, v in vals.items():
        text = text.replace("{{" + k + "}}", v)
    return text


# ----------------------------------------------------------------------------- claude + key

def claude_bin(cfg: dict) -> str:
    # Prefer the build the host session runs (inside the desktop app that is its bundled CLI).
    for cand in (os.environ.get("MULTIAGENTS_CLAUDE_BIN"), cfg.get("claude_bin"), os.environ.get("CLAUDE_CODE_EXECPATH")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    found = shutil.which("claude")
    if found:
        return found
    die("Claude Code executable not found; install the CLI or set MULTIAGENTS_CLAUDE_BIN")
    return ""


def claude_version(binary: str) -> str:
    r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60)
    return (r.stdout.strip() or r.stderr.strip()).split(" ")[0]


def key_source(cfg: dict) -> tuple[str | None, str | None]:
    """Return (description, shell command that prints the key). Never returns the key itself."""
    if os.environ.get("FIREWORKS_API_KEY"):
        return "env FIREWORKS_API_KEY", "printenv FIREWORKS_API_KEY"
    service = cfg["keychain_service"]
    if sys.platform == "darwin" and shutil.which("security"):
        r = subprocess.run(["security", "find-generic-password", "-s", service], capture_output=True)
        if r.returncode == 0:
            return f'macOS Keychain "{service}"', f"security find-generic-password -s {shlex.quote(service)} -w"
    key_file = Path(cfg["key_file"]).expanduser()
    if key_file.is_file():
        return f"file {key_file}", f"cat {shlex.quote(str(key_file))}"
    return None, None


def read_key(helper: str) -> str:
    r = subprocess.run(helper, shell=True, capture_output=True, text=True)
    key = r.stdout.strip()
    if r.returncode != 0 or not key:
        die("could not read the Fireworks API key")
    return key


NO_KEY_HELP = """no Fireworks API key found. Store it (do not paste it into chat), e.g. on macOS:
  security add-generic-password -s fireworks-api -a "$USER" -w
(the command asks for the key), or export FIREWORKS_API_KEY, or put it in ~/.multiagents/fireworks.key (chmod 600)."""


# ----------------------------------------------------------------------------- worker isolation

def worker_home(cfg: dict) -> Path:
    """Separate CLAUDE_CONFIG_DIR for workers: no stored login, no user MCP servers or plugins,
    but the user's own agents, skills and CLAUDE.md are linked in so workers can use them."""
    home = Path(cfg.get("worker_home") or STATE_HOME / "worker-home").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    user_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    if user_dir.resolve() != home.resolve():
        for name in ("agents", "skills", "CLAUDE.md"):
            src, dst = user_dir / name, home / name
            if src.exists() and not dst.exists() and not dst.is_symlink():
                dst.symlink_to(src)
    return home


def worker_env(cfg: dict, model: str, home: Path, base_url: str, keep_fireworks_env: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(STRIP_PREFIXES) or k in KEEP_ENV}
    if not keep_fireworks_env:
        env.pop("FIREWORKS_API_KEY", None)
    fast = resolve_model(cfg, cfg["models"].get("background") or "flash")
    env.update({
        "CLAUDE_CONFIG_DIR": str(home),
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": fast,
        "ANTHROPIC_SMALL_FAST_MODEL": fast,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "BASH_DEFAULT_TIMEOUT_MS": "600000",
        "BASH_MAX_TIMEOUT_MS": "1800000",
        "MULTIAGENTS_WORKER": "1",
    })
    env.update({k: str(v) for k, v in (cfg.get("worker_env") or {}).items()})
    return env


def worker_settings(cfg: dict, helper: str) -> dict:
    return {
        "apiKeyHelper": helper,
        "permissions": {
            "allow": DEFAULT_ALLOW + list(cfg.get("allow") or []),
            "deny": DEFAULT_DENY + list(cfg.get("deny") or []),
        },
        # Commits are written by DeepSeek, not Claude: no Claude co-author trailer.
        "includeCoAuthoredBy": False,
        "attribution": {"commit": "", "pr": ""},
    }


# ----------------------------------------------------------------------------- leak self-test

class _Capture(http.server.BaseHTTPRequestHandler):
    records: list = []

    def _handle(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        creds = {k.lower(): v for k, v in self.headers.items()
                 if k.lower() in ("authorization", "x-api-key", "x-fireworks-api-key", "cookie")}
        _Capture.records.append({"path": self.path, "creds": creds, "beta": self.headers.get("anthropic-beta", "")})
        body = b'{"type":"error","error":{"type":"authentication_error","message":"multiagents self-test"}}'
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = _handle
    do_GET = _handle

    def log_message(self, *args) -> None:
        pass


def _selftest_key(binary: str, version: str, env: dict) -> str:
    names = ",".join(sorted(env))
    return hashlib.sha256(f"{VERSION}|{binary}|{version}|{names}".encode()).hexdigest()[:16]


def run_selftest(cfg: dict, binary: str, force: bool = False) -> tuple[bool, str]:
    """Point an isolated worker at a local server and check which credentials it sends."""
    version = claude_version(binary)
    home = worker_home(cfg)
    canary = "fw_selftest_" + secrets.token_hex(8)
    model = resolve_model(cfg, cfg["models"]["coder"])
    probe_env = worker_env(cfg, model, home, "http://127.0.0.1:1", keep_fireworks_env=False)
    cache_file = STATE_HOME / "selftest.json"
    cache = read_json(cache_file, {}) or {}
    key = _selftest_key(binary, version, probe_env)
    if not force and cache.get(key, {}).get("result") == "pass":
        return True, f"cached pass for Claude Code {version}"

    _Capture.records = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cwd = STATE_HOME / "selftest-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    env = worker_env(cfg, model, home, f"http://127.0.0.1:{port}", keep_fireworks_env=False)
    cmd = [binary, "-p", "ping", "--output-format", "json", "--max-turns", "1", "--strict-mcp-config",
           "--settings", json.dumps(worker_settings(cfg, f"echo {canary}"))]
    try:
        subprocess.run(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        pass
    finally:
        server.shutdown()

    records = _Capture.records
    problems = []
    if not records:
        problems.append("the worker sent no request to the test endpoint")
    for r in records:
        values = list(r["creds"].values())
        if any("sk-ant" in v for v in values):
            problems.append(f"{r['path']}: an Anthropic credential was sent")
        if "oauth" in r["beta"]:
            problems.append(f"{r['path']}: request used the OAuth (subscription) beta header")
        if not any(canary in v for v in values):
            problems.append(f"{r['path']}: request did not authenticate with the Fireworks key")
    _Capture.records = []
    ok = not problems
    cache[key] = {"result": "pass" if ok else "fail", "at": now_iso(), "claude_version": version}
    write_json(cache_file, cache)
    if ok:
        return True, f"{len(records)} request(s) captured, only the Fireworks key was sent (Claude Code {version})"
    return False, "; ".join(sorted(set(problems)))


def ensure_selftest(cfg: dict, binary: str) -> None:
    ok, detail = run_selftest(cfg, binary)
    if not ok:
        die("leak self-test FAILED, refusing to start a worker: " + detail +
            "\nThe worker could expose your Claude credentials to the Fireworks endpoint on this Claude Code build.")


# ----------------------------------------------------------------------------- tasks

class Task:
    def __init__(self, repo: Path, path: Path):
        self.repo = repo
        self.dir = path
        self.meta_path = path / "task.json"
        self.meta = read_json(self.meta_path, {}) or {}

    @property
    def id(self) -> str:
        return self.meta["id"]

    def save(self) -> None:
        write_json(self.meta_path, self.meta)

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.repo))
        except ValueError:
            return str(p)


def team_dir(repo: Path) -> Path:
    d = repo / TEAM_DIR
    if not d.exists():
        (d / "tasks").mkdir(parents=True)
        exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = repo / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if f"/{TEAM_DIR}/" not in lines:
            with exclude.open("a") as f:
                f.write(f"\n# multiagents task files (local only)\n/{TEAM_DIR}/\n")
    return d


def all_tasks(repo: Path) -> list[Task]:
    tasks_dir = team_dir(repo) / "tasks"
    return [Task(repo, p) for p in sorted(tasks_dir.iterdir()) if (p / "task.json").exists()]


def find_task(repo: Path, ref: str) -> Task:
    ref = ref.strip()
    if ref.isdigit():
        ref = f"T{int(ref):03d}"
    for t in all_tasks(repo):
        if t.dir.name == ref or t.dir.name.split("-")[0].upper() == ref.upper():
            return t
    die(f"task {ref} not found (see: multiagents status)")
    return None  # unreachable


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40].strip("-") or "task"


# ----------------------------------------------------------------------------- git per task

def dirty_files(repo: Path) -> list[str]:
    out = git(repo, "status", "--porcelain")
    return [line for line in out.splitlines() if line.strip()]


def prepare_git(cfg: dict, task: Task, role: str, allow_dirty: bool) -> None:
    repo, m = task.repo, task.meta
    current = git(repo, "branch", "--show-current", check=False)
    if role == "scout" and not m.get("base_commit"):
        return  # read-only research: no branch needed
    if not m.get("base_commit"):
        dirty = dirty_files(repo)
        if dirty and not allow_dirty:
            die("working tree has uncommitted changes; commit or stash them before the first run of a task "
                "(or pass --allow-dirty):\n  " + "\n  ".join(dirty[:15]))
        m["base_commit"] = git(repo, "rev-parse", "HEAD")
        m["base_branch"] = current
        if cfg["use_branches"]:
            branch = cfg["branch_prefix"] + task.dir.name
            git(repo, "checkout", "-b", branch)
            m["branch"] = branch
        else:
            m["branch"] = current
        task.save()
    elif m.get("branch") and current != m["branch"]:
        if dirty_files(repo):
            die(f"on branch {current} with uncommitted changes; the task lives on {m['branch']}. "
                "Commit or stash first, then re-run.")
        git(repo, "checkout", m["branch"])


# ----------------------------------------------------------------------------- run a worker

def latest_round(task: Task, roles: tuple) -> dict | None:
    for r in reversed(task.meta.get("rounds", [])):
        if r["role"] in roles:
            return r
    return None


def read_report(task: Task, rnd: dict | None) -> str:
    if not rnd:
        return ""
    p = task.repo / rnd["report"]
    return p.read_text() if p.exists() else ""


def build_prompts(task: Task, role: str, n: int, report_rel: str, feedback: Path | None,
                  resuming: bool) -> tuple[str, str]:
    m = task.meta
    vals = {
        "task_id": m["id"], "title": m.get("title", ""), "branch": m.get("branch", ""),
        "base_commit": m.get("base_commit", "")[:12], "report_path": report_rel, "round": str(n),
        "repo": str(task.repo), "task_dir": task.rel(task.dir),
    }
    system = render((PROMPTS / "common.md").read_text(), **vals) + "\n\n" + render((PROMPTS / f"{role}.md").read_text(), **vals)
    spec = (task.dir / "task.md").read_text()
    parts: list[str] = []
    if role == "fixer":
        impl_round = latest_round(task, ("coder", "fixer"))
        review = latest_round(task, ("reviewer",))
        if resuming:
            # A resumed session replays the coder's system prompt, so the fixer rules go here.
            parts.append(render((PROMPTS / "fixer.md").read_text(), **vals))
        else:
            parts.append(f"# Task spec ({m['id']})\n\n{spec}")
            impl = read_report(task, impl_round)
            if impl:
                parts.append(f"# Previous implementation report\n\n{impl}")
        if review and (not resuming or review["n"] > impl_round["n"]):
            parts.append("# Reviewer report (the reviewer may have changed code after your last turn; "
                         f"re-read files before editing)\n\n{read_report(task, review)}")
        parts.append(f"# Lead feedback ({task.rel(feedback)})\n\n{feedback.read_text()}")
        shots = sorted((task.dir / "shots").glob("*")) if (task.dir / "shots").is_dir() else []
        if shots:
            parts.append("Screenshots from the lead (open them with the Read tool to see them):\n" +
                         "\n".join(f"- {task.rel(s)}" for s in shots))
    else:
        parts.append(f"# Task spec ({m['id']})\n\n{spec}")
        if role == "reviewer":
            impl = read_report(task, latest_round(task, ("coder", "fixer")))
            parts.append(f"# Implementation report (verify, don't trust)\n\n{impl or '(no report was written)'}")
            parts.append(f"Changes to review: `git diff {vals['base_commit']}` (base commit of this task) "
                         f"and `git log --oneline {vals['base_commit']}..HEAD`.")
    parts.append(f"When you are done, write your report to `{report_rel}` (your last step).")
    return system, "\n\n---\n\n".join(parts)


def summarize_tool(name: str, inp: dict, repo: Path) -> str:
    def rel(p: str) -> str:
        try:
            return str(Path(p).relative_to(repo))
        except ValueError:
            return p
    if name == "Bash":
        return inp.get("command", "").replace("\n", " ")[:160]
    if name in ("Read", "Edit", "Write", "NotebookEdit"):
        return rel(inp.get("file_path", inp.get("notebook_path", "")))
    if name in ("Grep", "Glob"):
        return f"{inp.get('pattern', '')} {inp.get('path', '')}".strip()
    if name in ("Agent", "Task"):
        return f"{inp.get('subagent_type', '')}: {inp.get('description', '')}"
    return json.dumps(inp, ensure_ascii=False)[:120]


def cost_of(cfg: dict, model_usage: dict) -> tuple[float, bool]:
    total, known = 0.0, True
    for model, u in (model_usage or {}).items():
        p = cfg["prices"].get(model)
        if not p:
            known = False
            continue
        fresh = u.get("inputTokens", 0) + u.get("cacheCreationInputTokens", 0)
        total += (fresh * p["input"] + u.get("cacheReadInputTokens", 0) * p["cached_input"]
                  + u.get("outputTokens", 0) * p["output"]) / 1e6
    return total, known


def cmd_run(args) -> int:
    if os.environ.get("MULTIAGENTS_WORKER"):
        die("refusing to dispatch a worker from inside a worker")
    repo = repo_root()
    cfg = load_config(repo)
    task = find_task(repo, args.task)
    role = args.role
    if role == "fixer" and not args.feedback:
        die("fixer needs --feedback <file> (the lead's review feedback)")
    feedback = None
    if args.feedback:
        feedback = Path(args.feedback)
        if not feedback.is_absolute():
            candidate = task.dir / feedback
            feedback = candidate if candidate.exists() else (Path.cwd() / feedback)
        if not feedback.exists():
            die(f"feedback file not found: {args.feedback}")
    model = resolve_model(cfg, args.model or cfg["models"][role])
    binary = claude_bin(cfg)
    source, helper = key_source(cfg)
    if not helper:
        die(NO_KEY_HELP)
    lock = acquire_lock(repo, task.id, role)
    try:
        ensure_selftest(cfg, binary)
        prepare_git(cfg, task, role, args.allow_dirty)
        return _run_worker(args, cfg, repo, task, role, model, binary, source, helper, feedback)
    finally:
        lock.unlink(missing_ok=True)


def acquire_lock(repo: Path, task_id: str, role: str) -> Path:
    """Workers share the working tree, so only one may run per repository at a time."""
    lock = team_dir(repo) / "worker.lock"
    held = read_json(lock, None) if lock.exists() else None
    if held:
        try:
            os.kill(held["pid"], 0)
            die(f"another worker is running in this repo ({held['role']} on {held['task']}, pid {held['pid']}); "
                "wait for it to finish")
        except (ProcessLookupError, KeyError, TypeError):
            pass  # stale lock
    write_json(lock, {"pid": os.getpid(), "task": task_id, "role": role, "started": now_iso()})
    return lock


def _run_worker(args, cfg, repo, task, role, model, binary, source, helper, feedback) -> int:
    rounds = task.meta.setdefault("rounds", [])
    n = len(rounds) + 1
    tag = f"{n:02d}-{role}"
    report = task.dir / f"{tag}-report.md"
    log_path, raw_path = task.dir / f"{tag}.log", task.dir / f"{tag}.jsonl"

    resume_id = None
    if role == "fixer" and not args.fresh:
        prev = latest_round(task, ("coder", "fixer"))
        if prev and prev.get("session_id") and prev.get("model") == model:
            resume_id = prev["session_id"]
    system, prompt = build_prompts(task, role, n, task.rel(report), feedback, resuming=bool(resume_id))

    mode = args.permission_mode or cfg["permission_mode"]
    max_turns = args.max_turns or cfg["max_turns"].get(role)
    cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", mode,
           "--settings", json.dumps(worker_settings(cfg, helper)), "--strict-mcp-config",
           "--append-system-prompt", system]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    fallback = resolve_model(cfg, cfg["models"].get("fallback") or "flash")
    if fallback != model:
        cmd += ["--fallback-model", fallback]  # used by Claude Code when the main model is overloaded
    if role == "scout":
        cmd += ["--disallowedTools", "Edit", "NotebookEdit"]
    if resume_id:
        cmd += ["--resume", resume_id]
    env = worker_env(cfg, model, worker_home(cfg), cfg["base_url"], keep_fireworks_env=source.startswith("env"))

    head_before = git(repo, "rev-parse", "HEAD")
    rnd = {"n": n, "role": role, "model": model, "started": now_iso(), "report": task.rel(report),
           "log": task.rel(log_path), "head_before": head_before, "resumed": bool(resume_id),
           "feedback": str(feedback) if feedback else None}
    rounds.append(rnd)
    task.meta["status"] = {"coder": "in_progress", "fixer": "fixing", "reviewer": "in_review", "scout": "scouting"}[role]
    task.save()

    timeout = (args.timeout or cfg["timeout_minutes"]) * 60
    idle_limit = cfg["idle_timeout_minutes"] * 60
    start = last = time.time()
    state = {"init": None, "result": None, "killed": None, "errors": 0}
    denials: dict[str, int] = {}

    log = log_path.open("w", buffering=1)
    raw = raw_path.open("w", buffering=1)

    def logline(text: str) -> None:
        log.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")

    logline(f"{role} on {short_model(model)} for {task.id}{' (resumed session)' if resume_id else ''}")
    proc = subprocess.Popen(cmd, cwd=repo, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    proc.stdin.write(prompt)
    proc.stdin.close()

    def stop_worker(signum, _frame) -> None:
        # The worker runs in its own process group; take it down with us.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, stop_worker)

    def watchdog() -> None:
        while proc.poll() is None:
            t = time.time()
            reason = "timeout" if t - start > timeout else "idle timeout" if t - last > idle_limit else None
            if reason:
                state["killed"] = reason
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(10)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return
            time.sleep(5)

    threading.Thread(target=watchdog, daemon=True).start()
    for line in proc.stdout:
        last = time.time()
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            raw.write(line)
            if line.strip() and not NOISE.search(line):
                logline("[stderr] " + line.strip()[:300])
            continue
        typ, subtype = ev.get("type"), ev.get("subtype")
        if typ == "system" and subtype == "thinking_tokens":
            continue  # per-token progress ticks: keep them out of the transcript
        raw.write(line)
        sub = "  > " if ev.get("parent_tool_use_id") else ""
        if typ == "system" and subtype == "init":
            if state["init"] is None:
                state["init"] = ev
                logline(f"session {ev.get('session_id')} · {len(ev.get('tools', []))} tools · agents: {', '.join(ev.get('agents', []))}")
        elif typ == "system" and subtype == "api_retry":
            state["retries"] = state.get("retries", 0) + 1
            logline(f"[retry] API attempt {ev.get('attempt')}/{ev.get('max_retries')} failed "
                    f"({ev.get('error_status') or ev.get('error')}); next in {ev.get('retry_delay_ms', 0) / 1000:.0f}s")
        elif typ == "assistant":
            for c in ev.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    logline(f"{sub}[{c.get('name')}] {summarize_tool(c.get('name', ''), c.get('input') or {}, repo)}")
                elif c.get("type") == "text" and c.get("text", "").strip():
                    logline(f"{sub}[say] " + c["text"].strip().replace("\n", " ")[:300])
        elif typ == "user":
            content = ev.get("message", {}).get("content")
            for c in content if isinstance(content, list) else []:
                if c.get("type") == "tool_result" and c.get("is_error"):
                    state["errors"] += 1
                    text = c.get("content")
                    if isinstance(text, list):
                        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict))
                    logline(f"{sub}[error] " + str(text).replace("\n", " ")[:300])
        elif typ == "result":
            state["result"] = ev
            for d in ev.get("permission_denials") or []:
                inp = d.get("tool_input") or {}
                label = d.get("tool_name", "?") + (f"({inp['command'][:80]})" if "command" in inp else "")
                denials[label] = denials.get(label, 0) + 1
    proc.wait()
    elapsed = time.time() - start
    log.close()
    raw.close()

    res = state["result"] or {}
    if not report.exists() and res.get("result"):
        report.write_text(f"# {task.id} {role} report (auto-captured: the worker did not write a report)\n\n{res['result']}\n")
    report_text = report.read_text() if report.exists() else ""
    status_match = re.search(r"^\s*(?:\*\*)?(Status|Verdict)(?:\*\*)?\s*:\s*\**\s*([A-Z_]+)", report_text, re.M)
    worker_status = status_match.group(2) if status_match else "UNKNOWN"
    cost, cost_known = cost_of(cfg, res.get("modelUsage") or {})
    usage = {"input": 0, "cached": 0, "output": 0}
    for u in (res.get("modelUsage") or {}).values():
        usage["input"] += u.get("inputTokens", 0) + u.get("cacheCreationInputTokens", 0)
        usage["cached"] += u.get("cacheReadInputTokens", 0)
        usage["output"] += u.get("outputTokens", 0)
    head_after = git(repo, "rev-parse", "HEAD")
    uncommitted = dirty_files(repo)
    outcome = state["killed"] or res.get("subtype") or f"exit {proc.returncode}"

    rnd.update({
        "ended": now_iso(), "seconds": round(elapsed), "outcome": outcome, "worker_status": worker_status,
        "turns": res.get("num_turns"), "session_id": res.get("session_id") or (state["init"] or {}).get("session_id"),
        "tokens": usage, "cost_usd": round(cost, 4) if cost_known else None, "head_after": head_after,
        "uncommitted": len(uncommitted), "denied": denials,
    })
    task.meta["status"] = {"coder": "implemented", "fixer": "fixed", "reviewer": "reviewed", "scout": "scouted"}[role]
    task.save()

    base = task.meta.get("base_commit")  # scouts never set one
    stat = (git(repo, "diff", "--shortstat", base, check=False) or "no changes vs base") if base else "n/a (read-only task)"
    commits = git(repo, "log", "--oneline", f"{head_before}..{head_after}", check=False).splitlines()
    print(f"== {task.id} · round {n} · {role} · {short_model(model)} ==")
    print(f"outcome: {outcome} · worker status: {worker_status} · {res.get('num_turns', '?')} turns · {fmt_secs(elapsed)}")
    print(f"tokens: in {fmt_tokens(usage['input'])} · cached {fmt_tokens(usage['cached'])} · out {fmt_tokens(usage['output'])}"
          + (f" · est. ${cost:.3f}" if cost_known else " · cost: add model price to config"))
    print(f"git: {task.meta.get('branch') or '(no branch)'} · {len(commits)} new commit(s) this round · task total vs base: {stat}")
    if uncommitted:
        print(f"uncommitted: {len(uncommitted)} file(s): " + ", ".join(l[3:] for l in uncommitted[:8]))
    if denials:
        print("denied (add to \"allow\" in .claude/multiagents.json if needed): " +
              ", ".join(f"{k} x{v}" for k, v in denials.items()))
    if state["errors"]:
        print(f"tool errors during run: {state['errors']} (details: {task.rel(log_path)})")
    if state.get("retries"):
        print(f"API retries: {state['retries']} (Fireworks was slow or flaky for {short_model(model)})")
    print(f"report: {task.rel(report)}   log: {task.rel(log_path)}")
    print("----- report -----")
    lines = report_text.splitlines()
    print("\n".join(lines[:150]) if lines else "(empty)")
    if len(lines) > 150:
        print(f"... ({len(lines) - 150} more lines in {task.rel(report)})")
    return 0 if outcome == "success" else 1


# ----------------------------------------------------------------------------- other commands

def cmd_new(args) -> int:
    repo = repo_root()
    tasks_dir = team_dir(repo) / "tasks"
    nums = [int(m.group(1)) for p in tasks_dir.iterdir() if (m := re.match(r"T(\d+)", p.name))]
    tid = f"T{(max(nums) + 1) if nums else 1:03d}"
    path = tasks_dir / f"{tid}-{slugify(args.slug or args.title)}"
    path.mkdir()
    (path / "shots").mkdir()
    title = args.title or args.slug
    (path / "task.md").write_text(render((TEMPLATES / "task.md").read_text(), task_id=tid, title=title))
    write_json(path / "task.json", {"id": tid, "title": title, "created": now_iso(), "status": "draft", "rounds": []})
    print(f"{tid} created: {path.relative_to(repo)}/task.md  (fill in the spec, then: multiagents run coder {tid})")
    return 0


def cmd_feedback(args) -> int:
    repo = repo_root()
    task = find_task(repo, args.task)
    n = len(list(task.dir.glob("feedback-*.md"))) + 1
    path = task.dir / f"feedback-{n}.md"
    path.write_text(render((TEMPLATES / "feedback.md").read_text(), task_id=task.id, round=str(n)))
    print(f"{task.rel(path)} created (fill it in, then: multiagents run fixer {task.id} --feedback {path.name})")
    return 0


def cmd_status(args) -> int:
    repo = repo_root()
    cfg = load_config(repo)
    tasks = [find_task(repo, args.task)] if args.task else all_tasks(repo)
    if not tasks:
        print("no tasks yet (multiagents new <slug> --title ...)")
        return 0
    for t in tasks:
        m = t.meta
        rounds = m.get("rounds", [])
        spent = sum(r.get("cost_usd") or 0 for r in rounds)
        print(f"{m['id']}  {m.get('status', '?'):<12} {m.get('title', '')}  [{t.rel(t.dir)}]")
        for r in rounds if args.task or args.verbose else rounds[-3:]:
            cost = f"${r['cost_usd']:.3f}" if r.get("cost_usd") is not None else "-"
            print(f"   {r['n']:>2}. {r['role']:<8} {short_model(r['model']):<24} {r.get('outcome', 'running'):<16} "
                  f"{r.get('worker_status', ''):<16} {fmt_secs(r.get('seconds', 0)):>7} {cost:>8}")
        if rounds:
            print(f"   total est. cost: ${spent:.3f}")
    return 0


def cmd_diff(args) -> int:
    repo = repo_root()
    task = find_task(repo, args.task)
    base = task.meta.get("base_commit")
    if not base:
        die(f"{task.id} has not been started yet")
    current = git(repo, "branch", "--show-current", check=False)
    target = [] if current == task.meta.get("branch") else [task.meta["branch"]]
    stat = args.stat or "--stat" in args.rest
    paths = [p for p in args.rest if p not in ("--", "--stat")]
    r = subprocess.run(["git", "diff", *(["--stat"] if stat else []), base, *target, "--", *paths], cwd=repo)
    return r.returncode


def cmd_accept(args) -> int:
    repo = repo_root()
    task = find_task(repo, args.task)
    m = task.meta
    if not m.get("base_commit"):
        die(f"{task.id} has no work to accept")
    if dirty_files(repo):
        die("uncommitted changes in the working tree; commit (or discard) them before accepting")
    if m.get("branch") and m.get("base_branch") and m["branch"] != m["base_branch"]:
        git(repo, "checkout", m["base_branch"])
        if args.squash:
            git(repo, "merge", "--squash", m["branch"])
            git(repo, "commit", "-m", f"{task.id}: {m.get('title', '')}")
        else:
            git(repo, "merge", "--no-ff", m["branch"], "-m", f"Merge {task.id}: {m.get('title', '')}")
    m["status"] = "accepted"
    m["accepted"] = now_iso()
    task.save()
    print(f"{task.id} accepted" + (f" and merged into {m.get('base_branch')}" if m.get("branch") != m.get("base_branch") else ""))
    return 0


def cmd_reject(args) -> int:
    repo = repo_root()
    task = find_task(repo, args.task)
    m = task.meta
    if m.get("base_branch") and git(repo, "branch", "--show-current", check=False) != m["base_branch"]:
        if dirty_files(repo):
            die("uncommitted changes in the working tree; commit or discard them first")
        git(repo, "checkout", m["base_branch"])
    m["status"] = "rejected"
    task.save()
    kept = f"; branch {m['branch']} kept for reference" if m.get("branch") and m.get("branch") != m.get("base_branch") else ""
    print(f"{task.id} rejected{kept}")
    return 0


def http_json(url: str, key: str, payload: dict | None = None, timeout: int = 60):
    headers = {"x-api-key": key, "Authorization": f"Bearer {key}", "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}
    except urllib.error.URLError as e:
        if "CERTIFICATE" not in str(e):
            raise
        # Some Python builds lack CA certificates: fall back to curl, key passed via stdin config.
        conf = "".join(f'header = "{k}: {v}"\n' for k, v in headers.items())
        if data:
            conf += "data = " + json.dumps(data.decode()) + "\n"
        r = subprocess.run(["curl", "-sS", "-m", str(timeout), "-w", "\n%{http_code}", "-K", "-", url],
                           input=conf, capture_output=True, text=True)
        body, _, code = r.stdout.rpartition("\n")
        return int(code or 0), json.loads(body or "{}")


def cmd_doctor(args) -> int:
    ok = True

    def line(label: str, status: str, detail: str = "") -> None:
        print(f"  {label:<11} {status:<5} {detail}")

    print(f"multiagents {VERSION} doctor")
    try:
        repo = repo_root()
    except Fail:
        repo = None
    cfg = load_config(repo)
    binary = claude_bin(cfg)
    line("claude", "OK", f"{binary} ({claude_version(binary)})")
    source, helper = key_source(cfg)
    if not helper:
        line("api key", "FAIL", "not found")
        print("\n" + NO_KEY_HELP)
        return 1
    line("api key", "OK", source)
    key = read_key(helper)
    try:
        code, body = http_json(cfg["base_url"] + "/v1/models", key)
        ids = [m.get("id", "") for m in body.get("data", [])] if code == 200 else []
        line("fireworks", "OK" if code == 200 else "FAIL", f"HTTP {code}, {len(ids)} models visible")
        ok &= code == 200
        wanted = sorted({resolve_model(cfg, cfg["models"][r]) for r in ROLES})
        for model in wanted:
            roles = [r for r in ROLES if resolve_model(cfg, cfg["models"][r]) == model]
            if args.quick:
                seen = model in ids
                line("model", "OK" if seen else "WARN", f"{short_model(model)} ({', '.join(roles)}) {'listed' if seen else 'not in /v1/models list'}")
                continue
            t0 = time.time()
            code, body = http_json(cfg["base_url"] + "/v1/messages", key,
                                   {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "Reply OK"}]})
            good = code == 200
            ok &= good
            detail = f"{time.time() - t0:.1f}s" if good else (body.get("error", {}) or {}).get("message", f"HTTP {code}")
            line("model", "OK" if good else "FAIL", f"{short_model(model)} ({', '.join(roles)}) {detail}")
    except Exception as e:  # network problems should not hide the other checks
        ok = False
        line("fireworks", "FAIL", str(e)[:200])
    del key
    passed, detail = run_selftest(cfg, binary, force=args.force)
    ok &= passed
    line("isolation", "PASS" if passed else "FAIL", detail)
    if repo:
        branch = git(repo, "branch", "--show-current", check=False)
        dirty = dirty_files(repo)
        line("repo", "OK", f"{repo} (branch {branch}, {'clean' if not dirty else f'{len(dirty)} uncommitted'})")
        pc = repo / PROJECT_CONFIG
        line("config", "OK", f"user {USER_CONFIG if USER_CONFIG.exists() else '(defaults)'}; project {pc if pc.exists() else '(none)'}")
    line("workers", "OK", f"coder={short_model(resolve_model(cfg, cfg['models']['coder']))} "
                           f"reviewer={short_model(resolve_model(cfg, cfg['models']['reviewer']))} "
                           f"mode={cfg['permission_mode']}")
    return 0 if ok else 1


CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]


def cmd_shot(args) -> int:
    """Save a screenshot into a task's shots/ folder so the fixer (vision model) can see it."""
    repo = repo_root()
    task = find_task(repo, args.task)
    shots = task.dir / "shots"
    shots.mkdir(exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", args.name)
    path = shots / (name if name.endswith(".png") else name + ".png")
    if args.ios:
        r = subprocess.run(["xcrun", "simctl", "io", args.device, "screenshot", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            die("simulator screenshot failed: " + (r.stderr.strip() or r.stdout.strip()))
    elif args.url:
        chrome = next((c for c in CHROME_CANDIDATES if (os.path.isfile(c) or shutil.which(c))), None)
        if not chrome:
            die("no Chrome/Chromium/Edge found for headless screenshots")
        width, height, target = args.width, args.height, args.url
        narrow = width < 500  # headless Chrome never lays out narrower than 500px: frame the page
        if narrow:
            wrapper = STATE_HOME / "shot-frame.html"
            # Centred, so the (always centred) crop below keeps exactly the framed page.
            wrapper.write_text('<!doctype html><body style="margin:0;display:flex;justify-content:center">'
                               f'<iframe src="{html.escape(args.url, quote=True)}" width="{width}" height="{height}" '
                               'style="border:0;display:block"></iframe></body>')
            target = wrapper.as_uri()
        cmd = [shutil.which(chrome) or chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--no-default-browser-check", f"--window-size={max(width, 500)},{height}",
               f"--screenshot={path}", f"--user-data-dir={STATE_HOME / 'chrome-profile'}"]
        if args.dark:
            cmd += ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"]
        cmd += ["--virtual-time-budget=3000", target]
        path.unlink(missing_ok=True)
        # Headless Chrome on macOS often keeps running after writing the file: wait for a
        # complete screenshot, then stop it ourselves.
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline, last_size = time.time() + 60, -1
        while time.time() < deadline:
            size = path.stat().st_size if path.exists() else -1
            if size > 0 and size == last_size:
                break
            if proc.poll() is not None and size <= 0:
                break
            last_size = size
            time.sleep(0.5)
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not path.exists() or path.stat().st_size == 0:
            die("headless screenshot failed (is the page reachable?)")
        if narrow and shutil.which("sips"):
            subprocess.run(["sips", "-c", str(height), str(width), str(path)],
                           capture_output=True)
    else:
        die("give --url <address> (web page, headless Chrome) or --ios (booted simulator)")
    print(task.rel(path))
    return 0


def cmd_models(args) -> int:
    cfg = load_config(None)
    _, helper = key_source(cfg)
    if not helper:
        die(NO_KEY_HELP)
    code, body = http_json(cfg["base_url"] + "/v1/models", read_key(helper))
    if code != 200:
        die(f"Fireworks returned HTTP {code}")
    ids = sorted(m.get("id", "") for m in body.get("data", []))
    for i in ids:
        if args.all or args.filter.lower() in i.lower():
            print(i)
    return 0


def cmd_selftest(args) -> int:
    cfg = load_config(None)
    passed, detail = run_selftest(cfg, claude_bin(cfg), force=True)
    print(("PASS: " if passed else "FAIL: ") + detail)
    return 0 if passed else 1


# ----------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="multiagents", description="Run DeepSeek workers (via Fireworks) for a Claude Code lead.")
    p.add_argument("--version", action="version", version=f"multiagents {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="check claude, key, Fireworks, models and credential isolation")
    s.add_argument("--quick", action="store_true", help="skip the per-model test requests")
    s.add_argument("--force", action="store_true", help="re-run the leak self-test even if cached")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("selftest", help="re-run the credential leak self-test")
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("models", help="list models your Fireworks key can see")
    s.add_argument("filter", nargs="?", default="deepseek")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_models)

    s = sub.add_parser("new", help="create a task folder with a spec template")
    s.add_argument("slug", help="short name, e.g. login-validation")
    s.add_argument("--title", default="", help="human title")
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("run", help="run a worker on a task (blocks until it finishes)")
    s.add_argument("role", choices=ROLES)
    s.add_argument("task", help="task id, e.g. T001")
    s.add_argument("--model", help="Fireworks model id or alias (flash)")
    s.add_argument("--feedback", help="feedback file for the fixer (relative to the task folder or cwd)")
    s.add_argument("--fresh", action="store_true", help="fixer: start a new session instead of resuming the coder's")
    s.add_argument("--max-turns", type=int)
    s.add_argument("--timeout", type=int, help="minutes")
    s.add_argument("--permission-mode", choices=["acceptEdits", "bypassPermissions", "dontAsk", "manual"])
    s.add_argument("--allow-dirty", action="store_true", help="first run: allow uncommitted changes in the tree")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("feedback", help="create the next feedback-N.md for a task from the template")
    s.add_argument("task")
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("shot", help="save a screenshot into a task's shots/ folder (for feedback)")
    s.add_argument("task")
    s.add_argument("name", help="file name, e.g. login-dark-360")
    s.add_argument("--url", help="web page to capture with headless Chrome")
    s.add_argument("--width", type=int, default=1280)
    s.add_argument("--height", type=int, default=900)
    s.add_argument("--dark", action="store_true", help="emulate dark mode (web)")
    s.add_argument("--ios", action="store_true", help="capture the iOS Simulator screen")
    s.add_argument("--device", default="booted", help="simulator UDID/name for --ios")
    s.set_defaults(fn=cmd_shot)

    s = sub.add_parser("status", help="list tasks and worker rounds")
    s.add_argument("task", nargs="?")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("diff", help="show a task's changes vs its base commit")
    s.add_argument("task")
    s.add_argument("--stat", action="store_true")
    s.add_argument("rest", nargs=argparse.REMAINDER, help="[--] paths to limit the diff to")
    s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("accept", help="merge the task branch into its base branch")
    s.add_argument("task")
    s.add_argument("--squash", action="store_true")
    s.set_defaults(fn=cmd_accept)

    s = sub.add_parser("reject", help="mark a task rejected and return to its base branch")
    s.add_argument("task")
    s.set_defaults(fn=cmd_reject)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except Fail as e:
        print(f"multiagents: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
