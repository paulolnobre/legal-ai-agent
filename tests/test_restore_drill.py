"""The seams of the host jobs that CI can check without Docker.

The drill itself restores into a real Postgres and was exercised against real
backups, a real Alertmanager and every failure it names. What is left for this
file is where it meets the rest of the project: the playbook that schedules it,
the storage format it trusts, and the README that tells somebody when it runs.
"""
import os
import re
import subprocess
from pathlib import Path

import yaml

from core.services.document_storage import storage_key

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DRILL = ROOT / "scripts" / "restore_drill.sh"


def _cron_tasks() -> dict[str, dict]:
    playbook = yaml.safe_load((ROOT / "infra/ansible/provision.yml").read_text())
    tasks = playbook[0]["tasks"]
    return {
        task["ansible.builtin.cron"]["name"]: task["ansible.builtin.cron"]
        for task in tasks
        if "ansible.builtin.cron" in task
    }


def test_every_scheduled_script_exists_and_is_executable():
    # A cron line naming a script that was renamed fails at 04:10 on a Sunday,
    # into a log file, which is how #76's renames could have broken the backup.
    scripts = [
        re.findall(r"/(scripts/[\w.]+)", cron["job"])[0]
        for cron in _cron_tasks().values()
        if "{{ deploy_root }}" in cron["job"]
    ]
    assert "scripts/backup.sh" in scripts
    assert "scripts/restore_drill.sh" in scripts
    for script in scripts:
        assert os.access(ROOT / script, os.X_OK), f"{script} is not executable"


def test_the_drill_runs_after_the_backup_it_reads():
    # The drill fails a copy older than 36 hours, so it must run after that
    # night's backup has landed, not before it.
    crons = _cron_tasks()
    backup = crons["demo-firm-agent backup"]
    drill = crons["demo-firm-agent restore drill"]
    assert (int(drill["hour"]), int(drill["minute"])) > (
        int(backup["hour"]), int(backup["minute"]),
    )


def test_the_readme_states_the_schedule_the_playbook_installs():
    drill = _cron_tasks()["demo-firm-agent restore drill"]
    days = {"0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
            "4": "Thursday", "5": "Friday", "6": "Saturday"}
    stated = f"every {days[drill['weekday']]} at {int(drill['hour']):02d}:{int(drill['minute']):02d}"
    assert stated in (ROOT / "README.md").read_text()


def _drill_key_pattern() -> str:
    match = re.search(r'\[\[ "\$key" =~ (\S+) \]\]', DRILL.read_text())
    assert match, "the drill no longer validates storage keys"
    return match.group(1)


def _bash_accepts(pattern: str, key: str) -> bool:
    # Bash's own =~, not Python's re: the two dialects differ, and the one that
    # decides in production is bash.
    result = subprocess.run(
        ["bash", "-c", f'[[ "$1" =~ {pattern} ]]', "_", key], check=False,
    )
    return result.returncode == 0


def test_the_drill_accepts_every_key_storage_produces():
    # If storage_key() changes shape, every drill after it dies on the first
    # document with "unexpected storage key". This is where that is caught.
    pattern = _drill_key_pattern()
    digest = "ab" * 32
    for filename in ("contrato.PDF", "peticao.docx", "lei.html", "sem_extensao",
                     "estranho.ext!", "../../etc/passwd"):
        key = storage_key(digest, filename)
        assert _bash_accepts(pattern, key), key


def test_the_drill_refuses_a_key_that_would_leave_its_directory():
    pattern = _drill_key_pattern()
    digest = "ab" * 32
    for key in (f"../ab/{digest}", f"ab/cd/../../{digest}", f"/ab/cd/{digest}",
                f"ab/cd/{digest}.pdf/../../x", f"ab/cd/{digest[:-1]}"):
        assert not _bash_accepts(pattern, key), key


def test_the_drill_accepts_every_key_the_bucket_produces():
    # Originals in the bucket are checked by the same guard before rclone is
    # asked about them, so bucket_key() has to stay inside it too.
    from core.services.document_storage import bucket_key

    pattern = _drill_key_pattern()
    for filename in ("contrato.PDF", "peticao.docx", "sem_extensao", "../../etc/passwd"):
        key = bucket_key(filename)
        assert _bash_accepts(pattern, key), key


def test_an_original_in_the_bucket_is_looked_for_in_the_backup_of_it():
    """A bucket original is ciphertext outside documents.tar.gz by design, so
    the drill must look elsewhere. Where matters: in the backup's copy, never
    in the live bucket.

    A drill that reads production proves the production object exists, which
    is not a recovery property. It would pass on the exact night the bucket
    was lost, which is the night the drill exists for.
    """
    text = DRILL.read_text()

    assert "source_wrapped_key IS NULL" in text, "the drill must tell the two apart"
    assert 'objects_backup="$BACKUP_REMOTE/objects"' in text
    assert "MGC_BUCKET" not in text, (
        "the drill reads the backup, not the live bucket; naming the "
        "operational bucket here is how it drifts back to checking production"
    )


