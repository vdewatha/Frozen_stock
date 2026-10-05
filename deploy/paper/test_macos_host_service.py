from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import macos_host_service as service


def test_profile_is_specific_without_global_context_or_config_changes():
    configs = service.definitions(Path("/Users/Test User"), Path("/opt/homebrew/bin/colima"))
    startup = configs[service.PREFIX + ".startup"]
    assert startup["ProgramArguments"] == ["/opt/homebrew/bin/colima", "start", "--profile",
        "frozen-stock", "--activate=false", "--save-config=false"]
    assert startup["KeepAlive"] == {"SuccessfulExit": False}
    assert startup["ThrottleInterval"] == 60
    assert configs[service.PREFIX + ".ac-awake"]["ProgramArguments"] == ["/usr/bin/caffeinate", "-s"]
    for config in configs.values():
        assert plistlib.loads(plistlib.dumps(config)) == config
        assert set(config["EnvironmentVariables"]) == {"PATH", "HOME"}


def test_missing_profile_never_creates_vm(tmp_path):
    with patch.object(service, "run") as run:
        with pytest.raises(RuntimeError, match="Existing"):
            service.install(tmp_path, Path("/bin/colima"))
        run.assert_not_called()


def prepare(home):
    profile = home / ".colima/frozen-stock/colima.yaml"
    profile.parent.mkdir(parents=True)
    profile.touch()


def test_install_is_idempotent_and_private(tmp_path):
    prepare(tmp_path)
    loaded = set()
    calls = []
    def fake(args, check=True):
        calls.append(args)
        if args[1] == "bootstrap":
            loaded.add(Path(args[-1]).stem)
        return SimpleNamespace(returncode=int(args[1] == "print" and args[-1].split("/")[-1] not in loaded))
    with patch.object(service, "run", side_effect=fake):
        service.install(tmp_path, Path("/bin/colima"))
        service.install(tmp_path, Path("/bin/colima"))
    assert sum(c[1] == "bootstrap" for c in calls) == 2
    for path in (tmp_path / "Library/LaunchAgents").glob("*.plist"):
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("symlink", [False, True])
def test_conflicting_file_is_preserved(tmp_path, symlink):
    prepare(tmp_path)
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    path = agents / f"{service.PREFIX}.startup.plist"
    if symlink:
        path.symlink_to(tmp_path / "elsewhere")
    else:
        path.write_bytes(plistlib.dumps({"Label": "unrelated"}))
    with patch.object(service, "run") as run:
        with pytest.raises(RuntimeError, match="Existing service differs"):
            service.install(tmp_path, Path("/bin/colima"))
        run.assert_not_called()


def test_uninstall_does_not_stop_or_delete_vm(tmp_path):
    with patch.object(service, "run", return_value=SimpleNamespace(returncode=0)) as run:
        service.uninstall(tmp_path)
    assert all(c.args[0][0] == "/bin/launchctl" for c in run.call_args_list)
    assert sum(c.args[0][1] == "bootout" for c in run.call_args_list) == 2
