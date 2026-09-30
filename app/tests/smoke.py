"""本题 API 冒烟脚本（在运行中的 web 容器内执行，访问本机服务）。

覆盖：健康检查、页面、最优求解 + 约化成本证据、审计重放原记录、
改载荷拒绝(409)、不可行证据、非法输入字段定位。任一断言失败即以
非零退出码结束。
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, (
                json.loads(raw) if "json" in resp.headers.get(
                    "Content-Type", "") else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


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
        {"id": "p7", "from": "D1", "to": "D2", "lo": 0, "hi": 3,
         "cost": -1}],
}

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("  PASS " if cond else "  FAIL ") + name +
          (f" — {detail}" if detail and not cond else ""))


print(f"[smoke] target = {BASE}")

s, h = call("GET", "/health")
check("GET /health 200/ok", s == 200 and h.get("status") == "ok", str(h))

s, page = call("GET", "/")
check("GET / 页面", s == 200 and "最小费用流" in page)

s, b1 = call("POST", "/api/solve", SAMPLE, {"X-Audit-Id": "smoke-run-1"})
r1 = b1.get("result", {})
check("POST /api/solve 最优", s == 200 and r1.get("status") == "optimal",
      f"{s} {r1.get('status')}")
check("总成本为整数", isinstance(r1.get("total_cost"), int))
pi = r1.get("potentials", {})
rc_ok = all(
    a["reduced_cost"] >= 0 and
    a["reduced_cost"] == a["cost"] + pi[a["from"]] - pi[a["to"]]
    for e in r1.get("edges", []) for a in e["residual"])
check("正残量边约化成本全部 >= 0（最优性证据）", rc_ok)
node_ok = all(n["ok"] for n in r1.get("node_check", []))
check("逐站净供需复算全部通过", node_ok)

s, b2 = call("POST", "/api/solve", SAMPLE, {"X-Audit-Id": "smoke-run-1"})
check("同标识同载荷重放原记录",
      s == 200 and b2.get("replayed") is True and
      b2.get("result") == r1, str(s))

changed = json.loads(json.dumps(SAMPLE))
changed["pipes"][0]["cost"] = 42
s, b3 = call("POST", "/api/solve", changed, {"X-Audit-Id": "smoke-run-1"})
check("同标识改载荷被拒绝 409", s == 409 and "不同载荷" in b3.get("error", ""),
      str(s))

bad_cut = {"stations": [{"id": "S", "balance": 5},
                        {"id": "T", "balance": -5}],
           "pipes": [{"id": "x", "from": "S", "to": "T",
                      "lo": 0, "hi": 2, "cost": 1}]}
s, b4 = call("POST", "/api/solve", bad_cut)
check("无可行流返回未满足需求与可达集",
      s == 200 and b4.get("status") == "infeasible" and
      b4["unmet_demand"][0]["station"] == "T" and
      b4["unmet_demand"][0]["unsatisfied"] == 3 and
      "S" in b4["reachable_from_source"], str(b4)[:200])

bad_input = {"stations": [{"id": "S", "balance": 1.5},
                          {"id": "T", "balance": -1}], "pipes": []}
s, b5 = call("POST", "/api/solve", bad_input)
check("非法输入定位字段", s == 400 and b5.get("loc") == "stations[0].balance",
      str(b5))

# ---------------------------------------------------------------- 编组嵌入
print("[smoke] 编组嵌入：可装入 / 管路竞争失败 / 原审计回归")

# (1) 一组可装入：来源审计 optimal，两批同时嵌入冻结流量
s, es = call("POST", "/api/solve", SAMPLE, {"X-Audit-Id": "smoke-embed-src"})
check("编组来源审计为 optimal",
      s == 200 and es.get("result", {}).get("status") == "optimal", str(s))
flows = {f["id"]: f["flow"] for f in es["result"]["flows"]}

EMBED_REQ = {
    "audit_id": "smoke-embed-src",
    "group_id": "smoke-grp-fit",
    "batches": [
        {"id": "b1", "source": "S1", "target": "D1", "volume": 2},
        {"id": "b2", "source": "S2", "target": "D2", "volume": 3,
         "forbid": ["p5"]}],
}
s, e1 = call("POST", "/api/embed", EMBED_REQ)
r1 = e1.get("result", {})
check("编组嵌入：一组可装入", s == 200 and e1.get("status") == "embedded",
      f"{s} {e1.get('status')}")

pmap = {p["id"]: p for p in SAMPLE["pipes"]}
used, paths_ok = {}, True
for b in r1.get("batches", []):
    cur = b["source"]
    for pid in b["path"]:
        if pid not in pmap or pmap[pid]["from"] != cur:
            paths_ok = False
            break
        cur = pmap[pid]["to"]
        used[pid] = used.get(pid, 0) + b["volume"]
    if cur != b["target"]:
        paths_ok = False
check("规范路径连通且按批次标识给出", paths_ok and
      {b["id"] for b in r1.get("batches", [])} == {"b1", "b2"})
check("禁行管路未被经过",
      all("p5" not in b["path"] for b in r1["batches"] if b["id"] == "b2"))
check("任意管路批次总量不超过原流量",
      all(tot <= flows[pid] for pid, tot in used.items()))
check("剩余通量 = 冻结流量 - 逐管占用",
      all(u["remaining"] == u["flow"] - u["used"] and
          u["used"] == used.get(u["id"], 0) for u in r1["pipe_usage"]))

s, e2 = call("POST", "/api/embed", EMBED_REQ)
check("同编组标识重传回放原结论",
      s == 200 and e2.get("replayed") is True and e2.get("result") == r1,
      str(s))

s, g1 = call("GET", "/api/groups/smoke-grp-fit")
check("编组记录可读取且结论一致（刷新/重开可核对）",
      s == 200 and g1.get("result") == r1 and
      g1.get("status") == "embedded" and
      g1.get("audit_id") == "smoke-embed-src", str(s))

changed = json.loads(json.dumps(EMBED_REQ))
changed["batches"][0]["volume"] = 9
s, e3 = call("POST", "/api/embed", changed)
check("同编组标识改换批次被拒绝 409", s == 409, str(s))
changed_src = json.loads(json.dumps(EMBED_REQ))
changed_src["audit_id"] = "smoke-run-1"
s, e4 = call("POST", "/api/embed", changed_src)
check("同编组标识改换来源被拒绝 409", s == 409, str(s))

# (2) 管路竞争失败：两批各 3 单位竞争流量 5 的瓶颈管
BOTTLENECK = {
    "stations": [{"id": "BS", "balance": 5}, {"id": "BM", "balance": 0},
                 {"id": "BT", "balance": -5}],
    "pipes": [{"id": "q1", "from": "BS", "to": "BM", "lo": 0, "hi": 5,
               "cost": 1},
              {"id": "q2", "from": "BM", "to": "BT", "lo": 0, "hi": 5,
               "cost": 1}],
}
s, bs = call("POST", "/api/solve", BOTTLENECK,
             {"X-Audit-Id": "smoke-embed-bottle"})
check("瓶颈来源审计为 optimal",
      s == 200 and bs.get("result", {}).get("status") == "optimal", str(s))
CONTEND = {
    "audit_id": "smoke-embed-bottle",
    "group_id": "smoke-grp-block",
    "batches": [
        {"id": "c1", "source": "BS", "target": "BT", "volume": 3},
        {"id": "c2", "source": "BS", "target": "BT", "volume": 3}],
}
s, f1 = call("POST", "/api/embed", CONTEND)
rf = f1.get("result", {})
check("管路竞争失败返回 cannot_embed",
      s == 200 and f1.get("status") == "cannot_embed", f"{s} {f1.get('status')}")
check("稳定列出最先耗尽层与未安置批次",
      rf.get("first_exhausted_layer") == 1 and
      [b["id"] for b in rf.get("unplaced_batches", [])] == ["c2"],
      str(rf.get("first_exhausted_layer")))
rem = {u["id"]: u["remaining"] for u in rf.get("remaining_capacity", [])}
check("剩余容量按冻结管路列出", rem == {"q1": 2, "q2": 2}, str(rem))
check("已占满管路字段存在（本例未占满）",
      isinstance(rf.get("saturated_pipes"), list))
s, f2 = call("POST", "/api/embed", CONTEND)
check("失败结论同标识重传亦回放",
      s == 200 and f2.get("replayed") is True and f2.get("result") == rf,
      str(s))

# (3) 原审计回归：求解、重放、记录读取接口保持可用
s, rec = call("GET", "/api/records/smoke-run-1")
check("原审计记录读取回归",
      s == 200 and rec.get("result", {}).get("status") == "optimal", str(s))
s, again = call("POST", "/api/solve", SAMPLE, {"X-Audit-Id": "smoke-run-1"})
check("原审计重放回归",
      s == 200 and again.get("replayed") is True and
      again.get("result", {}).get("status") == "optimal", str(s))
s, g404 = call("GET", "/api/groups/no-such-group")
check("未知编组标识 404", s == 404, str(s))
s, bad_src = call("POST", "/api/embed",
                  {"audit_id": "no-such-audit", "group_id": "smoke-grp-x",
                   "batches": [{"id": "b1", "source": "S", "target": "T",
                                "volume": 1}]})
check("来源审计不存在 404", s == 404, str(s))

failed = [n for n, ok, _ in CHECKS if not ok]
print(f"[smoke] {len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
sys.exit(1 if failed else 0)
