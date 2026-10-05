import importlib.util
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import encrypted_backup as backup


def test_container_is_restricted(tmp_path):
    args = backup.command(tmp_path, ["check"], source=tmp_path / "source")
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert "ALL" in args
    assert "no-new-privileges" in args
    assert any("target=/source,readonly" in arg for arg in args)
    assert "--password-file" in args


def test_existing_vault_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        backup.initialize(tmp_path)


def test_symlink_source_rejected(tmp_path):
    (tmp_path / "database.dump").symlink_to("missing")
    with pytest.raises(ValueError):
        backup.fingerprints(tmp_path)


@pytest.mark.skipif(os.environ.get("RUN_RESTIC_INTEGRATION") != "1", reason="requires Docker Restic image")
def test_real_encryption_restore_wrong_password_and_corruption(tmp_path):
    vault = tmp_path / "vault"
    backup.initialize(vault)
    source = tmp_path / "source"
    source.mkdir()
    for name in backup.FILES:
        (source / name).write_bytes(os.urandom(8192))
    report = backup.encrypt_and_verify(vault, source)
    assert report["status"] == "verified"
    assert report["off_host"] is False
    assert (vault / "password").stat().st_mode & 0o077 == 0
    wrong = tmp_path / "wrong-password"
    wrong.write_text("not-the-password")
    with pytest.raises(RuntimeError, match="exit 12"):
        backup.restic(vault, ["snapshots"], password=wrong)
    pack = next(path for path in (vault / "repository/data").rglob("*") if path.is_file())
    # Restic makes packs read-only. Only the disposable corruption fixture changes.
    pack.chmod(0o600)
    with pack.open("r+b") as stream:
        stream.write(b"corrupt fixture pack")
    with pytest.raises(RuntimeError):
        backup.restic(vault, ["check", "--read-data"])


@pytest.mark.parametrize("platform,override,expected", [
    ("linux", None, ["docker"]),
    ("darwin", None, ["docker", "--context", "colima-frozen-stock"]),
    ("darwin", "", ["docker"]),
    ("linux", "custom", ["docker", "--context", "custom"]),
])
def test_context_selection(monkeypatch, platform, override, expected):
    monkeypatch.setattr(sys, "platform", platform)
    if override is None:
        monkeypatch.delenv("PAPER_DOCKER_CONTEXT", raising=False)
    else:
        monkeypatch.setenv("PAPER_DOCKER_CONTEXT", override)
    spec = importlib.util.spec_from_file_location("context_fixture", Path(__file__).with_name("verify_restore.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DOCKER == expected
