"""Which commit is actually running, answerable from the running process.

Before this, production could only be identified by matching a container image's
build timestamp against a CD run. That inference breaks the moment two deploys
land close together, a build is retried, or nobody wrote the timestamp down —
and it broke exactly when it was needed, during an incident.
"""
import asyncio

from core import config
from core.web import app as web_app


def test_a_release_reports_the_commit_it_was_built_from(tmp_path, monkeypatch):
    sha_file = tmp_path / ".deployed_sha"
    sha_file.write_text("caf065431f7a9c2b8d4e5f60718293a4b5c6d7e8\n", encoding="utf-8")
    monkeypatch.setattr(config, "_DEPLOYED_COMMIT_FILE", sha_file)

    assert config.deployed_commit() == "caf065431f7a"


def test_an_image_built_without_the_file_says_unknown(tmp_path, monkeypatch):
    """A local build and a hand-built image must not claim a commit.

    Guessing here would be worse than saying nothing: the whole point is that
    the answer can be trusted during an incident.
    """
    monkeypatch.setattr(config, "_DEPLOYED_COMMIT_FILE", tmp_path / "absent")

    assert config.deployed_commit() == "unknown"


def test_an_empty_file_says_unknown_rather_than_empty_string(tmp_path, monkeypatch):
    """CD writing a blank line is a failure to record, not a commit named ''."""
    sha_file = tmp_path / ".deployed_sha"
    sha_file.write_text("\n", encoding="utf-8")
    monkeypatch.setattr(config, "_DEPLOYED_COMMIT_FILE", sha_file)

    assert config.deployed_commit() == "unknown"


def test_health_carries_the_commit_alongside_the_version(tmp_path, monkeypatch):
    """`version` is a constant somebody edits; `commit` is what actually shipped."""
    sha_file = tmp_path / ".deployed_sha"
    sha_file.write_text("0123456789abcdef", encoding="utf-8")
    monkeypatch.setattr(config, "_DEPLOYED_COMMIT_FILE", sha_file)

    body = asyncio.run(web_app.health())

    assert body["commit"] == "0123456789ab"
    assert body["status"] == "ok"
    assert "version" in body, "the edited constant stays, it is just no longer alone"