def test_the_backup_writes_the_bucket_copy_where_the_drill_reads_it():
    """The two scripts have to agree on one path and nothing else enforces it.
    If the backup writes elsewhere, the drill fails every week, and the
    originals are unprotected for as long as somebody takes to notice.

    The destination is read out of the `rclone copy` itself rather than
    searched for anywhere in the file: an earlier version of this asserted the
    path appeared somewhere in backup.sh, and passed while the copy went to a
    different directory, because a later `rclone size` line still mentioned the
    right one.
    """
    backup = (SCRIPTS / "backup.sh").read_text()
    drill = DRILL.read_text()

    # Scoped to the bucket block: the off-site copy of the dump is also an
    # `rclone copy`, and matching that one instead compares the wrong paths.
    block = backup.split("Originals that live in the object store", 1)[1]
    block = block.split("Retention", 1)[0]

    copy = re.search(r'rclone copy "\$[A-Za-z_]+" "([^"]+)"', block)
    assert copy, "backup.sh no longer copies the bucket with rclone copy"

    reads = re.search(r'objects_backup="([^"]+)"', drill)
    assert reads, "the drill no longer names where it reads bucket originals"

    assert copy.group(1) == reads.group(1), (
        f"backup.sh writes bucket originals to {copy.group(1)} but the drill "
        f"looks in {reads.group(1)}"
    )


def test_the_object_store_remote_is_not_derived_from_the_backup_remote():
    """They are different providers on purpose, and that is the whole point of
    the backup. Building one name out of the other recreates the coupling this
    step exists to break.

    This is a regression test, not a precaution. The first version built
    `${BACKUP_REMOTE%%:*}:$MGC_BUCKET` and died on the first real run,
    because the object store is on one provider and the backup on another.
    """
    backup = (SCRIPTS / "backup.sh").read_text()
    block = backup.split("Originals that live in the object store", 1)[1]
    block = block.split("Retention", 1)[0]

    assert "MGC_REMOTE" in block, "the source needs its own remote"
    assert "${BACKUP_REMOTE%%:*}" not in block, (
        "the object store remote must not be built out of the backup remote"
    )
    # Two independent variables can disagree, and this one fails quietly: the
    # backup would faithfully copy a bucket nobody is filling and report the
    # count of an empty directory as success.
    assert '"${MGC_REMOTE#*:}" == "$MGC_BUCKET"' in block, (
        "the remote must be checked against the bucket the application writes to"
    )


def test_the_bucket_copy_is_counted_rather_than_trusted():
    """`rclone copy` from an empty or absent bucket exits zero with no files,
    so a successful exit says nothing. Only the loud failure of a
    bucket-scoped key turned that into an error the first time; on a
    permissive key the backup would have reported success while copying
    nothing.
    """
    backup = (SCRIPTS / "backup.sh").read_text()
    block = backup.split("Originals that live in the object store", 1)[1]
    block = block.split("Retention", 1)[0]

    assert "in_backup < in_store" in block, (
        "the backup must compare what it copied against what is in the store"
    )


def test_the_bucket_copy_is_additive_and_never_mirrors_deletions():
    """`rclone sync` would delete from the backup whatever production deleted,
    which turns the backup into a second copy of the same mistake. The names
    are random per write, so nothing is ever overwritten and copy is enough."""
    backup = (SCRIPTS / "backup.sh").read_text()
    block = backup.split("Originals that live in the object store", 1)[1]
    block = block.split("Retention", 1)[0]

    assert "rclone copy" in block
    assert "rclone sync" not in block


def test_a_failed_bucket_copy_fails_the_backup():
    """A warning here would let the backup report success while the bucket
    originals still have exactly one copy, which is the state this whole step
    exists to end. The night it matters is the night nobody read the log."""
    backup = (SCRIPTS / "backup.sh").read_text()
    block = backup.split("Originals that live in the object store", 1)[1]
    block = block.split("Retention", 1)[0]

    assert "|| die" in block, "a failed bucket copy must stop the backup"
    assert "WARNING: bucket copy failed" not in block


def test_the_shared_objects_directory_is_not_mistaken_for_a_backup_stamp():
    """The off-site prune lists directories and parses their names as
    timestamps. `objects/` is not one, and it must survive the sweep: losing
    it would delete every bucket original's only backup, silently."""
    backup = (SCRIPTS / "backup.sh").read_text()
    prune = backup.split("Pruning off-site backups", 1)[1]

    assert "date -u -d" in prune and "|| continue" in prune, (
        "the prune must skip a directory whose name is not a timestamp"
    )
