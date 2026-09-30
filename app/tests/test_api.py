"""HTTP API 与审计幂等测试（线程内启动真实 HTTP 服务）。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("AUDIT_DB", os.path.join(tempfile.mkdtemp(), "audit.json"))
os.environ.setdefault("GROUP_DB", os.path.join(tempfile.mkdtemp(), "groups.json"))

import server  # noqa: E402

SAMPLE = {
    "stations": [
        {"id": "S1", "balance": 6}, {"id": "S2", "balance": 4},
        {"id": "V1", "balance": 0}, {"id": "V2", "balance": 0},
        {"id": "D1", "balance": -3}, {"id": "D2", "balance": -7}],
    "pipes": [
        {"id": "p1", "from": "S1", "to": "V1", "lo": 1, "hi": 8, "cost": 2},
        {"id": "p2", "from": "S2", "to": "V1", "lo": 0, "hi": 5, "cost": 4},
        {"id": "p3", "from": "S2", "to": "V2", "lo": 1, "hi": 6, "cost": 1},
        {"id": "p4", "from": "V1", "to": "D1", "lo": 0, "hi": 4, "cost": 0},
        {"id": "p5", "from": "V1", "to": "D2", "lo": 0, "hi": 9, "cost": 3},
        {"id": "p6", "from": "V2", "to": "D2", "lo": 0, "hi": 7, "cost": 2},
        {"id": "p7", "from": "D1", "to": "D2", "lo": 0, "hi": 3, "cost": -1}],
}


class ApiBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def req(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(raw)
                                     if "application/json" in ctype else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())


class ApiTest(ApiBase):

    def test_health_and_page(self):
        status, body = self.req("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, html = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("最小费用流", html)
        status, js = self.req("GET", "/static/app.js")
        self.assertEqual(status, 200)

    def test_solve_evidence(self):
        status, body = self.req("POST", "/api/solve", SAMPLE,
                                {"X-Audit-Id": "run-evidence-1"})
        self.assertEqual(status, 200)
        r = body["result"]
        self.assertEqual(r["status"], "optimal")
        self.assertFalse(body["replayed"])
        # 独立复算费用、平衡与约化成本
        flows = {f["id"]: f["flow"] for f in r["flows"]}
        cost = 0
        net = {s["id"]: 0 for s in SAMPLE["stations"]}
        for p in SAMPLE["pipes"]:
            f = flows[p["id"]]
            self.assertTrue(p["lo"] <= f <= p["hi"])
            cost += f * p["cost"]
            net[p["from"]] += f
            net[p["to"]] -= f
        bal = {s["id"]: s["balance"] for s in SAMPLE["stations"]}
        self.assertEqual(net, bal)
        self.assertEqual(cost, r["total_cost"])
        pi = r["potentials"]
        for e in r["edges"]:
            for arc in e["residual"]:
                self.assertGreaterEqual(arc["reduced_cost"], 0)
                self.assertEqual(
                    arc["reduced_cost"],
                    arc["cost"] + pi[arc["from"]] - pi[arc["to"]])

    def test_audit_same_payload_replays_original(self):
        headers = {"X-Audit-Id": "run-replay-1"}
        s1, b1 = self.req("POST", "/api/solve", SAMPLE, headers)
        s2, b2 = self.req("POST", "/api/solve", SAMPLE, headers)
        self.assertEqual(s1, s2, 200)
        self.assertFalse(b1["replayed"])
        self.assertTrue(b2["replayed"])
        self.assertEqual(b1["result"], b2["result"])  # 原记录字节级一致
        self.assertEqual(b1["payload_fingerprint"], b2["payload_fingerprint"])
        # 记录可通过 API 取回
        s3, b3 = self.req("GET", "/api/records/run-replay-1")
        self.assertEqual(s3, 200)
        self.assertEqual(b3["result"], b1["result"])

    def test_audit_changed_payload_rejected(self):
        s1, b1 = self.req("POST", "/api/solve", SAMPLE,
                          {"X-Audit-Id": "run-conflict-1"})
        self.assertEqual(s1, 200)
        changed = json.loads(json.dumps(SAMPLE))
        changed["pipes"][0]["cost"] = 999
        s2, b2 = self.req("POST", "/api/solve", changed,
                          {"X-Audit-Id": "run-conflict-1"})
        self.assertEqual(s2, 409)
        self.assertIn("不同载荷", b2["error"])
        self.assertEqual(b2["conflict"]["payload_fingerprint"],
                         b1["payload_fingerprint"])

    def test_invalid_locates_field(self):
        bad = {"stations": [{"id": "S", "balance": 4},
                            {"id": "T", "balance": -3}], "pipes": []}
        status, body = self.req("POST", "/api/solve", bad)
        self.assertEqual(status, 400)
        self.assertEqual(body["loc"], "stations")
        bad2 = json.loads(json.dumps(SAMPLE))
        bad2["pipes"][2]["hi"] = 0  # lo=1 > hi
        status, body = self.req("POST", "/api/solve", bad2)
        self.assertEqual(status, 400)
        self.assertEqual(body["loc"], "pipes[2].hi")

    def test_infeasible_evidence(self):
        payload = {"stations": [{"id": "S", "balance": 5},
                                {"id": "T", "balance": -5}],
                   "pipes": [{"id": "x", "from": "S", "to": "T",
                              "lo": 0, "hi": 2, "cost": 1}]}
        status, body = self.req("POST", "/api/solve", payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "infeasible")
        self.assertEqual(body["unmet_demand"][0]["station"], "T")
        self.assertEqual(body["unmet_demand"][0]["unsatisfied"], 3)
        self.assertIn("S", body["reachable_from_source"])


BOTTLENECK = {
    "stations": [{"id": "BS", "balance": 5}, {"id": "BM", "balance": 0},
                 {"id": "BT", "balance": -5}],
    "pipes": [{"id": "q1", "from": "BS", "to": "BM", "lo": 0, "hi": 5,
               "cost": 1},
              {"id": "q2", "from": "BM", "to": "BT", "lo": 0, "hi": 5,
               "cost": 1}],
}


class EmbedApiTest(ApiBase):
    def solve_src(self, audit_id, payload=SAMPLE):
        s, b = self.req("POST", "/api/solve", payload, {"X-Audit-Id": audit_id})
        self.assertEqual(s, 200)
        self.assertEqual(b["result"]["status"], "optimal")
        return b["result"]

    def test_embed_success_replay_lookup_and_conflicts(self):
        flows = {f["id"]: f["flow"]
                 for f in self.solve_src("embed-src-1")["flows"]}
        req = {"audit_id": "embed-src-1", "group_id": "grp-ok-1",
               "batches": [
                   {"id": "b1", "source": "S1", "target": "D1", "volume": 2},
                   {"id": "b2", "source": "S2", "target": "D2", "volume": 3,
                    "forbid": ["p5"]}]}
        s, e1 = self.req("POST", "/api/embed", req)
        self.assertEqual(s, 200)
        self.assertEqual(e1["status"], "embedded")
        self.assertFalse(e1["replayed"])
        r = e1["result"]
        # 独立复算：路径连通、遵守禁行、逐管占用不超过冻结流量
        pmap = {p["id"]: p for p in SAMPLE["pipes"]}
        used = {}
        for b in r["batches"]:
            cur = b["source"]
            for pid in b["path"]:
                self.assertIn(pid, pmap)
                self.assertEqual(pmap[pid]["from"], cur)
                cur = pmap[pid]["to"]
                used[pid] = used.get(pid, 0) + b["volume"]
            self.assertEqual(cur, b["target"])
        self.assertNotIn("p5", r["batches"][1]["path"])  # 禁行生效
        for pid, tot in used.items():
            self.assertLessEqual(tot, flows[pid])
        for u in r["pipe_usage"]:
            self.assertEqual(u["remaining"], u["flow"] - u["used"])
            self.assertEqual(u["used"], used.get(u["id"], 0))
            self.assertEqual(u["flow"], flows[u["id"]])  # 冻结原流量

        # 同标识同载荷重传：回放原编组结论，不重新求解
        s, e2 = self.req("POST", "/api/embed", req)
        self.assertEqual(s, 200)
        self.assertTrue(e2["replayed"])
        self.assertEqual(e2["result"], r)
        self.assertEqual(e2["request_fingerprint"], e1["request_fingerprint"])

        # 记录可读取（页面刷新/重开后核对同一结论）
        s, g = self.req("GET", "/api/groups/grp-ok-1")
        self.assertEqual(s, 200)
        self.assertEqual(g["result"], r)
        self.assertEqual(g["status"], "embedded")
        self.assertEqual(g["audit_id"], "embed-src-1")
        self.assertEqual([p["id"] for p in g["frozen"]["pipes"]],
                         [p["id"] for p in SAMPLE["pipes"]])  # 稳定管路顺序

        # 改换批次或来源：拒绝 409
        changed = json.loads(json.dumps(req))
        changed["batches"][0]["volume"] = 9
        s, c1 = self.req("POST", "/api/embed", changed)
        self.assertEqual(s, 409)
        self.assertIn("不同来源或批次", c1["error"])
        changed2 = json.loads(json.dumps(req))
        changed2["audit_id"] = "embed-src-other"
        s, _ = self.req("POST", "/api/embed", changed2)
        self.assertEqual(s, 409)

    def test_embed_contention_failure(self):
        self.solve_src("embed-src-2", BOTTLENECK)
        req = {"audit_id": "embed-src-2", "group_id": "grp-block-1",
               "batches": [
                   {"id": "c1", "source": "BS", "target": "BT", "volume": 3},
                   {"id": "c2", "source": "BS", "target": "BT", "volume": 3}]}
        s, e = self.req("POST", "/api/embed", req)
        self.assertEqual(s, 200)
        self.assertEqual(e["status"], "cannot_embed")
        r = e["result"]
        self.assertEqual(r["first_exhausted_layer"], 1)
        self.assertEqual([b["id"] for b in r["unplaced_batches"]], ["c2"])
        self.assertEqual(r["placed_prefix"][0]["path"], ["q1", "q2"])
        rem = {u["id"]: u["remaining"] for u in r["remaining_capacity"]}
        self.assertEqual(rem, {"q1": 2, "q2": 2})
        # 失败结论同样幂等回放
        s, e2 = self.req("POST", "/api/embed", req)
        self.assertTrue(e2["replayed"])
        self.assertEqual(e2["result"], r)

    def test_embed_source_must_be_optimal(self):
        # 不可行审计不能作为编组来源
        bad = {"stations": [{"id": "S", "balance": 5},
                            {"id": "T", "balance": -5}],
               "pipes": [{"id": "x", "from": "S", "to": "T",
                          "lo": 0, "hi": 2, "cost": 1}]}
        s, b = self.req("POST", "/api/solve", bad,
                        {"X-Audit-Id": "embed-src-infeasible"})
        self.assertEqual(b["result"]["status"], "infeasible")
        req = {"audit_id": "embed-src-infeasible", "group_id": "grp-bad-src",
               "batches": [{"id": "b1", "source": "S", "target": "T",
                            "volume": 1}]}
        s, e = self.req("POST", "/api/embed", req)
        self.assertEqual(s, 400)
        self.assertIn("optimal", e["error"])
        # 不存在的来源审计：404
        req["audit_id"] = "no-such-audit"
        req["group_id"] = "grp-bad-src2"
        s, e = self.req("POST", "/api/embed", req)
        self.assertEqual(s, 404)
        # 未知编组读取：404
        s, _ = self.req("GET", "/api/groups/no-such-group")
        self.assertEqual(s, 404)

    def test_embed_validation(self):
        self.solve_src("embed-src-3")
        base = {"audit_id": "embed-src-3", "group_id": "grp-val"}
        # 超过六批
        too_many = [{"id": f"b{i}", "source": "S1", "target": "D1",
                     "volume": 1} for i in range(7)]
        s, e = self.req("POST", "/api/embed", dict(base, batches=too_many))
        self.assertEqual(s, 400)
        self.assertEqual(e["loc"], "batches")
        # 非整数体积
        s, e = self.req("POST", "/api/embed", dict(base, batches=[
            {"id": "b1", "source": "S1", "target": "D1", "volume": 1.5}]))
        self.assertEqual(s, 400)
        self.assertEqual(e["loc"], "batches[0].volume")
        # 未知禁行管路
        s, e = self.req("POST", "/api/embed", dict(base, batches=[
            {"id": "b1", "source": "S1", "target": "D1", "volume": 1,
             "forbid": ["zz"]}]))
        self.assertEqual(s, 400)
        self.assertEqual(e["loc"], "batches[0].forbid[0]")
        # 缺编组标识
        s, e = self.req("POST", "/api/embed",
                        {"audit_id": "embed-src-3", "batches": [
                            {"id": "b1", "source": "S1", "target": "D1",
                             "volume": 1}]})
        self.assertEqual(s, 400)
        self.assertEqual(e["loc"], "group_id")
        # 校验失败不占用编组标识：修正后同标识可正常提交
        s, e = self.req("POST", "/api/embed", dict(base, batches=[
            {"id": "b1", "source": "S1", "target": "D1", "volume": 1}]))
        self.assertEqual(s, 200)
        self.assertEqual(e["status"], "embedded")

    def test_audit_regression_after_embed(self):
        # 编组功能不影响原审计与读取接口
        self.req("POST", "/api/solve", SAMPLE, {"X-Audit-Id": "regress-src"})
        s, b = self.req("POST", "/api/solve", SAMPLE,
                        {"X-Audit-Id": "regress-src"})
        self.assertEqual(s, 200)
        self.assertTrue(b["replayed"])  # 同标识同载荷重放原记录
        s, rec = self.req("GET", "/api/records/regress-src")
        self.assertEqual(s, 200)
        self.assertEqual(rec["result"]["status"], "optimal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
