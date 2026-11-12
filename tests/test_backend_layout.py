"""Backend package layout: the route table is frozen and every module obeys the size rules.

The route table is a characterization of the API as it was before backend/app.py was split
into modules (2026-11-12); a change here is an API change and must be deliberate.
"""
import ast
from pathlib import Path
import tempfile
import unittest

from backend.app import ESTIMATE_ERRORS, create_app

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_LINES = 300
MAX_FUNCTION_LINES = 100

EXPECTED_ROUTES = {
    ("GET", "/api/health"), ("GET", "/api/options"), ("GET", "/api/data/status"), ("GET", "/api/experiments"),
    ("POST", "/api/estimate"), ("POST", "/api/crawl"), ("POST", "/api/experiments/run"), ("GET", "/api/jobs/{job_id}"),
    ("GET", "/api/big-roads"), ("GET", "/api/big-roads/junctions"), ("GET", "/api/big-roads/meta"),
    ("GET", "/api/big-roads/land"), ("GET", "/api/big-roads/boundary"), ("GET", "/api/big-roads/provinces"),
    ("GET", "/api/big-roads/clusters"), ("GET", "/api/big-roads/review"), ("PUT", "/api/big-roads/review/{key}"),
    ("POST", "/api/big-roads/route"),
    ("GET", "/api/{path:path}"), ("POST", "/api/{path:path}"), ("PUT", "/api/{path:path}"), ("DELETE", "/api/{path:path}"),
    ("PATCH", "/api/{path:path}"), ("OPTIONS", "/api/{path:path}"), ("HEAD", "/api/{path:path}"),
    ("GET", "/{path:path}"),
}


def leaf_routes(router):
    """Routes in registration order, descending into included routers (FastAPI nests them)."""
    for route in router.routes:
        if hasattr(route, "methods"):
            yield route
        elif hasattr(route, "original_router"):  # fastapi >= 0.147 nests an included router
            yield from leaf_routes(route.original_router)
        elif hasattr(route, "routes"):
            yield from leaf_routes(route)


def route_table(app):
    table = set()
    for route in leaf_routes(app.router):
        for method in route.methods or ():
            if method in ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
                table.add((method, route.path))
    return table


class RouteTableTests(unittest.TestCase):
    def test_route_table_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(root=temp, osrm_probe=lambda: True, key_reader=lambda: None)
        table = {r for r in route_table(app) if not r[1].startswith(("/api/docs", "/api/openapi", "/docs"))}
        self.assertEqual(table, EXPECTED_ROUTES)

    def test_catch_alls_are_registered_last(self):
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(root=temp, osrm_probe=lambda: True, key_reader=lambda: None)
        paths = [route.path for route in leaf_routes(app.router)]
        self.assertEqual(paths[-2:], ["/api/{path:path}", "/{path:path}"])

    def test_estimate_error_map_keeps_the_exception_names(self):
        self.assertEqual(set(ESTIMATE_ERRORS), {"MissingRoadData", "RoadClassError", "RouteAlignmentError", "SpatialDataError"})


class SizeRuleTests(unittest.TestCase):
    def modules(self):
        return sorted((ROOT / "backend").glob("*.py"))

    def test_every_backend_module_is_at_most_300_lines(self):
        too_long = {p.name: n for p in self.modules() if (n := len(p.read_text(encoding="utf-8").splitlines())) > MAX_FILE_LINES}
        self.assertEqual(too_long, {})

    def test_no_backend_function_is_longer_than_100_lines(self):
        too_long = {}
        for path in self.modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    length = node.end_lineno - node.lineno + 1
                    if length > MAX_FUNCTION_LINES:
                        too_long[f"{path.name}:{node.name}"] = length
        self.assertEqual(too_long, {})


if __name__ == "__main__":
    unittest.main()
