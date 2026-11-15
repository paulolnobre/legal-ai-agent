"""The private costs dashboard: valid JSON, asking only for metrics that exist
and for prices that have a box to change them in."""
import json
import re
from pathlib import Path

from prometheus_client import REGISTRY

from core import metrics  # noqa: F401  (registers the counters)

DASHBOARD = Path(__file__).parent.parent / "infra/grafana/dashboards/costs.json"


def _dashboard() -> dict:
    return json.loads(DASHBOARD.read_text())


def _expressions() -> list[str]:
    return [t["expr"] for p in _dashboard()["panels"] for t in p.get("targets", [])
            if "expr" in t]


def _registered() -> set[str]:
    names: set[str] = set()
    for collector in list(REGISTRY._collector_to_names.values()):  # noqa: SLF001
        names.update(collector)
    return names


def test_every_metric_it_asks_for_exists():
    asked = {m for e in _expressions() for m in re.findall(r"\blegal_[a-z_]+", e)}
    assert asked, "it reads the app's own counters"
    known = _registered()
    missing = {m for m in asked if m not in known}
    assert not missing, f"not exported by the app: {missing}"


def test_every_price_it_uses_has_a_box():
    boxes = {v["name"] for v in _dashboard()["templating"]["list"]}
    used = {v for e in _expressions() for v in re.findall(r"\$([a-z0-9_]+)\b", e)
            if not v.startswith("_")}
    assert used and not used - boxes, f"no box for {used - boxes}"


def test_it_is_in_dollars_and_for_this_month_by_default():
    d = _dashboard()
    assert d["time"]["from"] == "now/M"
    stats = [p for p in d["panels"] if p["type"] == "stat"]
    assert stats and all(p["fieldConfig"]["defaults"]["unit"] == "currencyUSD" for p in stats)


def _model_patterns() -> dict[str, str]:
    """The model regex each price pair is attached to, as the cost panel writes it."""
    expr = next(p for p in _dashboard()["panels"] if p["id"] == 1)["targets"][0]["expr"]
    found = {}
    for pattern in set(re.findall(r'model=~"([^"]+)"', expr)):
        found[pattern] = pattern
    return found


def test_a_model_is_priced_by_exactly_one_pair():
    """Prometheus anchors a regex, so each must match whole names: gpt-4o's
    must not take the mini, nor the mini's the other."""
    patterns = _model_patterns()
    assert len(patterns) == 2
    full = {"gpt-4o": 0, "gpt-4o-2024-08-06": 0, "gpt-4o-mini": 1, "gpt-4o-mini-2024-07-18": 1}
    other = ["gemini-2.0-flash", "gpt-4.1", "gpt-4o-audio-preview", "whisper-1"]
    ordered = sorted(patterns)  # "gpt-4o(-20.*)?" sorts before "gpt-4o-mini.*"
    for name, owner in full.items():
        hits = [i for i, p in enumerate(ordered) if re.fullmatch(p, name)]
        assert hits == [owner], f"{name} matched {hits}"
    for name in other:
        assert not any(re.fullmatch(p, name) for p in ordered), f"{name} is priced by mistake"


def test_the_history_query_reads_columns_the_table_has():
    sql = next(t["rawSql"] for p in _dashboard()["panels"] for t in p.get("targets", [])
               if "rawSql" in t)
    table = Path(__file__).parent.parent.joinpath("infra/init.sql").read_text()
    block = table[table.index("CREATE TABLE IF NOT EXISTS budget_history"):]
    block = block[:block.index(");")]
    selected = re.search(r"SELECT (.*?) FROM", sql)
    assert selected
    columns = [c.strip() for c in selected.group(1).split(",")]
    assert "FROM budget_history" in sql
    assert columns and all(re.search(rf"\b{c}\b", block) for c in columns)
