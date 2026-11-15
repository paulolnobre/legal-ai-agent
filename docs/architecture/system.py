"""Draw the deployment as an SVG with real icons, rendered outside CI.

The architecture used to be drawn exactly this way, and #34 removed it for two
reasons that were both about the tooling rather than the picture: Graphviz
arrived through apt-get in CI, where a bad mirror hash failed a pull request
that had nothing to do with architecture, and a PNG nobody could diff sat beside
the SVG.

It came back because a chart of plain boxes is not what anybody pictures when
they think of this system, and the icons are read faster than the labels. What
changed is where and how it is drawn:

- rendered by scripts/render_architecture.sh inside a throwaway container, so
  CI installs nothing and no package mirror can fail a build over a diagram;
- SVG only, with its icons embedded, so GitHub shows it without a build step;
- checked for meaning by the ordinary test suite, with no Graphviz: every
  service in the production compose file must have a node here, and every node
  here must be in the committed SVG. A new service, or a render somebody forgot,
  fails a test instead of leaving an old picture that still looks current.

Diagrams is imported inside generate(), so the tests can read this module
without it installed.
"""
# reportUnusedExpression: every edge in this file is built with `a >> Edge(...)
# >> b`, which is diagrams' own API and not a mistake. The operator's return
# value is discarded on purpose; it is the graph's internal state, drawn for
# its side effect, that Diagram() writes out. The rule would otherwise flag
# every single edge declaration below.
# pyright: reportUnusedExpression=false
import base64
import html
import os
import re
from pathlib import Path

OUTPUT = Path(__file__).with_name("system")
_IMAGE_HREF = re.compile(r'(?P<prefix>(?:xlink:)?href=")(?P<path>[^"]+)(?P<suffix>")')

# The node that stands for each service in infra/vps/runtime-compose.yml.
# The test suite reads this without importing diagrams and fails when the
# compose file gains a service that has no node here.
COMPOSE_SERVICES = {
    "legal-bot": "api",
    "postgres": "postgres",
    "redis": "redis",
    "prometheus": "prometheus",
    "grafana": "grafana",
    "alertmanager": "alertmanager",
    "redis-exporter": "redis_exporter",
    "loki": "loki",
    "alloy": "alloy",
}


def _embed_svg_images(svg: Path) -> None:
    """Replace local icon paths with data URLs.

    GitHub serves an SVG as an image and blocks every external reference inside
    it, so an icon left as a path on the machine that drew it renders as a
    broken box for everybody else.
    """
    content = svg.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        image = Path(html.unescape(match["path"]))
        if not image.is_file() or image.suffix.lower() != ".png":
            return match[0]
        encoded = base64.b64encode(image.read_bytes()).decode("ascii")
        return f'{match["prefix"]}data:image/png;base64,{encoded}{match["suffix"]}'

    svg.write_text(_IMAGE_HREF.sub(replace, content), encoding="utf-8")


