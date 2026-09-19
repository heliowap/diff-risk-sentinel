import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from diff_risk_sentinel.jev import JEV_DIMENSIONS, percentiles, query_jev_function, triage_scores

GOOD = {"answers": {
    "introduces_bug": {"type": "noul", "noul": 0.7},
    "edge_cases": {"type": "noul", "noul": 0.4},
    "behavior_change": {"type": "score", "score": 2.5, "confidence": 0.8},
    "semantic_risk": {"type": "score", "score": 1.5, "confidence": 0.6},
}}


class _Server:
    def __init__(self, responses):
        received = self.received = []
        queue = list(responses)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.append(json.loads(self.rfile.read(length)))
                status, body = queue.pop(0) if len(queue) > 1 else queue[0]
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1/systemone"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


STATE = {"file": "a.py", "function": "f", "old_code": "def f(x):\n    return x\n",
         "new_code": "def f(x):\n    if x:\n        return x\n", "diff": "+    if x:"}


class TestQueryJevFunction(unittest.TestCase):

    def test_returns_all_dimensions_and_sends_full_code(self):
        srv = _Server([(200, GOOD)])
        try:
            answer = query_jev_function("key", STATE, api_url=srv.url)
        finally:
            srv.close()
        self.assertEqual(answer, {"introduces_bug": 0.7, "edge_cases": 0.4, "behavior_change": 2.5,
                                  "semantic_risk": 1.5, "confidence": 0.6})
        sent = srv.received[0]
        self.assertEqual(sent["state"]["new_code"], STATE["new_code"])
        self.assertEqual(set(sent["questions"]), set(JEV_DIMENSIONS))

    def test_retries_rate_limits_then_succeeds(self):
        srv = _Server([(429, {"error": "slow down"}), (200, GOOD)])
        try:
            answer = query_jev_function("key", STATE, api_url=srv.url, backoff=0.01)
        finally:
            srv.close()
        self.assertEqual(answer["introduces_bug"], 0.7)
        self.assertEqual(len(srv.received), 2)

    def test_malformed_answer_is_an_error(self):
        srv = _Server([(200, {"answers": {"introduces_bug": {"noul": "high"}}})])
        try:
            answer = query_jev_function("key", STATE, api_url=srv.url)
        finally:
            srv.close()
        self.assertIn("error", answer)

    def test_client_error_is_not_retried(self):
        srv = _Server([(400, {"error": "bad"})])
        try:
            answer = query_jev_function("key", STATE, api_url=srv.url, backoff=0.01)
        finally:
            srv.close()
        self.assertIn("error", answer)
        self.assertEqual(len(srv.received), 1)


class TestRanking(unittest.TestCase):

    def test_percentiles_are_tie_aware(self):
        self.assertEqual(percentiles([1, 5, 5, 9]), [0.0, 0.5, 0.5, 1.0])
        self.assertEqual(percentiles([3]), [1.0])

    def test_triage_is_the_mean_rank_of_crap_and_jev(self):
        crap = [100.0, 1.0, 50.0]
        jev = [
            {"introduces_bug": 0.1, "edge_cases": 0.1, "behavior_change": 0.5, "semantic_risk": 0.5},
            {"introduces_bug": 0.9, "edge_cases": 0.9, "behavior_change": 3.0, "semantic_risk": 3.0},
            {"introduces_bug": 0.5, "edge_cases": 0.5, "behavior_change": 1.0, "semantic_risk": 1.0},
        ]
        scores = triage_scores(crap, jev)
        # item 1: lowest CRAP (0.0) but top on all four Jev dimensions (1.0) -> 0.8
        self.assertAlmostEqual(scores[1], 0.8)
        self.assertAlmostEqual(scores[0], 0.2)
        self.assertGreater(scores[1], scores[2])

    def test_missing_jev_answers_rank_last_on_those_dimensions(self):
        scores = triage_scores([1.0, 2.0], [None, {"introduces_bug": 0.0, "edge_cases": 0.0,
                                                    "behavior_change": 0.0, "semantic_risk": 0.0}])
        self.assertEqual(scores, [0.0, 1.0])


if __name__ == "__main__":
    unittest.main()
