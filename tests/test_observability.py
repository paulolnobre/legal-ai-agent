"""Privacy, correlation, metrics, readiness, and generated-diagram checks."""
import asyncio
import importlib.util
import json
import logging
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from core.config import Settings
from core.integrations import llm
from core.logger import JsonFormatter
from core.observability import bind_context
from core.web import app as web_app


def test_json_logs_carry_safe_correlation_fields():
    record = logging.LogRecord("core.test", logging.INFO, __file__, 1, "worked", (), None)
    with bind_context(correlation="telegram-42", update=42):
        payload = json.loads(JsonFormatter().format(record))
    assert payload["correlation_id"] == "telegram-42"
    assert payload["update_id"] == "42"
    assert payload["message"] == "worked"


def test_trace_content_requires_an_explicit_double_opt_in(monkeypatch):
    monkeypatch.setenv("LANGSMITH_HIDE_INPUTS", "false")
    monkeypatch.setenv("LANGSMITH_HIDE_OUTPUTS", "true")
    monkeypatch.setenv("LANGSMITH_CAPTURE_CONTENT", "false")
    with pytest.raises(ValidationError):
        Settings()


def test_llm_trace_processors_redact_legal_content(monkeypatch):
    monkeypatch.setattr(llm.settings, "langsmith_capture_content", False)
    inputs = llm._trace_inputs({
        "system": "secret system prompt",
        "user": "client legal question",
        "task": "complex",
        "max_tokens": 100,
    })
    output = llm._trace_output("confidential answer")
    assert "secret" not in str(inputs)
    assert "client legal question" not in str(inputs)
    assert "confidential answer" not in str(output)
    assert inputs["content_redacted"] is True


def test_metrics_endpoint_refreshes_queue_depth(monkeypatch):
    async def queue_depths():
        return {"active": 3, "deferred": 1, "processing": 0, "dead_letter": 2}

    monkeypatch.setattr(web_app.settings, "metrics_enabled", True)
    monkeypatch.setattr(web_app.update_queue, "queue_depths", queue_depths)
    response = asyncio.run(web_app.metrics())
    assert b'legal_queue_depth{queue="active"} 3.0' in response.body
    assert b'legal_queue_depth{queue="dead_letter"} 2.0' in response.body


def test_readiness_checks_redis_and_postgres(monkeypatch):
    calls = []

    async def ping():
        calls.append("redis")

    class Connection:
        async def execute(self, statement):
            calls.append(statement)

    class ConnectionContext:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return None

    class Pool:
        def connection(self):
            return ConnectionContext()

    async def get_pool():
        return Pool()

    monkeypatch.setattr(web_app.update_queue, "ping", ping)
    monkeypatch.setattr(web_app, "get_pool", get_pool)
    monkeypatch.setattr(
        web_app,
        "configuration_report",
        lambda: {
            "status": "unavailable",
            "check": "configuration_only",
            "dependencies": {},
        },
    )
    assert asyncio.run(web_app.ready()) == {
        "status": "ready",
        "dependencies": {"redis": "ready", "postgres": "ready"},
        "capabilities": {
            "external_research": {
                "status": "unavailable",
                "check": "configuration_only",
                "dependencies": {},
            }
        },
    }
    assert calls == ["redis", "SELECT 1"]


def _agent_flow_generator():
    """Load the docs generator by path; `docs/architecture` is not a package."""
    spec = importlib.util.spec_from_file_location(
        "agent_flow", Path("docs/architecture/agent_flow.py")
    )
    assert spec and spec.loader, "docs/architecture/agent_flow.py is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_agent_flow_matches_compiled_graph():
    """The picture is written by the graph, so it cannot disagree with it."""
    artifact = Path("docs/architecture/agent-flow.md").read_text(encoding="utf-8")
    assert artifact == _agent_flow_generator().render()


ARCHITECTURE = Path("docs/architecture")