def generate() -> None:
    from diagrams import Cluster, Diagram, Edge
    from diagrams.generic.storage import Storage
    from diagrams.onprem.client import Users
    from diagrams.onprem.compute import Server
    from diagrams.onprem.database import PostgreSQL
    from diagrams.onprem.inmemory import Redis
    from diagrams.onprem.logging import Loki
    from diagrams.onprem.monitoring import Grafana, Prometheus
    from diagrams.onprem.network import Internet, Traefik
    from diagrams.programming.flowchart import Document, MultipleDocuments
    from diagrams.programming.framework import FastAPI
    from diagrams.programming.language import Python
    from diagrams.saas.chat import Telegram

    formats = [f for f in os.environ.get("ARCH_FORMATS", "svg").split(",") if f]
    # Top to bottom, not left to right. The first version ran sideways and
    # measured roughly 28x28 inches: with this many clusters, LR forces every
    # sibling of the VPS cluster (external providers, both Magalu Cloud buckets)
    # onto its own column stacked vertically, and most of that column sits
    # empty. TB folds the same nodes into rows instead and rendered at about
    # 60% of the area, measured by rendering both and comparing the SVG's own
    # width/height.
    #
    # ranksep and nodesep set the canvas size for a graph this size far more
    # than font size does, so those are what is tuned to shrink it further.
    # nodesep needed to be wider than first tried: the "legal-bot container"
    # row holds three two-line labels side by side, and a narrower value let
    # them touch. A cluster's own margin is passed per-cluster below, because
    # diagrams applies graph_attr to the whole canvas and has no top-level knob
    # for a subgraph's inner padding.
    graph_attr = {
        "bgcolor": "white",
        "fontname": "Arial",
        "fontsize": "16",
        "pad": "0.15",
        "ranksep": "0.45",
        "nodesep": "0.75",
    }
    node_attr = {"fontname": "Arial", "fontsize": "11"}
    edge_attr = {"fontname": "Arial", "fontsize": "9", "color": "#52606d"}
    cluster_attr = {"margin": "8"}
    _MONITORING_COLOR = "#9aa5b1"

    def watch(label: str = "") -> Edge:
        """An edge for the observability profile, drawn lighter because it is
        not the product.

        A named function instead of `Edge(**{"color": ...})`: pyright cannot
        verify that a plain `dict[str, str]` spread only ever supplies `color`,
        so it checked every keyword `Edge.__init__` accepts, including `node`
        and the two boolean direction flags, against a `str`, and reported all
        of them. A function with its own signature has none of that ambiguity.
        """
        return Edge(label=label, color=_MONITORING_COLOR)

    with Diagram(
        "Demo & Associados Legal Agent: deployment and observability",
        filename=str(OUTPUT),
        direction="TB",
        outformat=formats,
        show=False,
        graph_attr=graph_attr,
        node_attr=node_attr,
        edge_attr=edge_attr,
    ) as diagram:
        # Set after construction because diagrams only accepts "ortho" or
        # "curved", and both leave edge labels floating away from their edges.
        diagram.dot.graph_attr["splines"] = "spline"

        staff = Users("Authorized legal staff", nodeid="staff")
        telegram = Telegram("Telegram Bot API", nodeid="telegram")
        drive = MultipleDocuments("Firm's Google Drive", nodeid="drive")

        with Cluster("Magalu Cloud, br-se1", graph_attr=cluster_attr):
            inbox = Storage("Documents bucket\nentrada/ inbox,\nencrypted originals",
                            nodeid="mgc_documents")

        with Cluster("VPS, Docker Compose", graph_attr=cluster_attr):
            traefik = Traefik("Traefik\nTLS, the only public route", nodeid="traefik")

            with Cluster("legal-bot container", graph_attr=cluster_attr):
                api = FastAPI("Webhook and pages\n/web with 2FA, /health, /metrics",
                              nodeid="api")
                worker = Python("Update worker\nLangGraph specialists", nodeid="worker")
                scheduled = Python("Scheduled workers\nbucket sweep, retention", nodeid="scheduled")
                ingest = Document("Ingestion\nPDF, DOCX, HTML, OCR", nodeid="ingest")

            redis = Redis("Redis\ndurable queue + cache", nodeid="redis")
            postgres = PostgreSQL("PostgreSQL + pgvector\ncorpus, audit, staff", nodeid="postgres")
            files = Storage("Older originals\non disk", nodeid="files")

            with Cluster("Observability profile, loopback only", graph_attr=cluster_attr):
                prometheus = Prometheus("Prometheus", nodeid="prometheus")
                alertmanager = Server("Alertmanager", nodeid="alertmanager")
                redis_exporter = Server("redis_exporter", nodeid="redis_exporter")
                alloy = Server("Grafana Alloy\ncontainer logs", nodeid="alloy")
                loki = Loki("Loki", nodeid="loki")
                grafana = Grafana("Grafana\nread-only database role", nodeid="grafana")

        with Cluster("External providers", graph_attr=cluster_attr):
            models = Internet("OpenAI + Gemini\ngeneration, embeddings", nodeid="models")
            research = Internet("DataJud, Serper, Exa\nofficial sources", nodeid="research")
            langsmith = Internet("LangSmith\nredacted traces", nodeid="langsmith")

        with Cluster("Backblaze B2: backups", graph_attr=cluster_attr):
            backups = Storage("Backup bucket\nciphertext only", nodeid="b2_backups")

        # The path of one message, the story the picture exists to tell.
        staff >> Edge(label="messages, voice, documents") >> telegram
        telegram >> Edge(label="HTTPS webhook") >> traefik >> api
        api >> Edge(label="persist, then 200") >> redis
        redis >> Edge(label="reserve one") >> worker
        worker >> Edge(label="RAG + audit") >> postgres
        worker >> ingest
        ingest >> postgres
        ingest >> files
        ingest >> Edge(label="encrypted original") >> inbox
        worker >> models
        worker >> research
        worker >> Edge(style="dashed") >> langsmith
        # Declared in the direction of the flow with the arrowhead reversed, so
        # the reply helps the layout instead of looping over it, and its label
        # stays on its line. The arrow still points at Telegram.
        telegram << Edge(label="replies") << worker

        # Documents that never pass through the chat.
        drive >> Edge(label="folder sync") >> inbox
        inbox >> Edge(label="read in office hours") >> scheduled
        scheduled >> ingest
        postgres >> Edge(label="nightly backup, encrypted", style="dashed") >> backups

        # Watching it, drawn lighter because it is not the product.
        prometheus >> watch("scrape") >> api
        prometheus >> watch() >> redis_exporter >> watch() >> redis
        prometheus >> watch() >> alertmanager
        telegram << Edge(label="alerts, straight to Telegram", color="#c0392b") << alertmanager
        alloy >> watch() >> loki
        grafana >> watch() >> prometheus
        grafana >> watch() >> loki
        grafana >> watch() >> postgres

    svg = OUTPUT.with_suffix(".svg")
    if svg.exists():
        _embed_svg_images(svg)


if __name__ == "__main__":
    generate()
