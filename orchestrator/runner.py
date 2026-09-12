"""Spawns real Claude Code CLI sessions and streams their output into SQLite.

One app session == one Claude Code session UUID == one folder.
Turn 1 uses `--session-id <uuid>`; every later turn uses `--resume <uuid>`,
so the conversation keeps its full memory exactly like an interactive session.
The prompt is handed over on stdin, verbatim -- never through argv -- so it is
never reshaped by shell quoting and has no length limit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from pathlib import Path

from . import config, db, notify

log = logging.getLogger("ccremote.runner")

STREAM_LIMIT = 16 * 1024 * 1024  # `system/init` lines are far bigger than the 64 KiB default
MAX_TEXT = 20000
MAX_TOOL_PREVIEW = 1500
NEEDS_HUMAN = "NEEDS_HUMAN:"

# Only these are inherited. Copying the whole environment would leak the
# orchestrator's own context into the session -- and if the orchestrator is
# itself started from a Claude Code session, the child picks up that session's
# CLAUDE_CODE_SESSION_ID and writes files into the wrong place. It would also
# hand over any ANTHROPIC_API_KEY, silently moving billing off the subscription.
BASE_ENV_KEYS = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TZ", "TMPDIR", "TERM", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR",
    # How a headless server authenticates on a Claude subscription, from
    # `claude setup-token`. Distinct from ANTHROPIC_API_KEY, which switches
    # billing to API metering and is deliberately NOT inherited.
    "CLAUDE_CODE_OAUTH_TOKEN",
)


def child_env() -> dict:
    """A clean environment for the Claude Code process."""
    env = {k: os.environ[k] for k in BASE_ENV_KEYS if k in os.environ}
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    for key in config.PASS_ENV:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n] + f"\n… [truncated, {len(s)} chars total]"


def _tool_summary(name: str, tool_input: dict) -> str:
    """A one-line human summary of a tool call, like the interactive UI shows."""
    if not isinstance(tool_input, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "prompt"):
        if key in tool_input and isinstance(tool_input[key], str):
            return f"{name}: {_truncate(tool_input[key], 300)}"
    if name == "TodoWrite":
        return name
    return f"{name}: {_truncate(json.dumps(tool_input, ensure_ascii=False), 300)}"


def _blocks(content) -> list[dict]:
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


class Runner:
    def __init__(self, tasks) -> None:
        self.tasks = tasks
        self.sem = asyncio.Semaphore(config.MAX_CONCURRENT)
        self._locks: dict[str, asyncio.Lock] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        # Sessions whose running turn was killed on purpose from the phone, so
        # the exit is reported as "stopped" rather than as a crash.
        self._stopped: set[str] = set()
        # asyncio only keeps weak references to tasks; a turn that nothing
        # else points at could be garbage-collected mid-run.
        self._tasks: set[asyncio.Task] = set()

    # -- public API ---------------------------------------------------------

    def new_session_id(self) -> str:
        return str(uuid.uuid4())

    def _lock(self, sid: str) -> asyncio.Lock:
        if sid not in self._locks:
            self._locks[sid] = asyncio.Lock()
        return self._locks[sid]

    def submit(self, sid: str, prompt: str) -> None:
        """Queue one turn. Turns of a session always run in submission order."""
        # A follow-up sent while a turn is still running waits on the lock;
        # the session is genuinely still "running" until then, so don't relabel
        # it as queued and make the phone say so.
        if not self.is_busy(sid):
            db.update_session(sid, status="queued", last_error=None)
        task = asyncio.create_task(self._turn(sid, prompt))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def is_busy(self, sid: str) -> bool:
        return sid in self._procs

    async def stop(self, sid: str) -> bool:
        proc = self._procs.get(sid)
        if proc is None:
            return False
        self._stopped.add(sid)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
        return True

    async def shutdown(self) -> None:
        for sid in list(self._procs):
            await self.stop(sid)

    # -- internals ----------------------------------------------------------

    async def _turn(self, sid: str, prompt: str) -> None:
        try:
            async with self._lock(sid):
                async with self.sem:
                    await self._run_claude(sid, prompt)
        except Exception as exc:  # never let a turn take the server down
            log.exception("turn failed for %s", sid)
            db.add_message(sid, "error", f"Orchestrator error: {exc}")
            db.update_session(sid, status="error", last_error=str(exc))

    def _build_cmd(self, sid: str, profile, resume: bool,
                   model: str = None, effort: str = None,
                   permission_mode: str = None) -> list[str]:
        """Build the CLI invocation.

        `model`, `effort` and `permission_mode` are the per-session choices made
        on the phone. Permission mode governs what gets *asked*; the profile's
        `--tools` list governs what exists at all, and no mode can add a tool
        back -- which is what keeps a read-only profile read-only even on "auto".
        """
        cmd = [
            config.CLAUDE_BIN,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model or profile.model,
            "--permission-mode", permission_mode or profile.permission_mode,
        ]
        cmd += ["--resume", sid] if resume else ["--session-id", sid]
        if profile.max_budget_usd is not None:
            cmd += ["--max-budget-usd", str(profile.max_budget_usd)]
        chosen_effort = effort or profile.effort
        if chosen_effort:
            cmd += ["--effort", chosen_effort]
        if profile.fallback_model:
            cmd += ["--fallback-model", profile.fallback_model]
        if profile.append_system_prompt:
            cmd += ["--append-system-prompt", profile.append_system_prompt]
        if profile.strict_mcp_config:
            cmd += ["--strict-mcp-config"]
        # --restricted confines the file tools to the working directories.
        # Without it, an allowlisted Read reaches any path on the host.
        if profile.restricted:
            cmd += ["--restricted"]
        if profile.settings:
            cmd += ["--settings", json.dumps(profile.settings)]
        # --tools removes tools from the session entirely; --allowedTools only
        # says which calls may proceed without asking.
        if profile.tools:
            cmd += ["--tools", ",".join(profile.tools)]
        # Variadic options go last so they cannot swallow following arguments.
        for d in profile.add_dirs:
            cmd += ["--add-dir", d]
        if profile.disallowed_tools:
            cmd += ["--disallowedTools", *profile.disallowed_tools]
        if profile.allowed_tools:
            cmd += ["--allowedTools", *profile.allowed_tools]
        return cmd

    async def _run_claude(self, sid: str, prompt: str) -> None:
        session = db.get_session(sid)
        if session is None:
            return
        cwd = session["cwd"]
        if not Path(cwd).is_dir():
            raise RuntimeError(f"folder does not exist on this machine: {cwd}")

        # Most sessions run in a folder that was browsed to or created from the
        # phone, so there is nothing to look up in tasks.yaml. Pinned shortcuts
        # still resolve by key; anything else is rebuilt from its own folder,
        # re-checked against the workspace root in case the root has since moved.
        project = self.tasks.projects.get(session["project"])
        if project is None:
            try:
                here = self.tasks.workspace.resolve(cwd)
            except ValueError as exc:
                raise RuntimeError(f"folder is outside the workspace: {cwd}") from exc
            project = self.tasks.workspace.project_for(
                here, session["profile"] or self.tasks.workspace.default_profile
            )
        profile = self.tasks.profile_for(project, session["profile"])

        resume = bool(session["claude_started"])
        cmd = self._build_cmd(
            sid, profile, resume,
            model=session["model"] if "model" in session.keys() else None,
            effort=session["effort"] if "effort" in session.keys() else None,
            permission_mode=(session["permission_mode"]
                             if "permission_mode" in session.keys() else None),
        )
        # Full argv: no secret is ever passed on a command line (the token lives
        # in the child's environment), and "which flags did it actually get?"
        # is the first question worth answering when a run misbehaves.
        log.info("run %s in %s: %s", sid[:8], cwd, " ".join(cmd))

        env = child_env()

        started = time.time()
        db.update_session(sid, status="running")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=STREAM_LIMIT,
        )
        self._procs[sid] = proc
        stderr_tail: deque[str] = deque(maxlen=40)
        saw_result = False

        try:
            # Hand the prompt over on stdin, byte for byte.
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

            async def drain_stderr() -> None:
                async for raw in proc.stderr:
                    line = raw.decode("utf-8", "replace").rstrip()
                    if line:
                        stderr_tail.append(line)
                        log.debug("[%s stderr] %s", sid[:8], line)

            stderr_task = asyncio.create_task(drain_stderr())

            async def read_stdout() -> bool:
                got_result = False
                while True:
                    try:
                        raw = await proc.stdout.readline()
                    except (asyncio.LimitOverrunError, ValueError):
                        log.warning("oversized stream line dropped for %s", sid[:8])
                        continue
                    if not raw:
                        break
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if self._handle_event(sid, evt):
                        got_result = True
                return got_result

            saw_result = await asyncio.wait_for(read_stdout(), timeout=config.TURN_TIMEOUT)
            await proc.wait()
            await asyncio.wait_for(stderr_task, timeout=5)

        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            db.add_message(
                sid, "error", f"Turn timed out after {config.TURN_TIMEOUT}s and was stopped."
            )
            db.update_session(sid, status="error", last_error="timeout")
            await notify.push("Claude Code · timed out", session["project_name"], "high", "warning")
            return
        finally:
            self._procs.pop(sid, None)

        if sid in self._stopped:
            # Killed from the phone: not a failure, and the conversation is
            # still resumable, so it goes back to idle.
            self._stopped.discard(sid)
            db.add_message(sid, "system", "Stopped from the phone.")
            db.update_session(sid, status="idle", last_error=None)
            log.info("run %s stopped after %.1fs", sid[:8], time.time() - started)
            return

        if not saw_result:
            tail = "\n".join(stderr_tail) or f"claude exited with code {proc.returncode}"
            db.add_message(sid, "error", _truncate(tail, 4000), {"exit_code": proc.returncode})
            db.update_session(sid, status="error", last_error=tail[-500:])
            await notify.push(
                "Claude Code · failed", f"{session['project_name']}\n{tail[-300:]}", "high", "rotating_light"
            )
            return

        log.info("run %s finished in %.1fs", sid[:8], time.time() - started)

    def _handle_event(self, sid: str, evt: dict) -> bool:
        """Persist one stream-json event. Returns True for the terminal `result` event."""
        etype = evt.get("type")

        if etype == "system":
            if evt.get("subtype") == "init":
                db.update_session(sid, claude_started=1)
            return False

        if etype == "assistant":
            for block in _blocks((evt.get("message") or {}).get("content")):
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        db.add_message(sid, "assistant", _truncate(text, MAX_TEXT))
                elif btype == "thinking":
                    text = (block.get("thinking") or "").strip()
                    if text:
                        db.add_message(sid, "thinking", _truncate(text, 4000))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    db.add_message(
                        sid,
                        "tool",
                        _tool_summary(name, block.get("input") or {}),
                        {"name": name, "tool_use_id": block.get("id")},
                    )
            return False

        if etype == "user":
            for block in _blocks((evt.get("message") or {}).get("content")):
                if block.get("type") != "tool_result":
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    parts = [b.get("text", "") for b in content if isinstance(b, dict)]
                    text = "\n".join(p for p in parts if p)
                else:
                    text = content if isinstance(content, str) else ""
                db.add_message(
                    sid,
                    "tool_result",
                    _truncate(text.strip(), MAX_TOOL_PREVIEW),
                    {"is_error": bool(block.get("is_error")), "tool_use_id": block.get("tool_use_id")},
                )
            return False

        if etype == "rate_limit_event":
            info = evt.get("rate_limit_info") or {}
            if info.get("status") and info["status"] != "allowed":
                db.add_message(sid, "system", f"Rate limit: {info.get('status')}", info)
            return False

        if etype == "result":
            cost = float(evt.get("total_cost_usd") or 0.0)
            is_error = bool(evt.get("is_error"))
            result_text = evt.get("result") or ""
            meta = {
                "cost_usd": cost,
                "duration_ms": evt.get("duration_ms"),
                "num_turns": evt.get("num_turns"),
                "subtype": evt.get("subtype"),
                "is_error": is_error,
                "permission_denials": evt.get("permission_denials") or [],
            }
            db.add_message(sid, "result", "", meta)
            db.bump_session(sid, cost, 1)

            if is_error:
                status = "error"
            elif NEEDS_HUMAN in result_text:
                status = "blocked"
            else:
                status = "idle"
            db.update_session(sid, status=status, last_error=result_text if is_error else None)

            session = db.get_session(sid) or {}
            name = session.get("project_name", "session")
            if status == "blocked":
                question = result_text.split(NEEDS_HUMAN, 1)[1].strip()
                asyncio.create_task(
                    notify.push(f"Claude needs you · {name}", _truncate(question, 300), "high", "raising_hand")
                )
            elif status == "error":
                asyncio.create_task(
                    notify.push(f"Claude failed · {name}", _truncate(result_text, 300), "high", "rotating_light")
                )
            else:
                asyncio.create_task(
                    notify.push(
                        f"Claude done · {name}",
                        f"{_truncate(result_text, 240)}\n\n${cost:.3f} · {round((evt.get('duration_ms') or 0)/1000)}s",
                        "default",
                        "white_check_mark",
                    )
                )
            return True

        return False


async def prepare_worktree(project, sid: str) -> tuple[str, str | None]:
    """Optional per-session git worktree. Returns (cwd, branch)."""
    short = sid[:8]
    branch = f"ccr/{short}"
    root = project.worktree_root or str(Path(project.path).parent / ".ccr-worktrees" / project.key)
    dest = str(Path(root) / short)
    Path(root).mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", project.path, "worktree", "add", "-b", branch, dest,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        log.warning("worktree failed for %s, using the project folder: %s", project.key, err.decode()[:300])
        return project.path, None
    return dest, branch