def _system_module():
    """Load the drawing source by path. Diagrams is imported only inside
    generate(), so this needs neither diagrams nor Graphviz."""
    spec = importlib.util.spec_from_file_location(
        "system_diagram", ARCHITECTURE / "system.py"
    )
    assert spec and spec.loader, "docs/architecture/system.py is not importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ci_installs_no_renderer():
    """The picture is drawn in a container, never in CI.

    On 2026-09-09 Graphviz arrived through apt-get in CI, a mirror served a bad
    hash and the install exited 100, failing a pull request that had nothing to
    do with architecture. A drawing tool must not be able to break somebody
    else's build, which is why scripts/render_architecture.sh exists.
    """
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    for gone in ("graphviz", "apt-get", "diagrams=="):
        assert gone not in workflow, f"CI installs {gone} to draw a diagram again"


def test_only_the_svg_is_committed():
    """A PNG beside the SVG is several hundred kilobytes nobody can diff, and it
    was the first thing #34 removed. The renderer can write one as a preview,
    and git ignores it.

    Measured as what git would commit, tracked or not, so the preview on the
    machine that drew it does not count and a new file does.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         str(ARCHITECTURE)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert {Path(path).name for path in listed} == {
        "README.md", "agent-flow.md", "agent_flow.py", "system.py", "system.svg",
    }


def test_every_service_that_runs_is_drawn():
    """The compose file is what runs. A service it gains must appear in the
    picture, or the picture quietly describes a system that no longer exists."""
    compose = yaml.safe_load(
        Path("infra/vps/runtime-compose.yml").read_text(encoding="utf-8")
    )
    drawn = _system_module().COMPOSE_SERVICES
    missing = sorted(set(compose["services"]) - set(drawn))
    assert not missing, f"{missing} run in production and are not in the picture"


def test_the_committed_svg_is_the_rendered_source():
    """The nodes in system.svg are exactly the nodes in system.py.

    Graphviz writes each node's id as its <title>, so this compares two text
    files and needs no renderer. A node added to the source and never rendered,
    one removed from the source but still in the picture, or a compose service
    mapped to a node that does not exist, all fail here rather than leaving an
    old drawing that still looks current.
    """
    source = (ARCHITECTURE / "system.py").read_text(encoding="utf-8")
    svg = (ARCHITECTURE / "system.svg").read_text(encoding="utf-8")
    declared = set(re.findall(r'nodeid="([^"]+)"', source))
    rendered = set(re.findall(r'class="node">\s*<title>([^<]+)</title>', svg))

    assert set(_system_module().COMPOSE_SERVICES.values()) <= declared
    assert rendered == declared, (
        f"only in system.py: {sorted(declared - rendered)}; "
        f"only in system.svg: {sorted(rendered - declared)}. "
        "Run scripts/render_architecture.sh and commit the SVG."
    )


def test_every_icon_travels_inside_the_svg():
    """GitHub serves an SVG as an image and blocks every external reference in
    it, so an icon left as a path on the drawing machine is a broken box for
    everybody else."""
    svg = (ARCHITECTURE / "system.svg").read_text(encoding="utf-8")
    hrefs = re.findall(r'href="([^"]+)"', svg)

    assert hrefs, "the picture has no icons at all"
    outside = [href for href in hrefs if not href.startswith("data:image/")]
    assert not outside, f"{len(outside)} icon(s) point outside the file"


def test_local_icon_paths_are_embedded(tmp_path: Path):
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"png-content")
    svg = tmp_path / "diagram.svg"
    svg.write_text(f'<image xlink:href="{icon}" />', encoding="utf-8")

    _system_module()._embed_svg_images(svg)

    assert "data:image/png;base64,cG5nLWNvbnRlbnQ=" in svg.read_text(encoding="utf-8")


def test_both_readmes_show_the_picture():
    """An embedded image, not a link to one.

    The first version of this test looked for "](system.svg)", which a plain
    link in the introduction also contains. With the picture deleted from the
    page, the link kept the test green: a mutation check is what showed it.
    """
    image = r"!\[[^\]]*\]\({}\)"
    architecture = (ARCHITECTURE / "README.md").read_text(encoding="utf-8")
    root = Path("README.md").read_text(encoding="utf-8")

    assert re.search(image.format(re.escape("system.svg")), architecture)
    assert re.search(image.format(re.escape("docs/architecture/system.svg")), root)
    assert "```mermaid" in (ARCHITECTURE / "agent-flow.md").read_text(encoding="utf-8")
