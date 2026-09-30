"""不可拆分批次完整嵌入核验的单元测试。

覆盖：
- 可装入：规范路径、逐管占用、剩余通量且不超冻结流量；
- 管路竞争失败：未安置批次、占满管路、剩余容量，且结论稳定；
- 完整搜索 vs 朴素逐批贪心（贪心会误判的用例必须判可装入）；
- 禁经管路、无可用路径；
- 仅接受 optimal 来源、批次数量/类型校验；
- 随机小网络：用独立的全路径笛卡尔积暴力枚举对照 embed 结论（完备性）。
"""

import itertools
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solver import parse_and_validate, solve, solve_payload  # noqa: E402
from embed import (  # noqa: E402
    MAX_BATCHES,
    embed,
    embed_payload,
    enumerate_simple_paths,
    frozen_from_result,
    parse_batches,
)
from solver import ValidationError  # noqa: E402


def mk(stations, pipes):
    return {"stations": [{"id": s, "balance": b} for s, b in stations],
            "pipes": [
                {"id": f"e{i}", "from": u, "to": v, "lo": lo, "hi": hi,
                 "cost": c}
                for i, (u, v, lo, hi, c) in enumerate(pipes)]}


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

# 流量被上下界钉死的双路竞争网络
DIAMOND = {
    "stations": [{"id": "S", "balance": 4}, {"id": "C", "balance": 0},
                 {"id": "D", "balance": 0}, {"id": "T", "balance": -4}],
    "pipes": [
        {"id": "eSC", "from": "S", "to": "C", "lo": 2, "hi": 2, "cost": 0},
        {"id": "eCT", "from": "C", "to": "T", "lo": 2, "hi": 2, "cost": 0},
        {"id": "eSD", "from": "S", "to": "D", "lo": 2, "hi": 2, "cost": 0},
        {"id": "eDT", "from": "D", "to": "T", "lo": 2, "hi": 2, "cost": 0}],
}


def check_embedded(res, payload, batches):
    """独立复算成功结论：每批一条简单路径、整批体积、逐管不超冻结流量。"""
    assert res["status"] == "embedded"
    frozen = {e["id"]: e["flow"] for e in _edges_of(payload)}
    used = {pid: 0 for pid in frozen}
    pipe_map = {}
    for p in payload["pipes"]:
        pipe_map[(p["from"], p["to"])] = p["id"]
    by_id = {b["id"]: b for b in batches}
    assert len(res["batches"]) == len(batches)
    for br in res["batches"]:
        spec = by_id[br["id"]]
        st = br["path"]["stations"]
        pids = br["path"]["pipes"]
        assert st[0] == spec["from"] and st[-1] == spec["to"]
        assert len(st) == len(pids) + 1
        # 顶点简单路径、相邻站点由该管路连接、整批体积压在每管
        assert len(st) == len(set(st)), "路径必须顶点简单"
        assert set(pids).isdisjoint(spec.get("avoid", []))
        for a, bb, pid in zip(st, st[1:], pids):
            assert pipe_map[(a, bb)] == pid
            used[pid] += spec["volume"]
        # 逐管占用：路径上每管一条、整批体积
        occ_pipes = {o["pipe"]: o["amount"] for o in br["occupancy"]}
        assert occ_pipes == {pid: spec["volume"] for pid in pids}
    for pu in res["pipe_usage"]:
        assert used[pu["pipe"]] == pu["used"]
        assert pu["frozen_flow"] == frozen[pu["pipe"]]
        assert pu["remaining"] == pu["frozen_flow"] - pu["used"]
        assert pu["used"] <= pu["frozen_flow"], "超过冻结流量"
    return used


def _edges_of(payload):
    prob = parse_and_validate(payload)
    return solve(prob)["edges"]


