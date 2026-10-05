import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest

spec = importlib.util.spec_from_file_location("verify_restore", Path(__file__).with_name("verify_restore.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def archive(tmp_path, name="models/model.json", kind=tarfile.REGTYPE, duplicate=False):
    path = tmp_path / "models.tar"
    with tarfile.open(path, "w") as output:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = 2 if kind == tarfile.REGTYPE else 0
        info.linkname = "/tmp/untrusted" if kind == tarfile.SYMTYPE else ""
        output.addfile(info, io.BytesIO(b"{}"))
        if duplicate:
            output.addfile(info, io.BytesIO(b"{}"))
    return path


def test_artifact_restore_checks_actual_written_bytes(tmp_path):
    target = tmp_path / "restore"
    hashes = module.restore_artifacts(archive(tmp_path), target)
    assert hashes == {"models/model.json": hashlib.sha256(b"{}").hexdigest()}
    assert (target / "models/model.json").read_bytes() == b"{}"


@pytest.mark.parametrize("name,kind,duplicate", [
    ("../escape", tarfile.REGTYPE, False), ("/absolute", tarfile.REGTYPE, False),
    ("symlink", tarfile.SYMTYPE, False), ("duplicate", tarfile.REGTYPE, True),
])
def test_unsafe_or_duplicate_archive_rejected(tmp_path, name, kind, duplicate):
    with pytest.raises(ValueError):
        module.restore_artifacts(archive(tmp_path, name, kind, duplicate), tmp_path / "restore")
