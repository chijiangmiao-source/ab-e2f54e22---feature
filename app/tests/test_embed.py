"""编组嵌入求解测试：完整整数容量分配、规范路径、最先耗尽层证据、
禁行管路、回溯非贪心、幂等编组存储。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from embed import (  # noqa: E402
    MAX_BATCHES,
    EmbedError,
    GroupConflict,
    GroupStore,
    embed,
    freeze_from_audit_record,
    validate_batches,
)
from solver import solve_payload  # noqa: E402


def mk(stations, pipes):
    return {"stations": [{"id": s, "balance": b} for s, b in stations],
            "pipes": [
                {"id": f"e{i}", "from": u, "to": v, "lo": lo, "hi": hi,
                 "cost": c}
                for i, (u, v, lo, hi, c) in enumerate(pipes)]}


def freeze(payload):
    """求解并冻结为编组来源（要求 optimal）。"""
    res = solve_payload(payload)
    assert res["status"] == "optimal", res
    return freeze_from_audit_record({"status": "optimal", "result": res})


def batches(frozen, specs):
    return validate_batches(
        [dict(s) for s in specs], frozen)


# 与 test_api 相同的示例网络：最优流 p1=6,p2=0,p3=4,p4=4,p5=2,p6=4,p7=1
SAMPLE = mk(
    [("S1", 6), ("S2", 4), ("V1", 0), ("V2", 0), ("D1", -3), ("D2", -7)],
    [("S1", "V1", 1, 8, 2), ("S2", "V1", 0, 5, 4), ("S2", "V2", 1, 6, 1),
     ("V1", "D1", 0, 4, 0), ("V1", "D2", 0, 9, 3), ("V2", "D2", 0, 7, 2),
     ("D1", "D2", 0, 3, -1)])
SAMPLE_FLOW = {"e0": 6, "e1": 0, "e2": 4, "e3": 4, "e4": 2, "e5": 4, "e6": 1}


def check_paths_valid(frozen, specs, result):
    """独立复算：路径连通、简单、遵守禁行、逐管占用不超过冻结流量。"""
    pipes = {p.id: p for p in frozen.pipes}
    used = {p.id: 0 for p in frozen.pipes}
    by_id = {b["id"]: b for b in result["batches"]}
    for spec in specs:
        rep = by_id[spec["id"]]
        forbid = set(spec.get("forbid", []))
        cur, seen = spec["source"], {spec["source"]}
        for pid in rep["path"]:
            assert pid not in forbid, f"{pid} 为禁行管路"
            p = pipes[pid]
            assert p.frm == cur, f"路径在 {pid} 处断开"
            cur = p.to
            assert cur not in seen, "路径非简单路径"
            seen.add(cur)
            used[pid] += spec["volume"]
        assert cur == spec["target"], "路径未到达目标站"
        assert rep["stations"][0] == spec["source"]
        assert rep["stations"][-1] == spec["target"]
    for u in result["pipe_usage"]:
        assert u["used"] == used[u["id"]], f"{u['id']} 占用统计不一致"
        assert u["used"] <= u["flow"], f"{u['id']} 占用超过冻结流量"
        assert u["remaining"] == u["flow"] - u["used"]


class TestEmbedSuccess(unittest.TestCase):
    def test_two_batches_fit_sample(self):
        frozen = freeze(SAMPLE)
        self.assertEqual({p.id: p.flow for p in frozen.pipes}, SAMPLE_FLOW)
        specs = [
            {"id": "b1", "source": "S1", "target": "D1", "volume": 2},
            {"id": "b2", "source": "S2", "target": "D2", "volume": 3,
             "forbid": ["e4"]},
        ]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "embedded")
        self.assertEqual([b["id"] for b in res["batches"]], ["b1", "b2"])
        check_paths_valid(frozen, specs, res)
        # b2 禁行 e4(V1->D2) 且 e1 流量为 0：只能走 e2->e5
        self.assertEqual(res["batches"][1]["path"], ["e2", "e5"])

    def test_backtracking_revises_earlier_batch(self):
        # b1 规范首选 e0，但 b2 禁行 e1 只能走 e0：完整搜索必须回溯，
        # 让 b1 改走 e1（逐批贪心/最短路在此必然失败）。
        payload = mk([("S", 4), ("T", -4)],
                     [("S", "T", 0, 2, 1), ("S", "T", 0, 2, 1)])
        frozen = freeze(payload)
        specs = [
            {"id": "b1", "source": "S", "target": "T", "volume": 2},
            {"id": "b2", "source": "S", "target": "T", "volume": 2,
             "forbid": ["e1"]},
        ]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "embedded")
        self.assertEqual(res["batches"][0]["path"], ["e1"])  # 回溯改道
        self.assertEqual(res["batches"][1]["path"], ["e0"])
        check_paths_valid(frozen, specs, res)

    def test_determinism_repeat_identical(self):
        frozen = freeze(SAMPLE)
        specs = [
            {"id": "b1", "source": "S1", "target": "D2", "volume": 2},
            {"id": "b2", "source": "S2", "target": "D1", "volume": 1},
            {"id": "b3", "source": "S1", "target": "D1", "volume": 1},
        ]
        r1 = embed(frozen, batches(frozen, specs))
        r2 = embed(frozen, batches(frozen, [dict(s) for s in specs]))
        self.assertEqual(r1, r2)  # 规范解可逐字节复算

    def test_forbidden_forces_detour(self):
        # 直管 e0 流量 3，绕行 e1+e2 流量 2：禁行 e0 后只能绕行
        payload = mk([("S", 5), ("M", 0), ("T", -5)],
                     [("S", "T", 0, 3, 1),
                      ("S", "M", 0, 5, 2),
                      ("M", "T", 0, 5, 2)])
        frozen = freeze(payload)
        self.assertEqual([p.flow for p in frozen.pipes], [3, 2, 2])
        specs = [{"id": "b1", "source": "S", "target": "T", "volume": 2,
                  "forbid": ["e0"]}]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "embedded")
        self.assertEqual(res["batches"][0]["path"], ["e1", "e2"])
        check_paths_valid(frozen, specs, res)


class TestEmbedFailure(unittest.TestCase):
    BOTTLENECK = mk([("BS", 5), ("BM", 0), ("BT", -5)],
                    [("BS", "BM", 0, 5, 1), ("BM", "BT", 0, 5, 1)])

    def test_contention_first_exhausted_layer(self):
        # 两批各 3 单位竞争同一瓶颈（流量 5）：3+3>5，第 2 批所在层耗尽
        frozen = freeze(self.BOTTLENECK)
        specs = [
            {"id": "c1", "source": "BS", "target": "BT", "volume": 3},
            {"id": "c2", "source": "BS", "target": "BT", "volume": 3},
        ]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "cannot_embed")
        self.assertEqual(res["first_exhausted_layer"], 1)
        self.assertEqual([b["id"] for b in res["unplaced_batches"]], ["c2"])
        self.assertEqual(res["placed_prefix"][0]["path"], ["e0", "e1"])
        rem = {u["id"]: u["remaining"] for u in res["remaining_capacity"]}
        self.assertEqual(rem, {"e0": 2, "e1": 2})
        self.assertEqual(res["saturated_pipes"], [])  # 未占满，只是容不下

    def test_saturation_reported(self):
        # 第一批恰好占满瓶颈：已占满管路必须稳定列出
        frozen = freeze(self.BOTTLENECK)
        specs = [
            {"id": "c1", "source": "BS", "target": "BT", "volume": 5},
            {"id": "c2", "source": "BS", "target": "BT", "volume": 1},
        ]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "cannot_embed")
        self.assertEqual(res["first_exhausted_layer"], 1)
        sat = {u["id"] for u in res["saturated_pipes"]}
        self.assertEqual(sat, {"e0", "e1"})
        for u in res["saturated_pipes"]:
            self.assertEqual(u["remaining"], 0)
            self.assertEqual(u["used"], u["flow"])

    def test_no_path_layer_zero(self):
        # 禁行全部通路：第一批即无可用路径，第 0 层耗尽
        payload = mk([("S", 2), ("T", -2)], [("S", "T", 0, 2, 1)])
        frozen = freeze(payload)
        specs = [{"id": "b1", "source": "S", "target": "T", "volume": 1,
                  "forbid": ["e0"]}]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "cannot_embed")
        self.assertEqual(res["first_exhausted_layer"], 0)
        self.assertEqual(res["placed_prefix"], [])
        self.assertEqual([b["id"] for b in res["unplaced_batches"]], ["b1"])

    def test_volume_exceeds_every_path(self):
        frozen = freeze(self.BOTTLENECK)
        specs = [{"id": "b1", "source": "BS", "target": "BT", "volume": 6}]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "cannot_embed")
        self.assertEqual(res["first_exhausted_layer"], 0)

    def test_prefix_layer_two_of_three(self):
        # 2+2+3 > 6：前两批可安置，第三批所在层耗尽
        payload = mk([("S", 6), ("M", 0), ("T", -6)],
                     [("S", "M", 0, 6, 1), ("M", "T", 0, 6, 1)])
        frozen = freeze(payload)
        specs = [
            {"id": "b1", "source": "S", "target": "T", "volume": 2},
            {"id": "b2", "source": "S", "target": "T", "volume": 2},
            {"id": "b3", "source": "S", "target": "T", "volume": 3},
        ]
        res = embed(frozen, batches(frozen, specs))
        self.assertEqual(res["status"], "cannot_embed")
        self.assertEqual(res["first_exhausted_layer"], 2)
        self.assertEqual([b["id"] for b in res["unplaced_batches"]], ["b3"])
        self.assertEqual(len(res["placed_prefix"]), 2)
        rem = {u["id"]: u["remaining"] for u in res["remaining_capacity"]}
        self.assertEqual(rem, {"e0": 2, "e1": 2})


class TestEmbedValidation(unittest.TestCase):
    def setUp(self):
        self.frozen = freeze(SAMPLE)

    def err(self, raw):
        with self.assertRaises(EmbedError) as ctx:
            validate_batches(raw, self.frozen)
        return ctx.exception

    def test_count_bounds(self):
        self.assertEqual(self.err([]).loc, "batches")
        too_many = [{"id": f"b{i}", "source": "S1", "target": "D1",
                     "volume": 1} for i in range(MAX_BATCHES + 1)]
        e = self.err(too_many)
        self.assertEqual(e.loc, "batches")
        self.assertIn("1..6", e.message)

    def test_volume_rules(self):
        base = {"id": "b1", "source": "S1", "target": "D1"}
        for bad in (0, -1, 1.5, True, "2"):
            e = self.err([dict(base, volume=bad)])
            self.assertEqual(e.loc, "batches[0].volume")

    def test_unknown_stations_and_same_endpoints(self):
        e = self.err([{"id": "b1", "source": "ZZ", "target": "D1",
                       "volume": 1}])
        self.assertEqual(e.loc, "batches[0].source")
        e = self.err([{"id": "b1", "source": "S1", "target": "S1",
                       "volume": 1}])
        self.assertEqual(e.loc, "batches[0]")

    def test_unknown_forbid_pipe(self):
        e = self.err([{"id": "b1", "source": "S1", "target": "D1",
                       "volume": 1, "forbid": ["nope"]}])
        self.assertEqual(e.loc, "batches[0].forbid[0]")

    def test_duplicate_batch_id(self):
        e = self.err([{"id": "b1", "source": "S1", "target": "D1",
                       "volume": 1},
                      {"id": "b1", "source": "S2", "target": "D2",
                       "volume": 1}])
        self.assertEqual(e.loc, "batches[1].id")

    def test_freeze_rejects_non_optimal(self):
        with self.assertRaises(EmbedError) as ctx:
            freeze_from_audit_record({"status": "infeasible", "result": {}})
        self.assertEqual(ctx.exception.loc, "audit_id")


class TestGroupStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "groups.json")
        self.frozen = freeze(SAMPLE)
        self.specs = [{"id": "b1", "source": "S1", "target": "D1",
                       "volume": 2}]
        self.req = {"audit_id": "a1", "batches": self.specs}

    def test_save_replay_conflict_and_persistence(self):
        store = GroupStore(self.path)
        bs = batches(self.frozen, self.specs)
        res = embed(self.frozen, bs)
        rec = store.save("g1", self.req, self.frozen, bs, res["status"], res)
        self.assertFalse(rec["replayed"])
        self.assertEqual(rec["frozen"]["pipes"][0]["id"], "e0")

        hit = store.lookup("g1", self.req)
        self.assertTrue(hit["replayed"])
        self.assertEqual(hit["result"], res)

        changed = {"audit_id": "a1", "batches": [
            {"id": "b1", "source": "S1", "target": "D1", "volume": 3}]}
        with self.assertRaises(GroupConflict):
            store.lookup("g1", changed)
        with self.assertRaises(GroupConflict):  # 改换来源同样拒绝
            store.lookup("g1", {"audit_id": "a2", "batches": self.specs})

        # 重启（重新加载落盘文件）后仍可回放同一编组结论
        store2 = GroupStore(self.path)
        hit2 = store2.lookup("g1", self.req)
        self.assertIsNotNone(hit2)
        self.assertEqual(hit2["result"], res)
        self.assertEqual(store2.get("g1")["status"], "embedded")
        self.assertIsNone(store2.get("no-such"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
