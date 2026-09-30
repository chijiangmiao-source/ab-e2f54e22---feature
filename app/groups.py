"""稳定编组标识（group）的幂等存储。

与审计存储（audit.py）分离，落盘独立 JSON（默认 /data/groups.json）。

语义：
- 同一 group_id + 完全相同编组载荷（来源 audit_id 与 batches 均在内）：
  不重新做嵌入核验，直接回放首次原结论（replayed=true）。
- 同一 group_id 改换来源（audit_id）或改动批次：HTTP 409 拒绝，并回显
  已存来源与载荷指纹。
编组记录持久化，服务/容器重启后仍可重放与查询（GET /api/groups/<id>）。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, Optional

_FP_PREFIX = "﻿grp-v1"


def fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256((_FP_PREFIX + canonical).encode("utf-8")).hexdigest()


class GroupConflict(Exception):
    def __init__(self, message: str, existing: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.existing = existing


class GroupStore:
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

    def get(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = self._records.get(group_id)
            return dict(rec) if rec is not None else None

    def lookup(self, group_id: str, audit_id: str, payload: Any
               ) -> Optional[Dict[str, Any]]:
        """命中返回原记录；同 id 改来源/改批次抛 GroupConflict；未命中 None。"""
        fp = fingerprint(payload)
        with self._lock:
            rec = self._records.get(group_id)
            if rec is None:
                return None
            if rec["payload_fingerprint"] != fp:
                changed_source = rec.get("audit_id") != audit_id
                raise GroupConflict(
                    f"编组标识 {group_id!r} 已冻结于来源 "
                    f"{rec.get('audit_id')!r}，"
                    + ("本次改换了来源审计，拒绝复用；" if changed_source
                       else "本次改动了批次定义，拒绝复用；")
                    + "同一编组标识重传必须携带完全相同的来源与批次。",
                    existing={
                        "group_id": group_id,
                        "audit_id": rec.get("audit_id"),
                        "payload_fingerprint": rec["payload_fingerprint"],
                        "status": rec["status"],
                    })
            return dict(rec)

    def save(self, group_id: str, audit_id: str, payload: Any,
             status: str, result: Dict[str, Any]) -> Dict[str, Any]:
        fp = fingerprint(payload)
        with self._lock:
            existing = self._records.get(group_id)
            if existing is not None:
                if existing["payload_fingerprint"] != fp:
                    raise GroupConflict(
                        f"编组标识 {group_id!r} 已用于不同来源/批次，拒绝复用。",
                        existing={
                            "group_id": group_id,
                            "audit_id": existing.get("audit_id"),
                            "payload_fingerprint":
                                existing["payload_fingerprint"],
                            "status": existing["status"],
                        })
                return dict(existing)
            rec = {
                "group_id": group_id,
                "audit_id": audit_id,
                "payload_fingerprint": fp,
                "status": status,
                "result": result,
            }
            self._records[group_id] = rec
            self._flush_locked()
            return dict(rec)
