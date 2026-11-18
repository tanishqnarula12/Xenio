"""Who XENIO's commits are from: yours with XENIO as co-author, like Claude Code, or XENIO's when git does not know you."""

import subprocess

import pytest

from app.engineering import identity


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in ("XENIO_GIT_NAME", "XENIO_GIT_EMAIL", "XENIO_COMMIT_AS_XENIO"):
        monkeypatch.delenv(key, raising=False)
    identity._global.cache_clear()
    yield
    getattr(identity._global, "cache_clear", lambda: None)()  # a test may have swapped it for a fake


def known(monkeypatch, name="Asha Rao", email="asha@example.com"):
    monkeypatch.setattr(identity, "_global", lambda key: {"user.name": name, "user.email": email}.get(key, ""))


def test_a_commit_is_yours_and_names_xenio_as_co_author(monkeypatch):
    known(monkeypatch)
    assert identity.config() == ["-c", "user.name=Asha Rao", "-c", "user.email=asha@example.com"]
    assert identity.message("Add streaks") == "Add streaks\n\nCo-Authored-By: XENIO <358429157+xenio101@users.noreply.github.com>"
    assert identity.message(identity.message("Add streaks")).count("Co-Authored-By") == 1  # never twice


def test_another_address_can_be_set(monkeypatch):
    known(monkeypatch)
    monkeypatch.setenv("XENIO_GIT_EMAIL", "bot@example.com")
    assert identity.message("x").endswith("Co-Authored-By: XENIO <bot@example.com>")
    monkeypatch.setenv("XENIO_GIT_EMAIL", "not an email\nCo-Authored-By: someone")  # nothing can be smuggled into the message
    assert identity.message("x").endswith("Co-Authored-By: XENIO <358429157+xenio101@users.noreply.github.com>")


def test_without_your_git_identity_xenio_is_the_author_and_there_is_no_co_author_line(monkeypatch):
    known(monkeypatch, "", "")
    assert identity.config(("XENIO engineering", identity.ACCOUNT)) == ["-c", "user.name=XENIO engineering", "-c", f"user.email={identity.ACCOUNT}"]
    assert identity.config() == ["-c", "user.name=XENIO", "-c", f"user.email={identity.ACCOUNT}"]  # still on XENIO's GitHub account
    assert identity.message("x") == "x"


def test_you_can_keep_xenio_as_the_author(monkeypatch):
    known(monkeypatch)
    monkeypatch.setenv("XENIO_COMMIT_AS_XENIO", "true")
    assert identity.author() == ("XENIO", identity.ACCOUNT) and identity.message("x") == "x"


def test_a_real_commit_carries_the_line(monkeypatch, tmp_path):
    known(monkeypatch)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *identity.config(), "commit", "-q", "-m", identity.message("First")], cwd=tmp_path, check=True)
    out = subprocess.run(["git", "log", "-1", "--format=%an <%ae>%n%(trailers:key=Co-Authored-By)"], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert out.splitlines()[0] == "Asha Rao <asha@example.com>" and "Co-Authored-By: XENIO <358429157+xenio101@users.noreply.github.com>" in out
