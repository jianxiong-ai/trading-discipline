from __future__ import annotations

import json
import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


_WORKSPACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,80}")
_DEFAULT_PASSWORD = "960818"
_PASSWORD_ITERATIONS = 210_000


class WorkspaceError(ValueError):
    """A user-facing error for an unknown or malformed workspace link."""


@dataclass(frozen=True)
class Workspace:
    id: str
    created_at: str
    is_default: bool = False
    password_hash: str = field(default="", repr=False, compare=False)
    access_token_hash: str = field(default="", repr=False, compare=False)
    # Only populated on the return value of create(); never written to disk.
    initial_password: str | None = field(default=None, repr=False, compare=False)


class WorkspaceRegistry:
    """A deliberately small, link-scoped workspace registry.

    This is not an account system.  The opaque ID in the URL identifies a
    workspace, while its password and browser access token control access.
    Each workspace receives its own SQLite ledger, state and review log.
    Keeping the registry separate from the ledgers means the old single-user
    database can remain intact as the initial default workspace.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.path = data_dir / "workspaces.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config_path(cls, config_path: str | Path) -> "WorkspaceRegistry":
        return cls(Path(config_path).resolve().parent / "data")

    def default(self) -> Workspace:
        data = self._load()
        existing = next((item for item in data["workspaces"] if item.get("is_default")), None)
        if existing:
            if not existing.get("password_hash"):
                existing["password_hash"] = _hash_password(_DEFAULT_PASSWORD)
                self._save(data)
            return self._workspace(existing)
        workspace = Workspace(
            id=self._new_id(),
            created_at=_now(),
            is_default=True,
            password_hash=_hash_password(_DEFAULT_PASSWORD),
        )
        data["workspaces"].append(self._dump_workspace(workspace))
        self._save(data)
        return workspace

    def list(self) -> list[Workspace]:
        self.default()  # Creates the first workspace on an existing install.
        return [self._workspace(item) for item in self._load()["workspaces"]]

    def get(self, workspace_id: str) -> Workspace:
        workspace_id = self._validate_id(workspace_id)
        for item in self._load()["workspaces"]:
            if item.get("id") == workspace_id:
                return self._workspace(item)
        raise WorkspaceError("工作区链接无效或已不存在")

    def create(self) -> Workspace:
        self.default()  # Ensure the registry has its one privileged workspace.
        data = self._load()
        initial_password = secrets.token_urlsafe(32)
        workspace = Workspace(
            id=self._new_id(),
            created_at=_now(),
            is_default=False,
            password_hash=_hash_password(initial_password),
            initial_password=initial_password,
        )
        data["workspaces"].append(self._dump_workspace(workspace))
        self._save(data)
        return workspace

    def verify_password(self, workspace: Workspace, password: str) -> bool:
        return _verify_password(password, workspace.password_hash)

    def issue_access_token(self, workspace: Workspace) -> str:
        token = secrets.token_urlsafe(32)
        data = self._load()
        for item in data["workspaces"]:
            if item.get("id") == workspace.id:
                item["access_token_hash"] = _hash_access_token(token)
                self._save(data)
                return token
        raise WorkspaceError("工作区链接无效或已不存在")

    def has_access(self, workspace: Workspace, token: str | None) -> bool:
        stored = workspace.access_token_hash
        if not stored or not token:
            return False
        return secrets.compare_digest(stored, _hash_access_token(token))

    def ledger_path(self, workspace: Workspace, configured_path: str | Path) -> Path:
        """Use the existing ledger for the migrated default workspace.

        New workspaces are physically separate rather than merely filtered by a
        URL parameter, which prevents accidental cross-user query mistakes.
        """
        configured = Path(configured_path)
        if workspace.is_default:
            return configured
        return self.data_dir / "workspaces" / workspace.id / "portfolio.db"

    def state_path(self, workspace: Workspace, configured_path: str | Path) -> Path:
        configured = Path(configured_path)
        if workspace.is_default:
            return configured
        return self.data_dir / "workspaces" / workspace.id / "state.json"

    def log_path(self, workspace: Workspace, configured_path: str | Path) -> Path:
        configured = Path(configured_path)
        if workspace.is_default:
            return configured
        return self.data_dir / "workspaces" / workspace.id / "events.jsonl"

    @staticmethod
    def _validate_id(value: str) -> str:
        value = str(value or "").strip()
        if not _WORKSPACE_ID_RE.fullmatch(value):
            raise WorkspaceError("工作区链接格式无效")
        return value

    def _new_id(self) -> str:
        # 192 bits of entropy; this is only a routing identifier, not a password.
        return secrets.token_urlsafe(24)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"workspaces": []}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("workspaces"), list):
                return value
        except (OSError, ValueError):
            pass
        raise WorkspaceError("工作区注册表无法读取，请检查 data/workspaces.json")

    def _save(self, value: dict[str, Any]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _dump_workspace(workspace: Workspace) -> dict[str, Any]:
        return {
            "id": workspace.id,
            "created_at": workspace.created_at,
            "is_default": workspace.is_default,
            "password_hash": workspace.password_hash,
            "access_token_hash": workspace.access_token_hash,
        }

    @classmethod
    def _workspace(cls, value: dict[str, Any]) -> Workspace:
        return Workspace(
            id=cls._validate_id(str(value.get("id", ""))),
            created_at=str(value.get("created_at") or _now()),
            is_default=bool(value.get("is_default", False)),
            password_hash=str(value.get("password_hash") or ""),
            access_token_hash=str(value.get("access_token_hash") or ""),
        )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PASSWORD_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        _PASSWORD_ITERATIONS, salt.hex(), digest.hex()
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (TypeError, ValueError):
        return False
    return secrets.compare_digest(actual, expected)


def _hash_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
