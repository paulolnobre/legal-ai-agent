"""One bug shape, in every shell script that could carry it.

Under `set -o pipefail`, a pipeline that ends in a consumer which exits early
reports the writer's SIGPIPE as the pipeline's status. `rclone lsf | grep -q`
returns 141 whenever grep finds the match before rclone finishes writing, which
means the pipeline fails precisely when it succeeds.

It cost a night of backups on 2026-09-09: the copy reached B2 intact and the
verification declared it missing. Measured on the VPS afterwards, the same
pipeline returned 141 on two runs out of five, which is why it had passed every
night before. The same shape sat in the smoke test, where it was worse: a
traceback found in the log made the condition read as no traceback found.

Capturing the output first and searching it afterwards costs nothing and cannot
race, so that is the rule this asserts.
"""
import re
from pathlib import Path

import pytest

SCRIPTS = sorted((Path(__file__).resolve().parents[1] / "scripts").glob("*.sh"))

# Consumers that stop reading before their input ends. `tail`, `sed`, `awk` and
# `wc` all read to EOF, so they never send SIGPIPE upstream and are fine.
EARLY_EXIT = re.compile(r"\|\s*(grep\s+(-\w*[qm]|--quiet|--max-count)|head\b)")


def _statements(script: Path) -> list[tuple[int, str]]:
    """Numbered lines that are not comments."""
    return [
        (number, line)
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_pipeline_can_report_its_own_success_as_a_failure(script: Path):
    text = script.read_text(encoding="utf-8")
    if "pipefail" not in text:
        return

    offenders = [
        f"{script.name}:{number}: {line.strip()}"
        for number, line in _statements(script)
        if EARLY_EXIT.search(line)
    ]

    assert not offenders, (
        "under pipefail these pipelines return 141 when the consumer exits "
        "first, turning a success into a failure (or, in a condition, a "
        "failure into a success):\n  " + "\n  ".join(offenders) + "\n"
        "Capture the output into a variable and search it with a here-string."
    )


def test_the_guard_would_catch_the_bug_it_was_written_for():
    """The rule is only worth having if it recognises the original line."""
    assert EARLY_EXIT.search('rclone lsf "$REMOTE/$stamp" | grep -q postgres.dump')
    assert EARLY_EXIT.search('docker ps --quiet --filter x | head -n1')
    assert EARLY_EXIT.search('compose logs bot 2>&1 | grep -q "Traceback"')
    # And that it leaves alone the consumers that read to the end.
    assert not EARLY_EXIT.search('rclone size --json "$path" | sed -n "s/x/y/p"')
    assert not EARLY_EXIT.search('docker logs bot | tail -n 40')
    assert not EARLY_EXIT.search('grep -q pattern <<<"$captured"')
