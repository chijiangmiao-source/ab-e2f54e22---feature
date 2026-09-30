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

# ------------------------------------------------------------ 冻结嵌入核验
DIAMOND = {
    "stations": [{"id": "S", "balance": 4}, {"id": "C", "balance": 0},
                 {"id": "D", "balance": 0}, {"id": "T", "balance": -4}],
    "pipes": [
        {"id": "eSC", "from": "S", "to": "C", "lo": 2, "hi": 2, "cost": 0},
        {"id": "eCT", "from": "C", "to": "T", "lo": 2, "hi": 2, "cost": 0},
        {"id": "eSD", "from": "S", "to": "D", "lo": 2, "hi": 2, "cost": 0},
        {"id": "eDT", "from": "D", "to": "T", "lo": 2, "hi": 2, "cost": 0}],
}
s, b6 = call("POST", "/api/solve", DIAMOND, {"X-Audit-Id": "smoke-diamond"})
check("冻结来源 optimal", s == 200 and b6.get("status") == "optimal", str(s))

def embed(group, batches, audit="smoke-diamond"):
    return call("POST", "/api/embed",
                {"audit_id": audit, "group_id": group, "batches": batches},
                {"X-Audit-Id": audit, "X-Group-Id": group})

# 可装入：完整搜索让 X 走 S-D-T、Y 走 eCT（朴素贪心会误判失败）
s, g1 = embed("smoke-group-ok", [
    {"id": "X", "from": "S", "to": "T", "volume": 2},
    {"id": "Y", "from": "C", "to": "T", "volume": 1}])
rg = g1.get("result", {})
ok_paths = {x["id"]: x["path"]["pipes"] for x in rg.get("batches", [])}
cap_ok = all(p["used"] <= p["frozen_flow"] and
             p["remaining"] == p["frozen_flow"] - p["used"]
             for p in rg.get("pipe_usage", []))
check("可装入：完整分配给出规范路径且不超冻结流量",
      s == 200 and rg.get("status") == "embedded" and
      ok_paths.get("Y") == ["eCT"] and ok_paths.get("X") == ["eSD", "eDT"] and
      cap_ok, str(g1)[:300])

# 管路竞争失败：两批 C->T 争 eCT（冻结 2），总量 3 > 2
s, g2 = embed("smoke-group-bad", [
    {"id": "P", "from": "C", "to": "T", "volume": 2},
    {"id": "Q", "from": "C", "to": "T", "volume": 1}])
rg2 = g2.get("result", {})
check("管路竞争失败：列未安置批次/占满管路/剩余容量",
      s == 200 and rg2.get("status") == "cannot_embed" and
      "Q" in rg2.get("unplaced_batch_ids", []) and
      "eCT" in [x["pipe"] for x in rg2.get("saturated_pipes", [])] and
      any(p["pipe"] == "eCT" and p["remaining"] == 0
          for p in rg2.get("pipe_usage", [])), str(g2)[:300])

# 同编组同定义重放原结论
s, g3 = embed("smoke-group-ok", [
    {"id": "X", "from": "S", "to": "T", "volume": 2},
    {"id": "Y", "from": "C", "to": "T", "volume": 1}])
check("同编组同定义重放原结论",
      s == 200 and g3.get("replayed") is True and
      g3.get("result") == rg, str(s))

# 同编组改批次 / 换来源 -> 409
s, g4 = embed("smoke-group-ok", [
    {"id": "X", "from": "S", "to": "T", "volume": 1}])
check("同编组改批次拒绝 409", s == 409, str(s))
s, g5 = embed("smoke-group-ok",
              [{"id": "X", "from": "S", "to": "T", "volume": 2},
               {"id": "Y", "from": "C", "to": "T", "volume": 1}],
              audit="smoke-run-1")
check("同编组换来源拒绝 409", s == 409 and
      g5.get("conflict", {}).get("audit_id") == "smoke-diamond", str(s))

# 读取接口可取回同一编组结论
s, g6 = call("GET", "/api/groups/smoke-group-bad")
check("GET /api/groups/<id> 取回原结论",
      s == 200 and g6.get("result", {}).get("status") == "cannot_embed",
      str(s))

# 非 optimal 来源拒绝冻结：先制造一个 infeasible 审计作为来源
call("POST", "/api/solve",
     {"stations": [{"id": "S", "balance": 5}, {"id": "T", "balance": -5}],
      "pipes": [{"id": "x", "from": "S", "to": "T", "lo": 0, "hi": 2,
                 "cost": 1}]},
     {"X-Audit-Id": "smoke-infeasible-1"})
s, g7 = embed("smoke-group-src",
              [{"id": "P", "from": "S", "to": "T", "volume": 1}],
              audit="smoke-infeasible-1")
check("非 optimal 来源拒绝(409)", s == 409 and "optimal" in g7.get("error", ""),
      str(s))

failed = [n for n, ok, _ in CHECKS if not ok]
print(f"[smoke] {len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
sys.exit(1 if failed else 0)
