"""不可拆分实物批次向【已冻结 optimal 审计】的完整整数嵌入核验。

背景：一次最小费用流审计（audit）取得 status=optimal 后，其站点集合、稳定
管路顺序与每条管路的已求得流量即被**冻结**。工程师随后可提交一个稳定编组
（group）：至多 6 批不可拆分的标准样品，每批给定来源站、目标站、整数体积
以及不得经过的管路。本模块判定这些批次能否**同时**嵌入当次冻结流量。

硬性语义（不得以逐批最短路、贪心或有限尝试替代）：

1. 只接受 optimal 来源：从原审计结果重建冻结网络，容量即每条管路的原流量
   （批次总量在上界内逐管不得超过该流量）。
2. 每批在其（来源→目标、避开禁用管路的）**全部简单路径**之间择一行走，
   整批体积不可分地压在同一条路径的各管上。
3. 全部批次做联合的完整整数容量分配：按“可选路径数”升序分层，递归回溯 +
   后缀可行记忆（以逐层剩余容量向量为键），穷尽量级为各层路径数的完整
   笛卡尔积剪枝；找到的第一个解按规范路径序（路径长度、管路序号）确定，
   因而对同一输入稳定。
4. 成功：按批次给出规范路径、逐管占用与每条管路的剩余通量
   （冻结流量 − 批次占用合计）。
5. 失败：稳定报告搜索中最先耗尽的最深层起未安置的批次、该快照下已占满
   管路（占用 == 冻结流量）与全部管路剩余容量。

全程任意精度整数，无浮点数。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from solver import ValidationError

MAX_BATCHES = 6
MAX_SIMPLE_PATHS = 20000      # 单批简单路径枚举上限（显式拒绝，不静默截断）
MAX_SEARCH_NODES = 5_000_000  # 完整回溯节点安全闸（超限显式报错，不返回近似结论）


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True)
class FrozenPipe:
    id: str
    u: int
    v: int
    flow: int


@dataclass
class FrozenNetwork:
    audit_id: str
    ids: List[str]
    pipes: List[FrozenPipe]
    adj: List[List[Tuple[int, int]]]  # adj[u] = [(edge_index, v)]，按管路顺序

    @property
    def m(self) -> int:
        return len(self.pipes)


@dataclass(frozen=True)
class Batch:
    id: str
    src: int
    dst: int
    volume: int
    avoid: Tuple[str, ...]


@dataclass
class _Counters:
    nodes: int = 0
    limit_hit: bool = False
    critical_layer: int = -1
    critical_used: Optional[List[int]] = None


# ---------------------------------------------------------------- 冻结网络

def frozen_from_result(result: Dict[str, Any], audit_id: str) -> FrozenNetwork:
    """从原 optimal 审计的 result 体重建冻结网络。

    冻结的是节点（node_check 的稳定站点顺序）、管路（edges 的稳定标识与
    顺序）与每条管路的已求得流量。
    """
    if not isinstance(result, dict) or result.get("status") != "optimal":
        raise ValidationError(
            "来源审计不是 optimal 结果，服务只接受 optimal 来源进行冻结嵌入",
            "audit_id")

    node_check = result.get("node_check")
    edges = result.get("edges")
    if not isinstance(node_check, list) or not node_check:
        raise ValidationError("来源审计缺少 node_check，无法冻结站点", "audit_id")
    if not isinstance(edges, list) or not edges:
        raise ValidationError("来源审计缺少 edges，无法冻结管路", "audit_id")

    ids: List[str] = []
    seen: Set[str] = set()
    for i, node in enumerate(node_check):
        if not isinstance(node, dict) or not isinstance(node.get("station"), str):
            raise ValidationError("来源审计 node_check 结构损坏", f"node_check[{i}]")
        sid = node["station"]
        if sid in seen:
            raise ValidationError("来源审计站点重复，冻结数据非法", f"node_check[{i}]")
        if not node.get("ok", False):
            raise ValidationError(
                f"来源审计站点 {sid} 的净供需复算未通过，拒绝冻结", "audit_id")
        seen.add(sid)
        ids.append(sid)

    index = {sid: i for i, sid in enumerate(ids)}
    pipes: List[FrozenPipe] = []
    seen_eids: Set[str] = set()
    for i, e in enumerate(edges):
        loc = f"edges[{i}]"
        if not isinstance(e, dict):
            raise ValidationError("来源审计 edges 结构损坏", loc)
        eid = e.get("id")
        if not isinstance(eid, str) or eid in seen_eids:
            raise ValidationError("来源审计管路标识缺失或重复", f"{loc}.id")
        seen_eids.add(eid)
        u = index.get(e.get("from"))
        v = index.get(e.get("to"))
        if u is None or v is None:
            raise ValidationError("来源审计管路端点不在冻结站点集合内", loc)
        flow = e.get("flow")
        if not isinstance(flow, int) or isinstance(flow, bool):
            raise ValidationError("来源审计流量不是整数", f"{loc}.flow")
        lo = e.get("lo", 0)
        hi = e.get("hi", flow)
        if not (isinstance(lo, int) and isinstance(hi, int)) or not (lo <= flow <= hi):
            raise ValidationError(
                f"来源审计管路 {eid} 流量 {flow} 越界 [{lo}, {hi}]", f"{loc}.flow")
        pipes.append(FrozenPipe(eid, u, v, flow))

    adj: List[List[Tuple[int, int]]] = [[] for _ in ids]
    for k, p in enumerate(pipes):
        adj[p.u].append((k, p.v))  # 保留 edges 的稳定管路顺序
    return FrozenNetwork(audit_id=audit_id, ids=ids, pipes=pipes, adj=adj)


# ---------------------------------------------------------------- 批次校验

def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def parse_batches(payload: Any, net: FrozenNetwork) -> List[Batch]:
    if not isinstance(payload, dict):
        raise ValidationError("编组请求体必须是 JSON 对象", "$")
    raw = payload.get("batches")
    if not isinstance(raw, list):
        raise ValidationError("batches 必须是数组", "batches")
    if not (1 <= len(raw) <= MAX_BATCHES):
        raise ValidationError(
            f"每编组批次数必须在 1..{MAX_BATCHES} 之间，实际 {len(raw)}",
            "batches")

    pipe_ids = {p.id for p in net.pipes}
    batches: List[Batch] = []
    seen: Set[str] = set()
    for i, b in enumerate(raw):
        loc = f"batches[{i}]"
        if not isinstance(b, dict):
            raise ValidationError("批次必须是对象", loc)
        bid = b.get("id")
        if not isinstance(bid, str) or not bid or len(bid) > 32:
            raise ValidationError("批次标识须为 1..32 字符字符串", f"{loc}.id")
        if bid in seen:
            raise ValidationError(f"批次标识重复: {bid!r}", f"{loc}.id")
        seen.add(bid)

        src_s = b.get("from")
        dst_s = b.get("to")
        if src_s not in net.ids:
            raise ValidationError(f"来源站不在冻结审计站点中: {src_s!r}", f"{loc}.from")
        if dst_s not in net.ids:
            raise ValidationError(f"目标站不在冻结审计站点中: {dst_s!r}", f"{loc}.to")
        if src_s == dst_s:
            raise ValidationError("批次来源站与目标站不能相同", loc)

        volume = b.get("volume")
        if not _is_int(volume):
            raise ValidationError("批次体积必须为整数（不接受浮点数/布尔/字符串）",
                                  f"{loc}.volume")
        if volume <= 0:
            raise ValidationError("批次体积必须为正整数（不可拆分的实物批次）",
                                  f"{loc}.volume")
        if volume > 10 ** 18:
            raise ValidationError("批次体积过大（上限 1e18）", f"{loc}.volume")

        avoid_raw = b.get("avoid", [])
        if not isinstance(avoid_raw, list) or not all(
                isinstance(x, str) for x in avoid_raw):
            raise ValidationError("avoid 必须为管路标识字符串数组", f"{loc}.avoid")
        for pid in avoid_raw:
            if pid not in pipe_ids:
                raise ValidationError(f"禁经管路不在冻结审计管路中: {pid!r}",
                                      f"{loc}.avoid")
        batches.append(Batch(
            id=bid, src=net.ids.index(src_s), dst=net.ids.index(dst_s),
            volume=volume, avoid=tuple(sorted(set(avoid_raw)))))
    return batches


# ---------------------------------------------------------------- 简单路径枚举

def enumerate_simple_paths(net: FrozenNetwork, b: Batch) -> List[Tuple[int, ...]]:
    """枚举 b.src -> b.dst、不经过禁用管路的全部顶点简单路径。

    以边序号元组表示；按（长度、管路序号字典序）规范排序，保证结论稳定。
    """
    forbidden = {k for k, p in enumerate(net.pipes) if p.id in set(b.avoid)}
    paths: List[Tuple[int, ...]] = []
    visited = [False] * len(net.ids)
    visited[b.src] = True
    edge_path: List[int] = []

    def dfs(u: int) -> None:
        if u == b.dst:
            paths.append(tuple(edge_path))
            return
        if len(paths) >= MAX_SIMPLE_PATHS:
            return
        for (ek, v) in net.adj[u]:
            if ek in forbidden or visited[v]:
                continue
            visited[v] = True
            edge_path.append(ek)
            dfs(v)
            edge_path.pop()
            visited[v] = False

    dfs(b.src)
    if len(paths) >= MAX_SIMPLE_PATHS:
        raise ValidationError(
            f"批次 {b.id!r} 的简单路径数达到上限 {MAX_SIMPLE_PATHS}，"
            "为保证完整穷举（非有限尝试）拒绝求解；请增加禁经约束或缩小网络",
            "batches")
    paths.sort(key=lambda path: (len(path), path))
    return paths


# ---------------------------------------------------------------- 完整联合分配

def _complete_search(order: List[int],
                     paths: List[List[Tuple[int, ...]]],
                     volumes: List[int],
                     m: int,
                     capacity: List[int]) -> Tuple[bool, Dict[str, Any], List[int]]:
    """完整回溯 + 后缀可行记忆。

    返回 (是否成功, 调试信息, assignment)；assignment[i] 为批次 i 的路径。
    失败快照记录搜索中到达的最深耗尽层与其时占用向量。
    """
    k = len(order)
    used = [0] * m
    assignment: List[Optional[Tuple[int, ...]]] = [None] * k
    counters = _Counters()
    dead_states: Set[Tuple[int, Tuple[int, ...]]] = set()

    def snapshot_layer(pos: int) -> None:
        if pos > counters.critical_layer:
            counters.critical_layer = pos
            counters.critical_used = list(used)

    def feasible(pos: int) -> bool:
        counters.nodes += 1
        if counters.nodes > MAX_SEARCH_NODES:
            counters.limit_hit = True
            return False
        if pos == k:
            return True
        bi = order[pos]
        # 记忆键：当前层 + 全部管路剩余容量（容量只取决于已做分配）
        key = (pos, tuple(capacity[e] - used[e] for e in range(m)))
        if key in dead_states:
            return False
        for path in paths[bi]:  # 已按规范序排序
            vol = volumes[bi]
            fit = True
            for e in path:
                if used[e] + vol > capacity[e]:
                    fit = False
                    break
            if not fit:
                continue
            for e in path:
                used[e] += vol
            assignment[bi] = path
            if feasible(pos + 1):
                return True
            for e in path:
                used[e] -= vol
        assignment[bi] = None
        snapshot_layer(pos)
        dead_states.add(key)
        return False

    ok = feasible(0)
    info = {"nodes": counters.nodes, "limit_hit": counters.limit_hit,
            "critical_layer": counters.critical_layer,
            "critical_used": counters.critical_used}
    return ok, info, [a if a is not None else () for a in assignment]


# ---------------------------------------------------------------- 报告构造

def _pipe_usage_report(net: FrozenNetwork,
                       used: List[int],
                       by: List[List[Tuple[str, int]]]
                       ) -> List[Dict[str, Any]]:
    out = []
    for k, p in enumerate(net.pipes):
        out.append({
            "pipe": p.id,
            "from": net.ids[p.u],
            "to": net.ids[p.v],
            "frozen_flow": p.flow,
            "used": used[k],
            "remaining": p.flow - used[k],
            "by": [{"batch": bid, "amount": amt} for bid, amt in by[k]],
        })
    return out


def _build_success(net: FrozenNetwork, batches: List[Batch],
                   assignment: List[Tuple[int, ...]],
                   nodes: int) -> Dict[str, Any]:
    m = net.m
    used = [0] * m
    by: List[List[Tuple[str, int]]] = [[] for _ in range(m)]
    batch_reports = []
    for i, b in enumerate(batches):
        path = assignment[i]
        stations = [net.ids[b.src]]
        occupancy = []
        for ek in path:
            p = net.pipes[ek]
            used[ek] += b.volume
            by[ek].append((b.id, b.volume))
            occupancy.append({"pipe": p.id, "amount": b.volume})
            stations.append(net.ids[p.v])
        batch_reports.append({
            "id": b.id,
            "from": net.ids[b.src],
            "to": net.ids[b.dst],
            "volume": b.volume,
            "avoid": list(b.avoid),
            "path": {"stations": stations,
                     "pipes": [net.pipes[ek].id for ek in path]},
            "occupancy": occupancy,
        })
    return {
        "status": "embedded",
        "algorithm": "complete unsplittable simple-path integer allocation "
                     "(exhaustive backtracking + memoized suffix feasibility; "
                     "no greedy / shortest-path / bounded retries)",
        "frozen_audit_id": net.audit_id,
        "frozen_pipe_order": [p.id for p in net.pipes],
        "search": {"nodes": nodes, "layers": len(batches),
                   "backtracking": "exhaustive-with-memoization"},
        "batches": batch_reports,
        "pipe_usage": _pipe_usage_report(net, used, by),
        "note": "全部批次不可拆分地各走一条简单路径；任一管路批次占用合计"
                "均不超过该管路冻结流量，remaining 为剩余通量。",
    }


def _build_failure(net: FrozenNetwork, batches: List[Batch],
                   paths: List[List[Tuple[int, ...]]],
                   order: List[int],
                   info: Dict[str, Any]) -> Dict[str, Any]:
    if info["limit_hit"]:
        raise ValidationError(
            f"完整搜索超过节点安全闸 {MAX_SEARCH_NODES} 仍未穷尽，"
            "为避免以有限尝试替代完整分配，拒绝给出结论；请缩小路径空间",
            "batches")

    crit = info["critical_layer"]
    crit = max(crit, 0)
    used = info["critical_used"] or [0] * net.m
    unplaced_ids = [batches[order[j]].id for j in range(crit, len(order))]
    unplaced = []
    for j in range(crit, len(order)):
        bi = order[j]
        b = batches[bi]
        vol = b.volume
        open_paths = sum(
            1 for path in paths[bi]
            if all(used[e] + vol <= net.pipes[e].flow for e in path))
        unplaced.append({
            "id": b.id,
            "search_layer": j,
            "candidate_simple_paths": len(paths[bi]),
            "paths_still_fitting_snapshot": open_paths,
        })

    saturated = []
    for k, p in enumerate(net.pipes):
        if p.flow > 0 and used[k] >= p.flow:
            saturated.append({"pipe": p.id, "from": net.ids[p.u],
                              "to": net.ids[p.v], "frozen_flow": p.flow,
                              "used": used[k], "remaining": 0})
    # 快照占用发生在最深分支回退后，无法稳定归因到具体批次，故 by 留空，
    # 仅给每管占用总量与剩余容量。
    return {
        "status": "cannot_embed",
        "algorithm": "complete unsplittable simple-path integer allocation "
                     "(exhaustive backtracking + memoized suffix feasibility)",
        "frozen_audit_id": net.audit_id,
        "frozen_pipe_order": [p.id for p in net.pipes],
        "search": {"nodes": info["nodes"], "layers": len(batches),
                   "exhausted_layer": crit},
        "critical_layer": crit,
        "unplaced_batches": unplaced,
        "unplaced_batch_ids": unplaced_ids,
        "saturated_pipes": saturated,
        "pipe_usage": _pipe_usage_report(
            net, used, [[] for _ in range(net.m)]),
        "note": "已对各批全部简单路径的完整组合做穷尽整数分配（记忆剪枝），"
                "不存在可同时嵌入的路径组合；列出最深耗尽层起未安置的批次、"
                "该快照下占满的管路及各管剩余容量。",
    }


# ---------------------------------------------------------------- 入口

def embed(net: FrozenNetwork, batches: List[Batch]) -> Dict[str, Any]:
    paths = [enumerate_simple_paths(net, b) for b in batches]

    # 个体层面即无可用路径的批次先稳定报出（提交顺序）
    no_path = [batches[i].id for i, ps in enumerate(paths) if not ps]
    if no_path:
        return {
            "status": "cannot_embed",
            "algorithm": "complete unsplittable simple-path integer allocation",
            "frozen_audit_id": net.audit_id,
            "frozen_pipe_order": [p.id for p in net.pipes],
            "critical_layer": -1,
            "unplaced_batches": [
                {"id": bid, "search_layer": -1,
                 "candidate_simple_paths": 0,
                 "paths_still_fitting_snapshot": 0}
                for bid in no_path],
            "unplaced_batch_ids": no_path,
            "saturated_pipes": [],
            "pipe_usage": _pipe_usage_report(
                net, [0] * net.m, [[] for _ in range(net.m)]),
            "note": "下列批次在避开指定管路后不存在任何来源→目标简单路径"
                    "（或所有可达管路冻结流量为 0）。",
        }

    # 完整搜索的分层顺序：可选路径少的批次先安置（仅为剪枝；不影响完备性）
    order = sorted(range(len(batches)),
                   key=lambda i: (len(paths[i]), i))
    capacity = [p.flow for p in net.pipes]
    volumes = [b.volume for b in batches]
    ok, info, assignment = _complete_search(
        order, paths, volumes, net.m, capacity)
    if ok:
        return _build_success(net, batches, assignment, info["nodes"])
    return _build_failure(net, batches, paths, order, info)


def embed_payload(result: Dict[str, Any], audit_id: str,
                  payload: Dict[str, Any]) -> Dict[str, Any]:
    """服务端便捷入口：冻结审计结果 + 编组请求体 -> 结论体。"""
    net = frozen_from_result(result, audit_id)
    batches = parse_batches(payload, net)
    return embed(net, batches)