class TestEmbedSuccess(unittest.TestCase):
    def test_loadable_sample(self):
        res = solve_payload(SAMPLE)
        batches = [
            {"id": "B1", "from": "S1", "to": "D2", "volume": 2, "avoid": []},
            {"id": "B2", "from": "S2", "to": "D2", "volume": 2}]
        out = embed_payload(res, "run-x", {"batches": batches})
        used = check_embedded(out, SAMPLE, batches)
        # p5 冻结流量恰为 2，被 B1 占满
        self.assertEqual(used["p5"], 2)
        self.assertEqual(next(p for p in out["pipe_usage"]
                              if p["pipe"] == "p5")["remaining"], 0)

    def test_complete_search_beats_greedy(self):
        # X(S->T,2) 与 Y(C->T,1)。朴素逐批贪心按路径序让 X 走 S-C-T，
        # 会占满 eCT 导致 Y 无路；完整分配必须让 X 改走 S-D-T。
        res = solve_payload(DIAMOND)
        batches = [{"id": "X", "from": "S", "to": "T", "volume": 2},
                   {"id": "Y", "from": "C", "to": "T", "volume": 1}]
        out = embed_payload(res, "g", {"batches": batches})
        self.assertEqual(out["status"], "embedded")
        check_embedded(out, DIAMOND, batches)
        xpath = next(b for b in out["batches"] if b["id"] == "X")["path"]["pipes"]
        ypath = next(b for b in out["batches"] if b["id"] == "Y")["path"]["pipes"]
        self.assertEqual(ypath, ["eCT"])
        self.assertEqual(xpath, ["eSD", "eDT"])  # 完整搜索的回溯选择

    def test_forbidden_pipe_forces_alternate(self):
        res = solve_payload(DIAMOND)
        batches = [{"id": "X", "from": "S", "to": "T", "volume": 1,
                    "avoid": ["eSC", "eCT"]}]
        out = embed_payload(res, "g", {"batches": batches})
        self.assertEqual(out["status"], "embedded")
        self.assertEqual(out["batches"][0]["path"]["pipes"], ["eSD", "eDT"])

    def test_path_report_is_canonical_and_stable(self):
        res = solve_payload(SAMPLE)
        batches = [{"id": "B", "from": "S1", "to": "D2", "volume": 1}]
        a = embed_payload(res, "g", {"batches": batches})
        b = embed_payload(res, "g", {"batches": batches})
        self.assertEqual(a, b)  # 同输入字节级稳定
        # 选最短简单路径（hop 数最少）
        self.assertEqual(a["batches"][0]["path"]["stations"],
                         ["S1", "V1", "D2"])


class TestEmbedFailure(unittest.TestCase):
    def test_pipe_competition_failure(self):
        res = solve_payload(DIAMOND)
        batches = [{"id": "P", "from": "C", "to": "T", "volume": 2},
                   {"id": "Q", "from": "C", "to": "T", "volume": 1}]
        out = embed_payload(res, "g", {"batches": batches})
        self.assertEqual(out["status"], "cannot_embed")
        # C->T 只有 eCT（冻结 2）：两批总量 3 > 2，无法同时装入
        self.assertIn("Q", out["unplaced_batch_ids"])
        sat = {s["pipe"]: s for s in out["saturated_pipes"]}
        self.assertIn("eCT", sat)
        self.assertEqual(sat["eCT"]["used"], 2)
        self.assertEqual(sat["eCT"]["remaining"], 0)
        rem = {p["pipe"]: p["remaining"] for p in out["pipe_usage"]}
        self.assertEqual(rem["eCT"], 0)
        self.assertEqual(rem["eSC"], 2)  # 未被占用的管路保留全量
        self.assertGreaterEqual(out["search"]["nodes"], 1)

    def test_failure_report_is_stable(self):
        res = solve_payload(DIAMOND)
        body = {"batches": [
            {"id": "P", "from": "C", "to": "T", "volume": 2},
            {"id": "Q", "from": "C", "to": "T", "volume": 1}]}
        import json
        a = json.dumps(embed_payload(res, "g", body), sort_keys=True)
        b = json.dumps(embed_payload(res, "g", body), sort_keys=True)
        self.assertEqual(a, b)

    def test_forbidden_closes_all_paths(self):
        res = solve_payload(DIAMOND)
        out = embed_payload(res, "g", {"batches": [
            {"id": "Z", "from": "C", "to": "T", "volume": 1,
             "avoid": ["eCT"]}]})
        self.assertEqual(out["status"], "cannot_embed")
        self.assertEqual(out["unplaced_batch_ids"], ["Z"])

    def test_volume_exceeds_every_path(self):
        res = solve_payload(DIAMOND)  # 各管冻结 2
        out = embed_payload(res, "g", {"batches": [
            {"id": "Big", "from": "S", "to": "T", "volume": 3}]})
        self.assertEqual(out["status"], "cannot_embed")
        self.assertEqual(out["unplaced_batch_ids"], ["Big"])


