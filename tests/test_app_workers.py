"""Every background worker the application starts, it also stops.

Two PRs added a worker each in the same lines of `lifespan`, and the merge
between them is where one of the two can quietly drop out of the start list or
the shutdown tuple. A task never started is a feature that silently never runs;
one never cancelled holds the shutdown open. Read from the source, because
starting the real application needs a database.
"""
import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "core" / "web" / "app.py"


def _lifespan() -> ast.AsyncFunctionDef:
    tree = ast.parse(_APP.read_text(encoding="utf-8"))
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )


def _started() -> dict[str, str]:
    """Task variable -> the worker function it runs."""
    started = {}
    for node in ast.walk(_lifespan()):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == "asyncio.create_task"):
            worker = node.value.args[0]
            assert isinstance(worker, ast.Call)
            started[ast.unparse(node.targets[0])] = ast.unparse(worker.func)
    return started


def _cancelled() -> set[str]:
    for node in ast.walk(_lifespan()):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            return {ast.unparse(element) for element in node.iter.elts}
    raise AssertionError("lifespan no longer cancels its tasks in one loop")


def test_every_started_worker_is_cancelled_on_shutdown():
    assert set(_started()) == _cancelled()


def test_the_workers_the_firm_relies_on_are_started():
    workers = set(_started().values())
    for expected in ("run_update_worker", "run_maintenance_worker",
                     "run_inbox_sweep_worker", "run_law_refresh_worker",
                     "run_publication_watch_worker", "run_deadline_alerts_worker"):
        assert expected in workers, f"{expected} is never started"
