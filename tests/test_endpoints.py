import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from diff_risk_sentinel.endpoints import orphan_endpoints
from tests.gitutil import GitRepo

TASKS = '''\
from fastapi import APIRouter

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("/{task_id}/history")
def task_history(task_id: str):
    return []


@router.post("/{task_id}/archive")
def archive_task(task_id: str):
    return {}


@router.post("/catalog/{item_id}/activate")
def activate_item(item_id: str):
    return {}


@router.get("/{task_id}")
def get_task(task_id: str):
    return {}


@router.post("/legacy-sync")
@router.post("/sync")
def sync_tasks():
    return {}
'''

FACADE = '''\
from fastapi import APIRouter

router = APIRouter()


@router.get("/suggestions/pending")
async def pending_suggestions():
    return await crm_get("/suggestions/pending")


@router.post("/{account_id}/reauthorize")
def reauthorize(account_id: str):
    return {}


@router.get("/files/download")
def download(token: str):
    return b""


def download_link(path):
    return "/hub-api/files/download?token=" + sign(path)
'''

CRM = '''\
from fastapi import APIRouter

router = APIRouter()


@router.get("/suggestions/pending")
def list_pending():
    return []


@router.post("/internal/probe-inbound")
def probe_inbound():
    return {}
'''

WEB = '''\
export const history = (id: string) => http.get(`${API}/tasks/${id}/history`);
export const setActive = (id: string, action: string) => http.post(`/tasks/catalog/${id}/${action}`);
export const sync = () => http.post("/tasks/sync");
export const reauth = (id: string, suffix: string) => http.post(`/hub-api/accounts/${id}/reauthorize${suffix}`);
'''


class TestOrphanEndpoints(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        GitRepo(self.tmp.name).commit({
            "hub/routers/tasks.py": TASKS,
            "hub/routers/facade.py": FACADE,
            "crm/routers/suggestions.py": CRM,
            "web/api/tasks.ts": WEB,
            "docs/runbook.md": "Run `POST /internal/probe-inbound` after the cutover.\n",
            "hub/tests/test_tasks.py": "client.post('/tasks/abc/archive')\n",
        }, "init")
        self.found = {f["function"]: f for f in orphan_endpoints(self.tmp.name, "HEAD")}

    def tearDown(self):
        self.tmp.cleanup()

    def test_route_nothing_calls_is_reported_with_its_full_path(self):
        f = self.found["archive_task"]
        self.assertEqual((f["file"], f["method"], f["path"]), ("hub/routers/tasks.py", "post", "/tasks/{task_id}/archive"))

    def test_client_calls_with_parameters_keep_routes_alive(self):
        self.assertNotIn("task_history", self.found)  # `${API}/tasks/${id}/history`

    def test_client_may_interpolate_the_last_segment(self):
        self.assertNotIn("activate_item", self.found)  # `/tasks/catalog/${id}/${action}`

    def test_query_string_appended_by_the_client_still_matches(self):
        self.assertNotIn("reauthorize", self.found)  # `/hub-api/accounts/${id}/reauthorize${suffix}`

    def test_any_used_path_keeps_a_multi_route_handler_alive(self):
        self.assertNotIn("sync_tasks", self.found)

    def test_paths_without_distinctive_segments_are_not_judged(self):
        self.assertNotIn("get_task", self.found)  # /tasks/{task_id}: too generic to search

    def test_a_handler_proxying_its_own_path_is_not_its_own_client(self):
        self.assertIn("pending_suggestions", self.found)  # its only mention is its own proxy call
        self.assertNotIn("list_pending", self.found)  # the facade's proxy call is a real client of the backend

    def test_route_decorators_elsewhere_are_not_callers(self):
        self.assertIn("probe_inbound", self.found)

    def test_urls_built_elsewhere_in_the_same_file_count(self):
        self.assertNotIn("download", self.found)

    def test_docs_and_tests_do_not_count_but_docs_are_shown(self):
        self.assertEqual(self.found["probe_inbound"]["documented_in"], ["docs/runbook.md"])
        self.assertIn("archive_task", self.found)  # only a test calls it


class TestCustomRouterClasses(unittest.TestCase):

    def test_prefix_of_any_router_class_is_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            GitRepo(tmp).commit({
                "api/flows.py": 'router = VersionedRouter(prefix="/flows", tags=["x"])\n\n\n@router.post("/filter")\ndef read_flows():\n    return []\n',
                "api/deployments.py": 'router = VersionedRouter(prefix="/deployments")\n\n\n@router.post("/filter")\ndef read_deployments():\n    return []\n',
                "ui/api.ts": "export const deployments = () => http.post('/deployments/filter');\n",
            }, "init")
            found = [f["function"] for f in orphan_endpoints(tmp, "HEAD")]
        self.assertEqual(found, ["read_flows"])


class TestDeadCodeReportIncludesEndpoints(unittest.TestCase):

    def test_run_dead_code_lists_orphan_endpoints_separately(self):
        from diff_risk_sentinel.cli import run_dead_code
        with tempfile.TemporaryDirectory() as tmp:
            GitRepo(tmp).commit({"api/routes.py": ("from fastapi import APIRouter\nrouter = APIRouter()\n\n\n"
                                                   "@router.post('/reports/rebuild')\ndef rebuild():\n    return 1\n")},
                                "init")
            out = os.path.join(tmp, "dead.json")
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(run_dead_code(repo=tmp, rev="HEAD", output=out, top=10), 0)
            data = json.load(open(out))
        self.assertEqual([e["function"] for e in data["orphan_endpoints"]], ["rebuild"])
        self.assertEqual(data["dead_code"], [])
        self.assertIn("/reports/rebuild", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