class TestFrozenAndValidation(unittest.TestCase):
    def test_rejects_non_optimal_source(self):
        bad_net = mk([("S", 5), ("T", -5)], [("S", "T", 0, 2, 1)])
        infeasible = solve_payload(bad_net)
        self.assertEqual(infeasible["status"], "infeasible")
        with self.assertRaises(ValidationError):
            frozen_from_result(infeasible, "a")

    def test_too_many_batches(self):
        res = solve_payload(DIAMOND)
        net = frozen_from_result(res, "g")
        batches = [{"id": f"B{i}", "from": "S", "to": "T", "volume": 1}
                   for i in range(MAX_BATCHES + 1)]
        with self.assertRaises(ValidationError) as ctx:
            parse_batches({"batches": batches}, net)
        self.assertEqual(ctx.exception.loc, "batches")

    def test_float_volume_rejected(self):
        res = solve_payload(DIAMOND)
        with self.assertRaises(ValidationError) as ctx:
            embed_payload(res, "g", {"batches": [
                {"id": "B", "from": "S", "to": "T", "volume": 1.5}]})
        self.assertEqual(ctx.exception.loc, "batches[0].volume")

    def test_unknown_station_and_pipe(self):
        res = solve_payload(DIAMOND)
        with self.assertRaises(ValidationError) as ctx:
            embed_payload(res, "g", {"batches": [
                {"id": "B", "from": "ZZ", "to": "T", "volume": 1}]})
        self.assertEqual(ctx.exception.loc, "batches[0].from")
        with self.assertRaises(ValidationError) as ctx:
            embed_payload(res, "g", {"batches": [
                {"id": "B", "from": "S", "to": "T", "volume": 1,
                 "avoid": ["nope"]}]})
        self.assertEqual(ctx.exception.loc, "batches[0].avoid")

    def test_same_from_to_rejected(self):
        res = solve_payload(DIAMOND)
        with self.assertRaises(ValidationError) as ctx:
            embed_payload(res, "g", {"batches": [
                {"id": "B", "from": "S", "to": "S", "volume": 1}]})
        self.assertEqual(ctx.exception.loc, "batches[0]")


class TestSimplePathEnumeration(unittest.TestCase):
    def test_enumerates_all_simple_paths_independently(self):
        prob = parse_and_validate(SAMPLE)
        res = solve(prob)
        net = frozen_from_result(res, "g")
        b = parse_batches({"batches": [
            {"id": "B", "from": "S1", "to": "D2", "volume": 1}]}, net)[0]
        got = enumerate_simple_paths(net, b)

        # 独立 DFS 枚举（顶点简单路径），按管路序号集合对照
        ids = net.ids
        adj = {u: [] for u in ids}
        for k, p in enumerate(net.pipes):
            adj[ids[p.u]].append((ids[p.v], k))
        expect = []

        def dfs(u, seen, es):
            if u == "D2":
                expect.append(tuple(es))
                return
            for v, k in adj[u]:
                if v not in seen:
                    dfs(v, seen | {v}, es + [k])
        dfs("S1", {"S1"}, [])
        self.assertEqual(sorted(got), sorted(expect))
        # 规范序：长度升序、管路序号字典序
        keys = [(len(p), p) for p in got]
        self.assertEqual(keys, sorted(keys))


class TestBruteForceCompleteness(unittest.TestCase):
    def test_random_embed_matches_path_cartesian_bruteforce(self):
        """对随机小网络：独立枚举每批全部简单路径，做完整笛卡尔积暴力
        容量检查，其可行性必须与 embed 结论一致（证明非贪心/非有限尝试）。"""
        rng = random.Random(20260930)
        trials = 120
        for trial in range(trials):
            n = rng.randint(3, 5)
            ids = [f"v{i}" for i in range(n)]
            total = rng.randint(1, 4)
            bals = [0] * n
            for _ in range(total):
                bals[rng.randrange(n)] += 1
                bals[rng.randrange(n)] -= 1
            payload = {"stations": [{"id": s, "balance": b}
                                    for s, b in zip(ids, bals)], "pipes": []}
            used_pairs = set()
            for _ in range(rng.randint(n - 1, n + 2)):
                u, v = rng.sample(range(n), 2)
                if (u, v) in used_pairs:
                    continue
                used_pairs.add((u, v))
                hi = rng.randint(1, 3)
                payload["pipes"].append(
                    {"id": f"e{len(payload['pipes'])}", "from": ids[u],
                     "to": ids[v], "lo": 0, "hi": hi, "cost": rng.randint(0, 3)})
            res = solve_payload(payload)
            if res["status"] != "optimal":
                continue
            net = frozen_from_result(res, "t")
            # 选 1..3 批随机 s->t（s 有出边、t 有入边），体积 1..2
            specs = []
            for _ in range(rng.randint(1, 3)):
                s = rng.choice(ids)
                t = rng.choice([x for x in ids if x != s])
                specs.append({"id": f"B{len(specs)}", "from": s, "to": t,
                              "volume": rng.randint(1, 2), "avoid": []})
            batches = parse_batches({"batches": specs}, net)
            out = embed(net, batches)

            # 独立暴力：每批路径集合 -> 笛卡尔积 -> 任一组合容量可行?
            all_paths = [enumerate_simple_paths(net, b) for b in batches]
            brute_feasible = False
            if all(all_paths):
                cap = [p.flow for p in net.pipes]
                for combo in itertools.product(*all_paths):
                    load = [0] * net.m
                    ok = True
                    for b, path in zip(batches, combo):
                        for e in path:
                            load[e] += b.volume
                            if load[e] > cap[e]:
                                ok = False
                                break
                        if not ok:
                            break
                    if ok:
                        brute_feasible = True
                        break
            self.assertEqual(out["status"] == "embedded", brute_feasible,
                             f"trial {trial}: {payload} specs={specs}")
            if out["status"] == "embedded":
                check_embedded(out, payload, specs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
