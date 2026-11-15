"""The observability stack has to be wired, not just written.

Every part of this is configuration that only fails on the VPS: a mount whose
file does not exist, a service nobody starts, a rule that fires into nothing.
None of it shows up in the application's own tests, and the cost of finding out
in production is an outage nobody was told about. So the wiring is asserted
here, where it costs a second.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = "/opt/demo-firm-agent-v2"
RUNTIME_BOUNDARY = "/etc/demo-firm-agent"

COMPOSE = yaml.safe_load(
    (ROOT / "infra/vps/runtime-compose.yml").read_text(encoding="utf-8")
)
DEPLOY_SCRIPT = (ROOT / "scripts/deploy_vps_root.sh").read_text(encoding="utf-8")
ALERTS = yaml.safe_load(
    (ROOT / "infra/prometheus/alerts.yml").read_text(encoding="utf-8")
)
PROMETHEUS = yaml.safe_load(
    (ROOT / "infra/prometheus/prometheus.yml").read_text(encoding="utf-8")
)


def _bind_mounts() -> list[tuple[str, str]]:
    """Every (service, host path) the compose file mounts from the deploy tree."""
    found = []
    for name, service in COMPOSE["services"].items():
        for volume in service.get("volumes") or []:
            host = volume.split(":", 1)[0]
            if host.startswith("/"):
                found.append((name, host))
    return found


def _observability_services() -> set[str]:
    return {
        name
        for name, service in COMPOSE["services"].items()
        if "observability" in (service.get("profiles") or [])
    }


def test_every_mounted_file_exists_in_the_repository():
    """A bind mount to a path that does not exist does not fail loudly.

    Docker creates a directory where the file should be and the container then
    starts against nonsense, which is how a config typo becomes a crash loop at
    three in the morning rather than a failed test.
    """
    for service, host_path in _bind_mounts():
        if not host_path.startswith(DEPLOY_ROOT + "/"):
            continue
        relative = host_path[len(DEPLOY_ROOT) + 1:]
        assert (ROOT / relative).exists(), (
            f"{service} mounts {host_path}, which the deploy synchronizes from "
            f"{relative} in this repository, and that file does not exist"
        )


def test_the_alert_configuration_is_never_synchronized_with_the_source():
    """It holds a bot token, so it lives where the deploy account cannot write.

    Everything under the deploy root arrives by rsync from an SSH account. A
    file that can send messages as the firm's bot belongs beside the compose
    file instead, root-owned, rendered on the host.
    """
    mounts = dict((path, service) for service, path in _bind_mounts())
    alert_config = f"{RUNTIME_BOUNDARY}/alertmanager.yml"

    assert alert_config in mounts, "Alertmanager must read its config from the boundary"
    assert not any(
        "alertmanager" in path and path.startswith(DEPLOY_ROOT)
        for path in mounts
    ), "an Alertmanager config under the deploy root would put a token where rsync writes"


def test_the_deploy_starts_every_observability_service():
    """A service in the profile that nothing starts is a service that does not
    exist, and it exists in the file, which is worse: it reads as covered."""
    for name in _observability_services():
        assert name in DEPLOY_SCRIPT, (
            f"{name} carries the observability profile but the deploy script "
            "never names it, so it would never start"
        )


def test_alerting_reaches_alertmanager():
    """Rules that fire into nothing are decoration."""
    targets = [
        target
        for entry in PROMETHEUS["alerting"]["alertmanagers"]
        for config in entry["static_configs"]
        for target in config["targets"]
    ]

    assert any(t.startswith("alertmanager:") for t in targets)
    assert "/etc/prometheus/alerts.yml" in PROMETHEUS["rule_files"]


def test_every_alert_says_how_bad_it_is_and_what_happened():
    """Severity routes the message and the summary is the message. An alert
    missing either arrives as a name and a shrug."""
    for group in ALERTS["groups"]:
        for rule in group["rules"]:
            name = rule["alert"]
            assert rule.get("labels", {}).get("severity") in {"warning", "critical"}, (
                f"{name} has no severity, so Alertmanager cannot route it"
            )
            assert rule.get("annotations", {}).get("summary"), (
                f"{name} has no summary, so the message would carry no meaning"
            )
            assert "for" in rule, f"{name} would fire on a single scrape"


def test_the_outage_that_went_unnoticed_now_has_an_alert():
    """On 2026-09-09 the bot was down and a failed deploy log was the only
    notice. This is the rule that would have said so, and it stays."""
    names = {rule["alert"] for group in ALERTS["groups"] for rule in group["rules"]}

    assert "LegalAgentDown" in names


def test_the_config_check_can_read_the_file_it_checks():
    """The staged config is 0600 and owned by root; the image runs as nobody.

    Without `--user 0:0` amtool answers "permission denied", `set -e` aborts,
    and alerting can never be installed. Measured against the real image before
    this line existed.
    """
    script = (ROOT / "scripts/install_alertmanager_config.sh").read_text(encoding="utf-8")
    check = [line for line in script.splitlines() if "docker run" in line]

    assert check, "the rendered config is validated before it replaces a working one"
    assert "--user 0:0" in check[0]


def test_the_documented_psql_command_does_not_quote_the_password():
    """`:'password'` is psql's quote-as-a-literal form, so it adds the quotes.

    A value that arrives already quoted is stored with them, and the role ends
    up with a password nothing else knows: Grafana reads GRAFANA_DB_PASSWORD
    from the environment, unquoted, and is refused. The failure is silent at
    setup time and only shows as a datasource that cannot connect.
    """
    sql = (ROOT / "infra/sql/grafana_readonly.sql").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for text, where in ((sql, "the SQL file"), (readme, "the README")):
        assert "password=\"'" not in text, (
            f"{where} documents a command that stores the quotes as part of the "
            "password"
        )


def test_grafana_reads_the_database_through_a_role_that_cannot_write():
    """A dashboard is where a typo runs against production."""
    datasource = yaml.safe_load(
        (ROOT / "infra/grafana/provisioning/datasources/postgres.yml").read_text(
            encoding="utf-8"
        )
    )
    user = datasource["datasources"][0]["user"]
    # Comments explain what is excluded and why, so they name the very columns
    # this asserts on. Only the statements count.
    grants = "\n".join(
        line
        for line in (ROOT / "infra/sql/grafana_readonly.sql")
        .read_text(encoding="utf-8")
        .splitlines()
        if not line.lstrip().startswith("--")
    )

    assert user == "${GRAFANA_DB_USER}", "the credential belongs in the environment"
    assert "grafana_ro" in grants
    assert "GRANT SELECT" in grants
    for forbidden in ("GRANT INSERT", "GRANT UPDATE", "GRANT DELETE", "GRANT ALL"):
        assert forbidden not in grants, f"{forbidden} would make the dashboard writable"
    # The corpus itself is never readable from a dashboard. The guard is about
    # the text, not the table: counting passages needs the linkage column and
    # nothing else, so a column-scoped grant on document_chunks is allowed and
    # a grant of its content is not. Naming the columns says what is actually
    # forbidden, which the earlier version of this assertion did not.
    for column in ("content", "embedding", "full_text"):
        assert f"({column})" not in grants and f" {column}," not in grants, (
            f"{column} would put the corpus itself on a dashboard"
        )
    for line in grants.splitlines():
        if "ON document_chunks" in line:
            assert "GRANT SELECT (document_id)" in line, (
                "document_chunks may only be granted one column, the linkage"
            )


def test_the_role_script_can_actually_run():
    """psql does not substitute :'variables' inside dollar quoting.

    The first version of this file wrapped the CREATE ROLE in a DO $$ ... $$
    block holding :'password'. psql passed the literal text through and the
    server died on the colon, so the one file standing between a dashboard and
    a writable connection to production could never have run.
    """
    sql = (ROOT / "infra/sql/grafana_readonly.sql").read_text(encoding="utf-8")

    dollar_quoted = re.findall(r"\$\$(.*?)\$\$", sql, re.DOTALL)
    for block in dollar_quoted:
        assert ":'" not in block, (
            "a psql variable inside dollar quoting is passed through as text, "
            "not substituted; build the statement outside the block and run it "
            "with \\gexec"
        )
