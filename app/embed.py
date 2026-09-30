"""实物标准样品编组嵌入：在已冻结的最优审计流量上做完整整数容量分配。

语义：
- 只接受 status == "optimal" 的审计记录作为来源；冻结其站点顺序、管路
  顺序与每条已求得流量（即各管可用于实物批次的容量）。
- 每批样品不可拆分：枚举其（避开禁行管路、且冻结流量足以承载的）全部
  简单路径，按提交顺序逐批做带回溯的完整搜索——容量判断仅用于剪枝，
  不截断搜索空间，不以逐批最短路/贪心/有限尝试替代。
- 搜索顺序完全确定（批次按提交序、路径按 (站点序列, 管路标识) 字典序
  枚举），找到的第一组完整分配即规范解；同标识重传回放原记录，不改换
  来源或批次（否则拒绝）。
- 无法全部装入时，定位最先耗尽的搜索层（前缀不可行的第一批），稳定给出
  该层及之后的未安置批次、规范前缀安置下已占满的管路与逐管剩余容量。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from audit import fingerprint

MAX_BATCHES = 6
MAX_VOLUME = 10 ** 18

ALGORITHM = ("simple-path enumeration + complete backtracking search "
             "(exact integers; no per-batch shortest-path, greedy or "
             "bounded-retry substitution)")


class EmbedError(Exception):
    """编组请求校验错误，loc 为字段路径（如 batches[2].volume）。"""

    def __init__(self, message: str, loc: str):
        super().__init__(message)
        self.message = message
        self.loc = loc


class GroupConflict(Exception):
    """同编组标识但来源或批次不同。"""

    def __init__(self, message: str, existing: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.existing = existing


@dataclass(frozen=True)
class FrozenPipe:
    id: str
    frm: str
    to: str
    flow: int  # 已冻结的原流量，即可供实物批次使用的容量


@dataclass(frozen=True)
class FrozenNetwork:
    stations: Tuple[str, ...]           # 冻结的站点顺序
    pipes: Tuple[FrozenPipe, ...]       # 冻结的稳定管路顺序


@dataclass(frozen=True)
class Batch:
    id: str
    source: str
    target: str
    volume: int
    forbid: FrozenSet[str]              # 不得经过的管路标识


# ---------------------------------------------------------------- 冻结来源审计

def freeze_from_audit_record(record: Dict[str, Any]) -> FrozenNetwork:
    """从审计原记录冻结站点顺序、稳定管路顺序与每条已求得流量。

    只接受 status == "optimal" 的记录；其余（infeasible/invalid）拒绝。
    """
    status = record.get("status")
    if status != "optimal":
        raise EmbedError(
            f"只接受 status=optimal 的审计作为编组来源（实际 {status!r}）",
            "audit_id")
    result = record.get("result") or {}
    stations = tuple(n["station"] for n in result.get("node_check", []))
    pipes = tuple(
        FrozenPipe(e["id"], e["from"], e["to"], e["flow"])
        for e in result.get("edges", []))
    return FrozenNetwork(stations, pipes)


# ---------------------------------------------------------------- 批次校验

def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def validate_batches(raw: Any, frozen: FrozenNetwork) -> List[Batch]:
    if not isinstance(raw, list):
        raise EmbedError("batches 必须是数组", "batches")
    if not (1 <= len(raw) <= MAX_BATCHES):
        raise EmbedError(
            f"批次数量必须在 1..{MAX_BATCHES} 之间，实际 {len(raw)}", "batches")

    station_set = set(frozen.stations)
    pipe_set = {p.id for p in frozen.pipes}
    seen = set()
    batches: List[Batch] = []
    for i, rb in enumerate(raw):
        loc = f"batches[{i}]"
        if not isinstance(rb, dict):
            raise EmbedError("批次必须是对象", loc)
        bid = rb.get("id")
        if not isinstance(bid, str) or not bid or len(bid) > 32:
            raise EmbedError("批次 id 必须为 1..32 字符的字符串", f"{loc}.id")
        if bid in seen:
            raise EmbedError(f"批次 id 重复: {bid!r}", f"{loc}.id")
        seen.add(bid)

        source = rb.get("source")
        target = rb.get("target")
        if source not in station_set:
            raise EmbedError(f"未知来源站: {source!r}", f"{loc}.source")
        if target not in station_set:
            raise EmbedError(f"未知目标站: {target!r}", f"{loc}.target")
        if source == target:
            raise EmbedError("来源站与目标站不能相同", loc)

        volume = rb.get("volume")
        if not _is_int(volume):
            raise EmbedError("体积必须为整数（不接受浮点数/布尔值/字符串）",
                             f"{loc}.volume")
        if not (1 <= volume <= MAX_VOLUME):
            raise EmbedError(f"体积必须在 1..{MAX_VOLUME} 之间", f"{loc}.volume")

        forbid_raw = rb.get("forbid")
        if forbid_raw is None:
            forbid_raw = rb.get("forbidden_pipes", [])
        if not isinstance(forbid_raw, list):
            raise EmbedError("forbid 必须是管路标识数组", f"{loc}.forbid")
        forbid = set()
        for j, pid in enumerate(forbid_raw):
            if not isinstance(pid, str) or pid not in pipe_set:
                raise EmbedError(f"未知禁行管路: {pid!r}", f"{loc}.forbid[{j}]")
            forbid.add(pid)

        batches.append(Batch(bid, source, target, volume, frozenset(forbid)))
    return batches


# ---------------------------------------------------------------- 完整整数容量分配

def _enumerate_paths(adj: List[List[Tuple[int, int]]], src: int, dst: int
                     ) -> List[Tuple[Tuple[int, ...], Tuple[int, ...]]]:
    """枚举 src→dst 的全部简单路径。

    adj[u] = [(管路下标, 下游站点下标)]，调用前已按 (站点标识, 管路标识)
    字典序排序，故枚举顺序即规范顺序。路径 = (站点下标序列, 管路下标序列)。
    """
    paths: List[Tuple[Tuple[int, ...], Tuple[int, ...]]] = []
    cur_nodes = [src]
    cur_edges: List[int] = []
    visited = {src}

    def dfs(u: int) -> None:
        if u == dst:
            paths.append((tuple(cur_nodes), tuple(cur_edges)))
            return
        for ei, v in adj[u]:
            if v not in visited:
                visited.add(v)
                cur_nodes.append(v)
                cur_edges.append(ei)
                dfs(v)
                cur_edges.pop()
                cur_nodes.pop()
                visited.discard(v)

    dfs(src)
    return paths


def _complete_search(paths_per_batch: List[List[Tuple[Tuple[int, ...],
                                                    Tuple[int, ...]]]],
                     volumes: List[int],
                     flows: List[int]) -> Optional[List[int]]:
    """带回溯的完整整数容量分配。

    批次按提交顺序、路径按规范顺序逐一尝试；容量判断仅用于剪枝，搜索
    空间不被截断（无任何尝试次数上限）。返回各批所选路径下标（首个完整
    分配 = 规范解），不可行返回 None。
    """
    n = len(paths_per_batch)
    remaining = list(flows)
    assign = [-1] * n

    def dfs(k: int) -> bool:
        if k == n:
            return True
        vol = volumes[k]
        for pi, (_nodes, edge_idx) in enumerate(paths_per_batch[k]):
            if all(remaining[e] >= vol for e in edge_idx):
                for e in edge_idx:
                    remaining[e] -= vol
                assign[k] = pi
                if dfs(k + 1):
                    return True
                for e in edge_idx:
                    remaining[e] += vol
        return False

    return assign if dfs(0) else None


def _first_exhausted_layer(paths_per_batch, volumes, flows) -> int:
    """最先耗尽的搜索层：最小的 k 使前 k+1 批（下标 0..k）已无完整分配。

    前缀可行性单调（更多批次可行则其子集必可行），故该层唯一确定；
    对每一层前缀同样做完整搜索，不做近似。
    """
    for k in range(len(paths_per_batch)):
        if _complete_search(paths_per_batch[:k + 1],
                            volumes[:k + 1], flows) is None:
            return k
    raise AssertionError("整体不可行时必存在最先耗尽层")  # 防御：不可达


def _used_by_pipe(frozen: FrozenNetwork, volumes: List[int],
                  paths_per_batch, assign: List[int]) -> List[int]:
    used = [0] * len(frozen.pipes)
    for k, pi in enumerate(assign):
        if pi < 0:
            continue
        for e in paths_per_batch[k][pi][1]:
            used[e] += volumes[k]
    return used


def _usage_report(frozen: FrozenNetwork, used: List[int]) -> List[Dict[str, Any]]:
    return [{
        "id": p.id,
        "from": p.frm,
        "to": p.to,
        "flow": p.flow,
        "used": used[k],
        "remaining": p.flow - used[k],
    } for k, p in enumerate(frozen.pipes)]


def _path_report(frozen: FrozenNetwork, batch: Batch,
                 path: Tuple[Tuple[int, ...], Tuple[int, ...]]) -> Dict[str, Any]:
    nodes, edge_idx = path
    return {
        "id": batch.id,
        "source": batch.source,
        "target": batch.target,
        "volume": batch.volume,
        "stations": [frozen.stations[v] for v in nodes],
        "path": [frozen.pipes[e].id for e in edge_idx],
    }


def embed(frozen: FrozenNetwork, batches: List[Batch]) -> Dict[str, Any]:
    """完整整数容量分配主入口：成功给规范解，失败给最先耗尽层证据。"""
    index = {sid: i for i, sid in enumerate(frozen.stations)}
    flows = [p.flow for p in frozen.pipes]

    paths_per_batch = []
    for b in batches:
        adj: List[List[Tuple[int, int]]] = [[] for _ in frozen.stations]
        for k, p in enumerate(frozen.pipes):
            # 禁行管路剔除；冻结流量小于本批体积的管路永远不可能承载，剔除
            if p.id in b.forbid or p.flow < b.volume:
                continue
            adj[index[p.frm]].append((k, index[p.to]))
        for u in range(len(adj)):
            adj[u].sort(key=lambda x: (frozen.stations[x[1]],
                                       frozen.pipes[x[0]].id))
        paths_per_batch.append(
            _enumerate_paths(adj, index[b.source], index[b.target]))

    volumes = [b.volume for b in batches]
    assign = _complete_search(paths_per_batch, volumes, flows)
    if assign is not None:
        used = _used_by_pipe(frozen, volumes, paths_per_batch, assign)
        return {
            "status": "embedded",
            "algorithm": ALGORITHM,
            "batches": [
                _path_report(frozen, b, paths_per_batch[k][assign[k]])
                for k, b in enumerate(batches)],
            "pipe_usage": _usage_report(frozen, used),
            "evidence": {
                "capacity": "每条管路上各批占用之和 used 不超过冻结流量 flow，"
                            "剩余通量 remaining = flow - used，见 pipe_usage。",
                "completeness": "每批枚举全部可用简单路径后做带回溯的完整整数"
                                "容量分配；容量仅作剪枝，不以逐批最短路、贪心"
                                "或有限尝试替代。",
                "canonical": "批次按提交顺序、路径按 (站点序列, 管路标识) 字典序"
                             "枚举，首个完整分配即规范解，重算结果必然一致。",
            },
        }

    layer = _first_exhausted_layer(paths_per_batch, volumes, flows)
    prefix_assign = _complete_search(paths_per_batch[:layer],
                                     volumes[:layer], flows) or []
    prefix_used = _used_by_pipe(
        frozen, volumes[:layer],
        [paths_per_batch[k] for k in range(layer)], prefix_assign)
    usage = _usage_report(frozen, prefix_used)
    return {
        "status": "cannot_embed",
        "algorithm": ALGORITHM,
        "first_exhausted_layer": layer,
        "unplaced_batches": [{
            "id": b.id, "source": b.source, "target": b.target,
            "volume": b.volume,
        } for b in batches[layer:]],
        "placed_prefix": [
            _path_report(frozen, batches[k],
                         paths_per_batch[k][prefix_assign[k]])
            for k in range(layer)],
        "saturated_pipes": [u for u in usage
                            if u["flow"] > 0 and u["remaining"] == 0],
        "remaining_capacity": usage,
        "note": "最先耗尽搜索层 = 使前缀（按提交顺序）不可行的第一批；该层及"
                "之后的批次均无法安置。placed_prefix 为该层之前的规范安置，"
                "saturated_pipes 为该安置下剩余通量归零且原流量为正的管路，"
                "remaining_capacity 按冻结管路顺序给出全部剩余容量。",
    }


# ---------------------------------------------------------------- 编组幂等存储

class GroupStore:
    """稳定编组标识的幂等存储（落盘 JSON，服务/容器重启后仍可回放核对）。

    - 同 group_id + 同来源审计与同批次：返回首次原记录（replayed: true）。
    - 同 group_id + 改换来源或批次：抛 GroupConflict（由调用方转 409）。
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._records: Dict[str, Dict[str, Any]] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._records = data
            except (json.JSONDecodeError, OSError):
                self._records = {}

    def _flush_locked(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, self.path)

    @staticmethod
    def _fp(request: Dict[str, Any]) -> str:
        return fingerprint({
            "audit_id": request.get("audit_id"),
            "batches": request.get("batches"),
        })

    def get(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._records.get(group_id)
            return dict(rec) if rec is not None else None

    def lookup(self, group_id: str, request: Dict[str, Any]
               ) -> Optional[Dict[str, Any]]:
        """命中返回原记录；同标识改来源/批次抛 GroupConflict；未命中 None。"""
        fp = self._fp(request)
        with self._lock:
            rec = self._records.get(group_id)
            if rec is None:
                return None
            if rec["request_fingerprint"] != fp:
                raise GroupConflict(
                    f"编组标识 {group_id!r} 已用于不同来源或批次，拒绝复用。"
                    "同标识重传必须携带完全相同的来源审计与批次。",
                    existing={
                        "group_id": group_id,
                        "request_fingerprint": rec["request_fingerprint"],
                        "status": rec["status"],
                    })
            rec["replayed"] = True
            return dict(rec)

    def save(self, group_id: str, request: Dict[str, Any],
             frozen: FrozenNetwork, batches: List[Batch],
             status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        fp = self._fp(request)
        with self._lock:
            existing = self._records.get(group_id)
            if existing is not None:
                if existing["request_fingerprint"] != fp:
                    raise GroupConflict(
                        f"编组标识 {group_id!r} 已用于不同来源或批次，拒绝复用。",
                        existing={
                            "group_id": group_id,
                            "request_fingerprint":
                                existing["request_fingerprint"],
                            "status": existing["status"],
                        })
                existing["replayed"] = True
                return dict(existing)
            rec = {
                "group_id": group_id,
                "request_fingerprint": fp,
                "status": status,
                "replayed": False,
                "audit_id": request.get("audit_id"),
                "frozen": {
                    "stations": list(frozen.stations),
                    "pipes": [{"id": p.id, "from": p.frm, "to": p.to,
                               "flow": p.flow} for p in frozen.pipes],
                },
                "batches": [{
                    "id": b.id, "source": b.source, "target": b.target,
                    "volume": b.volume, "forbid": sorted(b.forbid),
                } for b in batches],
                "result": result,
            }
            self._records[group_id] = rec
            self._flush_locked()
            return dict(rec)
