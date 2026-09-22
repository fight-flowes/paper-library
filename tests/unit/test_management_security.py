from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from paper_library.jobs.security import ManagementGuard, ManagementSecurityError


def test_guard_persists_private_token_and_validates_it(tmp_path: Path) -> None:
    first = ManagementGuard.from_state_directory(tmp_path, "127.0.0.1", False)
    second = ManagementGuard.from_state_directory(tmp_path, "127.0.0.1", False)

    assert first.token == second.token
    assert first.validate(first.token)
    assert not first.validate("wrong")
    assert stat.S_IMODE((tmp_path / "management.token").stat().st_mode) == 0o600


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_bind_enables_mutations(tmp_path: Path, host: str) -> None:
    assert ManagementGuard.from_state_directory(tmp_path, host, False).mutations_enabled


def test_remote_bind_requires_explicit_opt_in(tmp_path: Path) -> None:
    assert not ManagementGuard.from_state_directory(tmp_path, "0.0.0.0", False).mutations_enabled
    assert ManagementGuard.from_state_directory(tmp_path, "0.0.0.0", True).mutations_enabled


def test_guard_rejects_symlink_and_foreign_owned_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("token", encoding="utf-8")
    tmp_path.joinpath("management.token").symlink_to(target)

    with pytest.raises(ManagementSecurityError):
        ManagementGuard.from_state_directory(tmp_path, "127.0.0.1", False)

    tmp_path.joinpath("management.token").unlink()
    tmp_path.joinpath("management.token").write_text("token", encoding="utf-8")
    real_lstat = os.lstat

    def foreign_owner(path: object):
        result = real_lstat(path)
        values = list(result)
        values[4] = result.st_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "lstat", foreign_owner)
    with pytest.raises(ManagementSecurityError):
        ManagementGuard.from_state_directory(tmp_path, "127.0.0.1", False)
