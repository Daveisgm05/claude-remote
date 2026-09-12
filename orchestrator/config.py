"""Configuration: environment + tasks.yaml (workspace, projects, profiles)."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

BASE_DIR = Path(__file__).resolve().parent

# --- environment -----------------------------------------------------------
BIND_HOST = os.environ.get("CCR_BIND_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("CCR_BIND_PORT", "8080"))
TOKEN = os.environ.get("CCR_TOKEN", "")
DB_PATH = Path(os.environ.get("CCR_DB", BASE_DIR / "data" / "runs.sqlite3"))
TASKS_FILE = Path(os.environ.get("CCR_TASKS", BASE_DIR / "tasks.yaml"))
CLAUDE_BIN = os.environ.get("CCR_CLAUDE_BIN") or shutil.which("claude") or "claude"
MAX_CONCURRENT = int(os.environ.get("CCR_MAX_CONCURRENT", "2"))
TURN_TIMEOUT = int(os.environ.get("CCR_TURN_TIMEOUT", "3600"))  # seconds per turn
NTFY_URL = os.environ.get("CCR_NTFY_URL", "").rstrip("/")
NTFY_TOPIC = os.environ.get("CCR_NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("CCR_NTFY_TOKEN", "")
PUBLIC_URL = os.environ.get("CCR_PUBLIC_URL", "")

# Extra environment variables to hand to the Claude Code child process, by name.
# Nothing else from the orchestrator's own environment is passed through.
# Add ANTHROPIC_API_KEY here to meter against the API instead of your subscription.
PASS_ENV = [v.strip() for v in os.environ.get("CCR_PASS_ENV", "").split(",") if v.strip()]

# Folders never worth showing in the picker or descending into.
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".next", ".nuxt",
    "dist", "build", ".cache", ".pytest_cache", ".mypy_cache", "vendor",
    ".terraform", "target", ".gradle", ".idea", ".DS_Store",
}


@dataclass
class Profile:
    """A named set of Claude Code CLI flags."""

    name: str
    model: str = "sonnet"
    effort: Optional[str] = None          # low | medium | high | xhigh | max
    permission_mode: str = "dontAsk"
    allowed_tools: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    strict_mcp_config: bool = False
    restricted: bool = False
    settings: dict = field(default_factory=dict)
    disallowed_tools: list = field(default_factory=list)
    add_dirs: list = field(default_factory=list)
    max_budget_usd: Optional[float] = None
    append_system_prompt: Optional[str] = None
    fallback_model: Optional[str] = None


@dataclass
class Project:
    """A folder Claude Code runs in."""

    key: str
    name: str
    path: str
    profile: str = "default"
    branch_per_session: bool = False
    worktree_root: Optional[str] = None
    pinned: bool = False

    @property
    def exists(self) -> bool:
        return Path(self.path).is_dir()


@dataclass
class Workspace:
    """The folder tree the phone may browse, create in, and run sessions in.

    This is what replaces a hardcoded project list. Any directory under `root`
    can be opened or created from the phone without editing a config file;
    anything outside it -- /etc, another service's .env, the rest of the disk --
    resolves out and is refused. One boundary instead of an inventory.
    """

    root: Path
    default_profile: str = "default"

    def resolve(self, raw: str) -> Path:
        """Turn a path sent by the phone into a real directory inside the root.

        Symlinks are resolved *before* the containment check, so a link planted
        inside the workspace cannot be used to step out of it.
        """
        text = str(raw or "").strip()
        p = Path(os.path.expandvars(os.path.expanduser(text or ".")))
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        root = self.root.resolve()
        if p != root and root not in p.parents:
            raise ValueError(f"'{raw}' is outside the workspace ({root})")
        return p

    def label(self, p: Path) -> str:
        """Path as shown on the phone: relative to the root, or '~' for it."""
        root = self.root.resolve()
        try:
            rel = p.resolve().relative_to(root)
        except ValueError:
            return str(p)
        return str(rel) if str(rel) != "." else "/"

    def project_for(self, p: Path, profile: str) -> Project:
        """An ad-hoc Project for a folder that isn't pinned in tasks.yaml."""
        return Project(key=str(p), name=p.name or self.label(p), path=str(p), profile=profile)


@dataclass
class Tasks:
    projects: dict
    profiles: dict
    workspace: Workspace

    def profile_for(self, project: Project, override: Optional[str] = None) -> Profile:
        name = override or project.profile
        if name not in self.profiles:
            raise KeyError(f"unknown profile: {name}")
        return self.profiles[name]


def _expand(p: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(p))))


def load_tasks(path: Optional[Path] = None) -> Tasks:
    path = path or TASKS_FILE
    raw = yaml.safe_load(path.read_text()) or {}

    profiles: dict = {}
    for name, body in (raw.get("profiles") or {}).items():
        body = body or {}
        profiles[name] = Profile(
            name=name,
            model=str(body.get("model", "sonnet")),
            effort=(str(body["effort"]) if body.get("effort") else None),
            permission_mode=str(body.get("permission_mode", "dontAsk")),
            allowed_tools=[str(t) for t in (body.get("allowed_tools") or [])],
            tools=[str(t) for t in (body.get("tools") or [])],
            strict_mcp_config=bool(body.get("strict_mcp_config", False)),
            restricted=bool(body.get("restricted", False)),
            settings=dict(body.get("settings") or {}),
            disallowed_tools=[str(t) for t in (body.get("disallowed_tools") or [])],
            add_dirs=[_expand(str(d)) for d in (body.get("add_dirs") or [])],
            max_budget_usd=(
                float(body["max_budget_usd"]) if body.get("max_budget_usd") is not None else None
            ),
            append_system_prompt=body.get("append_system_prompt"),
            fallback_model=body.get("fallback_model"),
        )
    if "default" not in profiles:
        profiles["default"] = Profile(name="default")

    ws_raw = raw.get("workspace") or {}
    workspace = Workspace(
        root=Path(_expand(str(ws_raw.get("root", "~/projects")))),
        default_profile=str(ws_raw.get("default_profile", "default")),
    )
    if workspace.default_profile not in profiles:
        workspace.default_profile = "default"
    workspace.root.mkdir(parents=True, exist_ok=True)

    # Optional shortcuts: folders that get a friendly name and their own profile
    # at the top of the picker. Everything else under the root still works
    # without appearing here.
    projects: dict = {}
    for key, body in (raw.get("projects") or {}).items():
        body = body or {}
        projects[key] = Project(
            key=key,
            name=str(body.get("name", key)),
            path=_expand(str(body["path"])),
            profile=str(body.get("profile", "default")),
            branch_per_session=bool(body.get("branch_per_session", False)),
            worktree_root=_expand(str(body["worktree_root"])) if body.get("worktree_root") else None,
            pinned=True,
        )

    return Tasks(projects=projects, profiles=profiles, workspace=workspace)
