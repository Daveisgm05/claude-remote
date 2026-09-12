"""HTTP API + PWA host for remote Claude Code sessions."""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import config, db, runner as runner_mod

logging.basicConfig(
    level=os.environ.get("CCR_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("ccremote")

WEB_DIR = Path(__file__).resolve().parent / "web"

# What the CLI actually accepts, checked against `claude --help` (2.1.263).
# Anything else is refused here rather than surfacing as a child-process crash.
# The aliases resolve to the current models -- each was asked what it is, and
# answered: fable -> claude-fable-5-1, opus -> claude-opus-5,
# sonnet -> claude-sonnet-5, haiku -> claude-haiku-4-5-20251001. Passing the
# alias rather than a pinned id means these keep tracking the latest release.
MODELS = [
    {"id": "opus", "label": "Opus 5", "note": "deepest reasoning, most expensive"},
    {"id": "sonnet", "label": "Sonnet 5", "note": "the everyday default"},
    {"id": "fable", "label": "Fable 5.1", "note": "fast, strong at writing"},
    {"id": "haiku", "label": "Haiku 4.5", "note": "cheapest and quickest"},
]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Exactly what `claude --permission-mode` accepts. bypassPermissions is
# deliberately absent: it is --dangerously-skip-permissions by another name, and
# nothing reachable from a phone should be able to select it.
PERMISSION_MODES = [
    {"id": "manual", "label": "Manual", "note": "asks before anything; safest"},
    {"id": "acceptEdits", "label": "Auto-accept edits", "note": "edits go through, commands still asked"},
    {"id": "plan", "label": "Plan", "note": "thinks it through, changes nothing"},
    {"id": "auto", "label": "Auto", "note": "runs without asking"},
    {"id": "dontAsk", "label": "Allowlist only", "note": "only what the profile permits; denies the rest"},
]
_MODEL_IDS = [m["id"] for m in MODELS]
_MODE_IDS = [m["id"] for m in PERMISSION_MODES]
_FULL_MODEL = re.compile(r"^claude-[a-z0-9][a-z0-9.-]{0,60}$")


def _check_mode(v):
    if v is None or v == "":
        return None
    if v in _MODE_IDS:
        return v
    raise ValueError(f"permission_mode must be one of {_MODE_IDS}")


def _check_model(v):
    if v is None or v == "":
        return None
    if v in _MODEL_IDS or _FULL_MODEL.match(v):
        return v
    raise ValueError(f"model must be one of {_MODEL_IDS}, or a claude-* full name")


def _check_effort(v):
    if v is None or v == "":
        return None
    if v in EFFORTS:
        return v
    raise ValueError(f"effort must be one of {EFFORTS}")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(config.DB_PATH)
    state["tasks"] = config.load_tasks()
    state["runner"] = runner_mod.Runner(state["tasks"])
    log.info("claude binary: %s", config.CLAUDE_BIN)
    log.info("projects: %s", ", ".join(state["tasks"].projects) or "(none configured)")
    if not config.TOKEN:
        log.warning("CCR_TOKEN is empty - the API is unauthenticated. Set it.")
    yield
    await state["runner"].shutdown()


app = FastAPI(title="Claude Code Remote", lifespan=lifespan)


def require_token(x_token: str = Header(default="")) -> None:
    if not config.TOKEN:
        raise HTTPException(500, "server has no CCR_TOKEN configured")
    if not secrets.compare_digest(x_token, config.TOKEN):
        raise HTTPException(401, "bad token")


def tasks():
    return state["tasks"]


def runner() -> runner_mod.Runner:
    return state["runner"]


# --- models ----------------------------------------------------------------

def _check_prompt(v: str) -> str:
    # min_length alone lets "   \n" through, and a blank prompt has nothing to
    # run -- it would only crash on the first-line title below.
    if not v.strip():
        raise ValueError("prompt cannot be blank")
    return v


class NewSession(BaseModel):
    """Either `project` (a pinned key from tasks.yaml) or `dir` (any folder
    inside the workspace root). The phone sends `dir` for anything it browsed
    to, which is most things."""

    prompt: str = Field(min_length=1)
    _v_prompt = field_validator("prompt")(lambda cls, v: _check_prompt(v))
    project: Optional[str] = None
    dir: Optional[str] = None
    profile: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None

    _v_model = field_validator("model")(lambda cls, v: _check_model(v))
    _v_effort = field_validator("effort")(lambda cls, v: _check_effort(v))
    _v_mode = field_validator("permission_mode")(lambda cls, v: _check_mode(v))


class PatchSession(BaseModel):
    """Change a live conversation: its name, or the model/effort of its next turn."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    model: Optional[str] = None
    effort: Optional[str] = None
    permission_mode: Optional[str] = None

    _v_model = field_validator("model")(lambda cls, v: _check_model(v))
    _v_effort = field_validator("effort")(lambda cls, v: _check_effort(v))
    _v_mode = field_validator("permission_mode")(lambda cls, v: _check_mode(v))


class NewFolder(BaseModel):
    parent: str = ""
    name: str = Field(min_length=1, max_length=120)


class NewMessage(BaseModel):
    prompt: str = Field(min_length=1)
    _v_prompt = field_validator("prompt")(lambda cls, v: _check_prompt(v))


# --- API -------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"ok": True, "claude_bin": config.CLAUDE_BIN, "auth_required": bool(config.TOKEN)}


@app.get("/api/config", dependencies=[Depends(require_token)])
async def get_config():
    t = tasks()
    return {
        "projects": [
            {"key": p.key, "name": p.name, "path": p.path, "profile": p.profile,
             "exists": p.exists, "branch_per_session": p.branch_per_session}
            for p in t.projects.values()
        ],
        "profiles": [
            {"name": pr.name, "model": pr.model, "permission_mode": pr.permission_mode,
             "allowed_tools": pr.allowed_tools, "max_budget_usd": pr.max_budget_usd,
             "effort": pr.effort}
            for pr in t.profiles.values()
        ],
        "workspace": {
            "root": str(t.workspace.root),
            "default_profile": t.workspace.default_profile,
        },
        "models": MODELS,
        "efforts": EFFORTS,
        "permission_modes": PERMISSION_MODES,
        "max_concurrent": config.MAX_CONCURRENT,
    }


@app.post("/api/reload", dependencies=[Depends(require_token)])
async def reload_tasks():
    state["tasks"] = config.load_tasks()
    state["runner"].tasks = state["tasks"]
    return {"ok": True, "projects": list(state["tasks"].projects)}


# --- folders ---------------------------------------------------------------
# The phone browses the workspace instead of choosing from a fixed list, so a
# new project needs no config change: make the folder, open it, start talking.

def _entry(d: Path, ws) -> dict:
    return {
        "name": d.name,
        "path": str(d),
        "rel": ws.label(d),
        "git": (d / ".git").exists(),
    }


@app.get("/api/folders", dependencies=[Depends(require_token)])
async def browse(path: str = ""):
    ws = tasks().workspace
    try:
        here = ws.resolve(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not here.is_dir():
        raise HTTPException(404, f"not a folder: {here}")

    dirs = []
    try:
        for child in sorted(here.iterdir(), key=lambda c: c.name.lower()):
            if child.name in config.SKIP_DIRS or child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
                # A symlink pointing out of the workspace is not browsable, so
                # don't offer it: tapping it would only ever 400.
                ws.resolve(str(child))
            except (OSError, ValueError):
                continue  # broken symlink, unstattable mount, or an escape
            dirs.append(_entry(child, ws))
    except PermissionError as exc:
        raise HTTPException(403, f"cannot read {here}") from exc

    root = ws.root.resolve()
    parent = None if here.resolve() == root else str(here.parent)
    return {
        "root": str(root),
        "path": str(here),
        "rel": ws.label(here),
        "name": here.name or "/",
        "parent": parent,
        "git": (here / ".git").exists(),
        "dirs": dirs,
    }


@app.post("/api/folders", dependencies=[Depends(require_token)])
async def make_folder(body: NewFolder):
    ws = tasks().workspace
    name = body.name.strip().strip("/")
    # A name, not a path: no traversal, no absolute escape, no separators.
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "folder name cannot contain / or ..")
    try:
        parent = ws.resolve(body.parent)
        target = ws.resolve(str(parent / name))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not parent.is_dir():
        raise HTTPException(404, f"no such folder: {parent}")
    if target.exists():
        raise HTTPException(409, f"'{name}' already exists here")
    target.mkdir(parents=True)
    log.info("created folder %s", target)
    return _entry(target, ws)


@app.delete("/api/folders", dependencies=[Depends(require_token)])
async def delete_folder(path: str, force: bool = False):
    """Remove a folder from the workspace.

    Refuses a non-empty folder unless `force`, and never the workspace root
    itself -- deleting that would take every project with it.
    """
    ws = tasks().workspace
    try:
        here = ws.resolve(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if here.resolve() == ws.root.resolve():
        raise HTTPException(400, "refusing to delete the workspace root")
    if not here.is_dir():
        raise HTTPException(404, f"no such folder: {here}")
    if here.is_symlink():
        raise HTTPException(400, "refusing to follow a symlink")

    entries = list(here.iterdir())
    if entries and not force:
        raise HTTPException(
            409,
            f"'{here.name}' is not empty ({len(entries)} item(s)). "
            "Confirm to delete it and everything inside.",
        )

    # Sessions that ran here keep their transcripts; only the files go.
    shutil.rmtree(here)
    log.warning("deleted folder %s (%d entries)", here, len(entries))
    return {"ok": True, "deleted": str(here), "entries": len(entries)}


@app.get("/api/sessions", dependencies=[Depends(require_token)])
async def list_sessions(limit: int = 50):
    return {"sessions": db.list_sessions(limit)}


@app.post("/api/sessions", dependencies=[Depends(require_token)])
async def create_session(body: NewSession):
    t = tasks()
    if body.project:
        project = t.projects.get(body.project)
        if project is None:
            raise HTTPException(404, f"unknown project '{body.project}'")
    elif body.dir is not None:
        try:
            here = t.workspace.resolve(body.dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project = t.workspace.project_for(here, body.profile or t.workspace.default_profile)
    else:
        raise HTTPException(400, "send either 'project' or 'dir'")
    if not project.exists:
        raise HTTPException(400, f"folder not found on the server: {project.path}")
    profile_name = body.profile or project.profile
    try:
        t.profile_for(project, profile_name)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    sid = runner().new_session_id()
    cwd, branch = project.path, None
    if project.branch_per_session:
        cwd, branch = await runner_mod.prepare_worktree(project, sid)

    first_line = body.prompt.strip().splitlines()[0]
    db.create_session(
        sid, project.key, project.name, cwd, profile_name, first_line[:80], branch,
        model=body.model, effort=body.effort, permission_mode=body.permission_mode,
    )
    db.add_message(sid, "user", body.prompt)
    runner().submit(sid, body.prompt)
    return {"session_id": sid, "cwd": cwd, "branch": branch}


@app.get("/api/sessions/{sid}", dependencies=[Depends(require_token)])
async def get_session(sid: str):
    session = db.get_session(sid)
    if session is None:
        raise HTTPException(404, "no such session")
    session["busy"] = runner().is_busy(sid)
    return session


@app.get("/api/sessions/{sid}/messages", dependencies=[Depends(require_token)])
async def get_messages(sid: str, after: int = 0):
    session = db.get_session(sid)
    if session is None:
        raise HTTPException(404, "no such session")
    session["busy"] = runner().is_busy(sid)
    return {"session": session, "messages": db.get_messages(sid, after)}


@app.post("/api/sessions/{sid}/messages", dependencies=[Depends(require_token)])
async def post_message(sid: str, body: NewMessage):
    session = db.get_session(sid)
    if session is None:
        raise HTTPException(404, "no such session")
    db.add_message(sid, "user", body.prompt)
    runner().submit(sid, body.prompt)
    return {"ok": True}


@app.patch("/api/sessions/{sid}", dependencies=[Depends(require_token)])
async def patch_session(sid: str, body: PatchSession):
    if db.get_session(sid) is None:
        raise HTTPException(404, "no such session")
    if body.title is not None:
        db.rename_session(sid, body.title.strip())
    # Applied from the next turn on: the current child already has its flags.
    # model_fields_set distinguishes "left alone" from "cleared back to the
    # profile default" -- both arrive as None once the validators have run.
    sent = body.model_fields_set
    changes = {k: getattr(body, k) for k in ("model", "effort", "permission_mode")
               if k in sent}
    if changes:
        db.update_session(sid, **changes)
    return {"ok": True, **{k: v for k, v in db.get_session(sid).items()
                           if k in ("title", "model", "effort", "permission_mode")}}


@app.post("/api/sessions/{sid}/stop", dependencies=[Depends(require_token)])
async def stop_session(sid: str):
    # The runner records the stop itself once the process is gone, so the
    # transcript and status are written exactly once and cannot race the
    # turn's own exit handling.
    stopped = await runner().stop(sid)
    return {"stopped": stopped}


@app.delete("/api/sessions/{sid}", dependencies=[Depends(require_token)])
async def delete_session(sid: str):
    await runner().stop(sid)
    db.delete_session(sid)
    return {"ok": True}


# --- PWA -------------------------------------------------------------------

@app.middleware("http")
async def no_stale_shell(request, call_next):
    """Make the browser revalidate the shell on every load.

    StaticFiles sends an ETag but no Cache-Control, so browsers fall back to
    heuristic freshness and can serve a stale styles.css/app.js for hours
    after a deploy. Revalidation is cheap: unchanged files answer 304.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.exception_handler(404)
async def not_found(request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(WEB_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "orchestrator.app:app",
        host=config.BIND_HOST,
        port=config.BIND_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
