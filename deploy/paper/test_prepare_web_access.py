import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("prepare_web_access", Path(__file__).with_name("prepare_web_access.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_only_viewer_key_is_exported_with_private_permissions(tmp_path, monkeypatch):
    root = tmp_path
    (root / "deploy/paper").mkdir(parents=True)
    (root / "deploy/paper/paper.env").write_text("AUTH_VIEWER_KEY=" + "a" * 64 + "\n")
    (root / ".env").write_text("AUTH_VIEWER_KEY=" + "b" * 64 + "\nAUTH_ADMIN_KEY=not-exported\nALPACA_API_SECRET=not-exported\n")
    monkeypatch.setattr(module, "ROOT", root)
    module.main()
    result = root / "deploy/paper/web.env"
    assert result.read_text() == "LOCAL_VIEWER_KEY=" + "b" * 64 + "\n"
    assert result.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("value", ["", "short", "x" * 32 + ";injection"])
def test_invalid_key_is_not_provisioned(tmp_path, monkeypatch, value):
    (tmp_path / "deploy/paper").mkdir(parents=True)
    (tmp_path / ".env").write_text("AUTH_VIEWER_KEY=" + value + "\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(ValueError):
        module.main()
    assert not (tmp_path / "deploy/paper/web.env").exists()
