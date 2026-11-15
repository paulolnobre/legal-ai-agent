"""Checks for safe preparation of the production environment."""
import os
import subprocess
from pathlib import Path

import pytest

import yaml

from clients.demo_firm.prepare_production_env import (
    build_environment,
    write_environment,
)


def test_deploy_env_inherits_only_service_credentials(tmp_path: Path):
    source = tmp_path / "old.env"
    source.write_text(
        "LEGAL_TELEGRAM_BOT_TOKEN=telegram-token\n"
        "OPENAI_API_KEY=openai-key\n"
        "GEMINI_API_KEY=gemini-key\n"
        "DB_PASSWORD=old-database-password\n",
        encoding="utf-8",
    )

    values = build_environment(source, "legal-agent.example.com")

    assert values["LEGAL_TELEGRAM_BOT_TOKEN"] == "telegram-token"
    assert values["GEMINI_API_KEY"] == "gemini-key"
    assert values["DB_PASSWORD"] != "old-database-password"
    assert values["PUBLIC_BASE_URL"] == "https://legal-agent.example.com"
    assert values["LANGSMITH_TRACING"] == "false"


def test_deploy_env_is_written_private(tmp_path: Path):
    destination = tmp_path / ".env"
    write_environment(destination, {"KEY": "value"}, force=False)
    assert destination.read_text(encoding="utf-8") == "KEY=value\n"
    assert destination.stat().st_mode & 0o777 == 0o600


_WORKFLOW = ".github/workflows/ci.yml"


def _yaml(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _deploy_job() -> dict:
    return _yaml(_WORKFLOW)["jobs"]["deploy"]


def test_cd_uses_a_root_owned_runtime_boundary():
    """The SSH account may update source but not Docker privileges or secrets."""
    workflow = Path(_WORKFLOW).read_text(encoding="utf-8")
    root_deploy = Path("scripts/deploy_vps_root.sh").read_text(encoding="utf-8")
    runtime = Path("infra/vps/runtime-compose.yml").read_text(encoding="utf-8")

    assert "sudo /usr/local/sbin/demo-firm-deploy" in workflow
    assert "/etc/demo-firm-agent/runtime-compose.yml" in root_deploy
    assert '-f "$runtime_config"' in root_deploy
    assert "runtime_owner" in root_deploy
    assert "root and not writable by the deployment account" in runtime


def test_one_workflow_carries_the_whole_pipeline():
    """A second, chained delivery workflow is how #108 merged and was never
    deployed: it saw a cancelled run and skipped without a word."""
    workflows = sorted(path.name for path in Path(".github/workflows").glob("*.yml"))
    # YAML 1.1 reads the bare key `on` as True.
    triggers = _yaml(_WORKFLOW)[True]

    assert workflows == ["ci.yml", "test-features.yml"]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    # A merge is a push to main: without this filter nothing deploys, and no
    # job fails to say so.
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {"branches": ["main"]}


def test_deploy_runs_only_after_both_checks_and_only_from_main():
    """The exact guard, not fragments of it: `push || main` would read as
    containing the right words and deploy a pull request."""
    job = _deploy_job()

    assert job["needs"] == ["test", "image"]
    assert " ".join(job["if"].split()) == (
        "github.ref == 'refs/heads/main' && "
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
    )
    # A status function such as always() in the guard would drop the implicit
    # requirement that `needs` succeeded.
    assert "always()" not in job["if"] and "failure()" not in job["if"]


def test_deploy_ships_the_commit_that_was_tested():
    steps = _deploy_job()["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith(
        "actions/checkout"))
    record = next(step for step in steps if step.get("name", "").startswith("Record"))

    assert "ref" not in checkout.get("with", {}), "the default ref is the tested commit"
    assert record["env"]["RELEASE_SHA"] == "${{ github.sha }}"


def test_the_checks_test_the_commit_and_cannot_be_waved_through():
    """Deploy trusts `needs`: a check that tested another commit, or that
    succeeds while a step fails, would let a broken commit through."""
    jobs = _yaml(_WORKFLOW)["jobs"]
    for name in ("test", "image"):
        job = jobs[name]
        assert "continue-on-error" not in job, name
        for step in job["steps"]:
            assert "continue-on-error" not in step, (name, step)
            if step.get("uses", "").startswith("actions/checkout"):
                assert "ref" not in step.get("with", {}), name


def test_the_checks_deploy_waits_for_actually_run():
    """`needs` passing means nothing if the job it names checks nothing."""
    jobs = _yaml(_WORKFLOW)["jobs"]
    runs = {step.get("name"): step.get("run", "") for step in jobs["test"]["steps"]}

    assert runs["Lint"].strip() == "ruff check ."
    assert runs["Type check"].strip() == "pyright"
    assert runs["Test"].strip() == "pytest"
    assert "REQUIRE_TEST_DATABASE" in next(
        step for step in jobs["test"]["steps"] if step.get("name") == "Test")["env"]
    build = next(step for step in jobs["image"]["steps"]
                 if step.get("uses", "").startswith("docker/build-push-action"))
    assert build["with"]["context"] == "."


