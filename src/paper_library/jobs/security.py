"""Persistent management-token and bind-address policy."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import secrets
import stat


class ManagementSecurityError(RuntimeError):
    """Raised when the management token cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ManagementGuard:
    token: str
    mutations_enabled: bool

    @classmethod
    def from_state_directory(
        cls,
        path: Path,
        host: str,
        remote_enabled: bool,
    ) -> "ManagementGuard":
        state = path.expanduser().resolve()
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_path = state / "management.token"
        token = cls._read_or_create_token(token_path)
        return cls(
            token=token,
            mutations_enabled=_is_loopback(host) or remote_enabled,
        )

    def validate(self, submitted: str | None) -> bool:
        return bool(submitted) and secrets.compare_digest(self.token, submitted)

    @staticmethod
    def _read_or_create_token(path: Path) -> str:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            return ManagementGuard._read_existing(path)
        token = secrets.token_urlsafe(32)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            path.unlink(missing_ok=True)
            raise
        return token

    @staticmethod
    def _read_existing(path: Path) -> str:
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise ManagementSecurityError("无法读取管理令牌") from exc
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ManagementSecurityError("管理令牌文件不安全")
        try:
            os.chmod(path, 0o600)
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                token = handle.read(256).strip()
        except OSError as exc:
            raise ManagementSecurityError("无法读取管理令牌") from exc
        if not token:
            raise ManagementSecurityError("管理令牌文件为空")
        return token


def _is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").casefold()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
