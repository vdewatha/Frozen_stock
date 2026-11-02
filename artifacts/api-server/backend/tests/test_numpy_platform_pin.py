from pathlib import Path

import pytest
from packaging.requirements import Requirement


@pytest.mark.parametrize("file", ["requirements.txt", "requirements-research-test.txt"])
@pytest.mark.parametrize("system,machine,version", [
    ("linux", "x86_64", "2.0.1"), ("linux", "aarch64", "2.0.1"),
    ("darwin", "arm64", "2.3.3"), ("darwin", "x86_64", "2.0.1"),
    ("win32", "AMD64", "2.0.1"),
])
def test_numpy_fix_is_scoped_to_apple_silicon(file, system, machine, version):
    requirements = [Requirement(line) for line in (Path(__file__).parents[1] / file).read_text().splitlines()
                    if line.startswith("numpy==")]
    resolved = [item for item in requirements
                if item.marker.evaluate({"sys_platform": system, "platform_machine": machine})]
    assert len(resolved) == 1
    assert str(resolved[0].specifier) == f"=={version}"