def test_deploys_never_overlap_are_never_cut_off_and_none_is_dropped():
    """The default queue keeps one waiting deploy and cancels the rest."""
    assert _deploy_job()["concurrency"] == {
        "group": "demo-firm-production", "cancel-in-progress": False, "queue": "max",
    }


def test_no_run_on_main_is_cancelled_or_displaced_by_another():
    """Only a pull request's stale run is cancelled (#108). Every other run
    gets its own group, so none waits behind or replaces another."""
    assert _yaml(_WORKFLOW)["concurrency"] == {
        "group": "ci-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


def _fresh_step() -> dict:
    return next(step for step in _deploy_job()["steps"] if step.get("id") == "fresh")


def test_every_step_that_changes_production_waits_for_the_freshness_check():
    steps = _deploy_job()["steps"]
    after = steps[steps.index(_fresh_step()) + 1:]

    assert [step["name"] for step in after] == [
        "Mark the release as under way",
        "Record this release and where it is deployed from",
        "Synchronize release files",
        "Build, restart, and verify",
        "Record the release production now runs",
    ]
    assert all(step.get("if") == "steps.fresh.outputs.deploy == 'true'" for step in after)


def test_the_deploy_has_the_history_to_compare_commits():
    checkout = next(step for step in _deploy_job()["steps"]
                    if step.get("uses", "").startswith("actions/checkout"))

    assert checkout["with"] == {"fetch-depth": 0}


def test_the_release_files_never_overwrite_the_release_marker():
    sync = next(step for step in _deploy_job()["steps"]
                if step.get("name") == "Synchronize release files")

    assert "--exclude='.released_sha*'" in sync["run"]
    assert "--exclude='.releasing_sha*'" in sync["run"]


# No user's git configuration: a CI runner has no identity, and a test that
# passes only on a machine with one is a test of that machine.
_GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=_GIT_ENV).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


# The runner's own invocation for `run:` steps on Linux.
_RUNNER_BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c"]

# Stands in for ssh: runs the remote command here, with the VPS directory
# mapped to a temporary one, or fails the way ssh does when it cannot connect.
_SSH = """#!/bin/sh
[ "$SSH_MODE" = fail ] && exit 255
shift
command=$(printf '%s' "$*" | sed "s#/opt/demo-firm-agent-v2#$VPS_ROOT#g")
exec sh -c "$command"
"""


