"""How XENIO finds a project's tests."""

from app.engineering import verify


def test_tests_kept_beside_the_code_are_found_as_pytest_finds_them(tmp_path):
    """A real backend kept its tests in app/test_main.py, and XENIO never ran them."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "test_main.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "test_theirs.py").write_text("")
    assert [p.name for p in verify.python_tests(tmp_path)] == ["test_main.py"]
    assert any(c.name == "pytest" for c in verify.detect(tmp_path))


def test_without_the_sandbox_code_xenio_did_not_write_is_blocked_not_run(tmp_path, monkeypatch):
    """Fail closed: it once ran agent-written tests unprotected and only said so in a label."""
    import os.path as osp

    (tmp_path / "test_x.py").write_text("open('ran', 'w').write('1')\ndef test_ok():\n    pass\n")
    real = osp.exists
    monkeypatch.setattr(verify.os.path, "exists", lambda p: False if p == "/usr/bin/sandbox-exec" else real(p))
    result = verify.run(verify.detect(tmp_path)[0], tmp_path, timeout=30, sandbox=True)
    assert result.status == "blocked" and "no sandbox" in result.detail and not (tmp_path / "ran").exists()


def test_agent_written_tests_cannot_reach_the_network_or_your_home_folder(tmp_path):
    import shutil
    import sys

    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        import pytest

        pytest.skip("needs macOS sandbox-exec")
    (tmp_path / "test_escape.py").write_text(
        "import os, socket, pytest\n"
        "def test_home():\n    with pytest.raises(PermissionError):\n        os.listdir(os.path.expanduser('~/Library'))\n"
        "def test_ssh_keys():\n    with pytest.raises((PermissionError, FileNotFoundError)):\n        open(os.path.expanduser('~/.ssh/id_ed25519')).read()\n"
        "def test_network():\n    with pytest.raises(OSError):\n        socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "def test_secrets_not_inherited():\n    assert not [k for k in os.environ if 'KEY' in k or 'TOKEN' in k or 'SECRET' in k]\n")
    result = verify.run(verify.detect(tmp_path)[0], tmp_path, timeout=120, sandbox=True)
    assert result.status == "passed" and result.sandboxed, (result.status, result.detail, result.tail[-600:])