@pytest.fixture
def pipeline(tmp_path):
    """main on an origin with an older commit; the runner's clone made before
    a newer commit landed, as when production is already ahead of a slow run."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    older = _commit(origin, "older")
    runner = tmp_path / "runner"
    subprocess.run(["git", "clone", "-q", str(origin), str(runner)], check=True, env=_GIT_ENV)
    newer = _commit(origin, "newer")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(_SSH)
    (bin_dir / "ssh").chmod(0o755)
    vps = tmp_path / "vps"
    vps.mkdir()

    def step(name: str, tested: str, *, ssh: str = "ok"):
        output = tmp_path / "output"
        output.write_text("")
        env = {**_GIT_ENV, "PATH": f"{bin_dir}:{os.environ['PATH']}",
               "VPS_ROOT": str(vps), "SSH_MODE": ssh, "RETRY_SECONDS": "0",
               "GITHUB_SHA": tested, "GITHUB_OUTPUT": str(output),
               "VPS_HOST": "h", "VPS_USER": "u"}
        script = next(s for s in _deploy_job()["steps"] if s.get("name") == name)["run"]
        run = subprocess.run([*_RUNNER_BASH, script], cwd=runner, env=env,
                             capture_output=True, text=True)
        return run.returncode, output.read_text().strip(), run.stdout + run.stderr

    def decide(tested: str, *, ssh: str = "ok"):
        return step("Deploy only what is newer than production", tested, ssh=ssh)

    def marker(name: str):
        def put(sha: str | None):
            path = vps / name
            if sha is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(sha + "\n")
        return put

    return {"older": older, "newer": newer, "decide": decide,
            "live": marker(".released_sha"), "under_way": marker(".releasing_sha"),
            "step": step, "vps": vps, "runner": runner}


def test_a_newer_commit_deploys_over_an_older_one(pipeline):
    _git(pipeline["runner"], "fetch", "-q", "origin")  # the checkout of `newer`
    pipeline["live"](pipeline["older"])

    assert pipeline["decide"](pipeline["newer"])[:2] == (0, "deploy=true")


def test_an_older_commit_never_overwrites_a_newer_one_already_live(pipeline):
    """The runner has never seen the newer commit: the step has to fetch it."""
    pipeline["live"](pipeline["newer"])

    code, decision, said = pipeline["decide"](pipeline["older"])

    assert (code, decision) == (0, "deploy=false")
    assert "Nothing to deploy" in said


def test_the_same_commit_redeploys(pipeline):
    pipeline["live"](pipeline["older"])

    assert pipeline["decide"](pipeline["older"])[:2] == (0, "deploy=true")


def test_the_first_release_deploys(pipeline):
    pipeline["live"](None)

    assert pipeline["decide"](pipeline["older"])[:2] == (0, "deploy=true")


def test_an_unreachable_vps_fails_instead_of_deploying_blind(pipeline):
    pipeline["live"](pipeline["newer"])

    code, decision, said = pipeline["decide"](pipeline["older"], ssh="fail")

    assert code != 0 and decision == ""
    assert "Could not read which commit production runs" in said


@pytest.mark.parametrize("recorded", ["0" * 40, "not-a-sha", "unrelated"])
def test_a_recorded_commit_on_no_shared_history_fails(pipeline, recorded):
    if recorded == "unrelated":
        runner = pipeline["runner"]
        _git(runner, "checkout", "-q", "--orphan", "elsewhere")
        recorded = _commit(runner, "unrelated root")
        _git(runner, "checkout", "-q", "main")
    pipeline["live"](recorded)

    code, decision, _ = pipeline["decide"](pipeline["older"])

    assert code != 0 and decision == ""


def test_an_unreadable_marker_fails_instead_of_reading_as_a_first_release(pipeline):
    (pipeline["vps"] / ".released_sha").mkdir()

    code, decision, said = pipeline["decide"](pipeline["older"])

    assert code != 0 and decision == ""
    assert "Could not read which commit production runs" in said


def test_a_deploy_that_stopped_halfway_blocks_anything_older(pipeline):
    """Newer started (files and maybe the bot changed) but never recorded
    itself: the older commit may not follow it, whatever .released_sha says."""
    pipeline["live"](pipeline["older"])
    pipeline["under_way"](pipeline["newer"])

    code, decision, _ = pipeline["decide"](pipeline["older"])

    assert (code, decision) == (0, "deploy=false")


def test_the_halfway_commit_itself_can_be_deployed_again(pipeline):
    _git(pipeline["runner"], "fetch", "-q", "origin")
    pipeline["live"](pipeline["older"])
    pipeline["under_way"](pipeline["newer"])

    assert pipeline["decide"](pipeline["newer"])[:2] == (0, "deploy=true")


def test_a_finished_deploy_records_itself_and_clears_the_under_way_marker(pipeline):
    pipeline["live"](pipeline["older"])
    vps = pipeline["vps"]

    assert pipeline["step"]("Mark the release as under way", pipeline["newer"])[0] == 0
    assert (vps / ".releasing_sha").read_text() == pipeline["newer"] + "\n"
    assert pipeline["step"]("Record the release production now runs", pipeline["newer"])[0] == 0

    assert (vps / ".released_sha").read_text() == pipeline["newer"] + "\n"
    assert not (vps / ".releasing_sha").exists()
    assert not list(vps.glob("*.tmp"))


@pytest.mark.parametrize("step, marker", [
    ("Mark the release as under way", ".releasing_sha"),
    ("Record the release production now runs", ".released_sha"),
])
def test_a_marker_is_never_half_written(pipeline, step, marker):
    """A write that fails leaves the previous marker whole, and the step fails."""
    vps = pipeline["vps"]
    (vps / marker).write_text(pipeline["older"] + "\n")
    (vps / f"{marker}.tmp").mkdir()  # the temporary file cannot be written

    code, _, _ = pipeline["step"](step, pipeline["newer"])

    assert code != 0
    assert (vps / marker).read_text() == pipeline["older"] + "\n"


def test_the_under_way_marker_stays_when_the_record_fails(pipeline):
    """Removing it first would leave nothing naming the commit now live."""
    vps = pipeline["vps"]
    pipeline["under_way"](pipeline["newer"])
    (vps / ".released_sha.tmp").mkdir()

    pipeline["step"]("Record the release production now runs", pipeline["newer"])

    assert (vps / ".releasing_sha").read_text() == pipeline["newer"] + "\n"


def test_the_image_build_pulls_docker_hub_through_a_mirror():
    """GitHub's runners share Docker Hub's anonymous pull limit, and on
    2026-10-09 every build failed on "429 Too Many Requests" for
    python:3.12-slim. BuildKit asks Google's public mirror first."""
    steps = _yaml(_WORKFLOW)["jobs"]["image"]["steps"]
    buildx = next(step for step in steps
                  if step.get("uses", "").startswith("docker/setup-buildx-action"))

    import tomllib

    config = tomllib.loads(buildx["with"]["buildkitd-config-inline"])
    assert config["registry"]["docker.io"]["mirrors"] == ["mirror.gcr.io"]


def test_the_test_database_is_pulled_through_the_mirror_too():
    """The same 429 then hit the Postgres service container, which the
    runner pulls itself, outside BuildKit: its image names the mirror."""
    service = _yaml(_WORKFLOW)["jobs"]["test"]["services"]["postgres"]

    assert service["image"] == "mirror.gcr.io/pgvector/pgvector:pg16"
