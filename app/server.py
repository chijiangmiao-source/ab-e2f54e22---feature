"""校准液路最小费用流服务（仅依赖 Python 标准库）。

路由：
  GET  /                     浏览器页面
  GET  /static/<file>        静态资源
  GET  /health               健康检查
  POST /api/solve            提交网络求解（支持 X-Audit-Id 幂等）
  GET  /api/records/<id>     查看审计原记录
  POST /api/embed            实物批次编组嵌入（基于已冻结的 optimal 审计，
                             支持 X-Group-Id / group_id 幂等回放）
  GET  /api/groups/<id>      查看编组原记录
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from audit import AuditError, AuditStore
from embed import (EmbedError, GroupConflict, GroupStore, embed,
                   freeze_from_audit_record, validate_batches)
from solver import ValidationError, solve_payload

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
AUDIT_DB = os.environ.get("AUDIT_DB", "/data/audit.json")
GROUP_DB = os.environ.get("GROUP_DB", "/data/groups.json")

_store = AuditStore(AUDIT_DB)
_groups = GroupStore(GROUP_DB)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "CalibrationFlow/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    # ------------------------------------------------------------ 工具
    def _send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name, content_type):
        path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(
                {"error": f"请求体不是合法 JSON: {exc}", "loc": "$"}, 400)
            return _MISSING

    # ------------------------------------------------------------ GET
    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self._send_json({"status": "ok", "service": "calibration-flow"})
        elif path == "/" or path == "/index.html":
            self._send_static("index.html", "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            name = unquote(path[len("/static/"):])
            if not _SAFE_NAME.match(name) or ".." in name:
                self._send_json({"error": "invalid path"}, 400)
                return
            ctype = {
                "app.js": "application/javascript; charset=utf-8",
                "style.css": "text/css; charset=utf-8",
            }.get(name, "application/octet-stream")
            self._send_static(name, ctype)
        elif path.startswith("/api/records/"):
            audit_id = unquote(path[len("/api/records/"):])
            rec = _store.get(audit_id)
            if rec is None:
                self._send_json({"error": "审计标识不存在", "audit_id": audit_id},
                                404)
            else:
                self._send_json(rec)
        elif path.startswith("/api/groups/"):
            group_id = unquote(path[len("/api/groups/"):])
            rec = _groups.get(group_id)
            if rec is None:
                self._send_json({"error": "编组标识不存在", "group_id": group_id},
                                404)
            else:
                self._send_json(rec)
        else:
            self._send_json({"error": "not found", "path": path}, 404)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/solve":
            self._handle_solve()
        elif path == "/api/embed":
            self._handle_embed()
        else:
            self._send_json({"error": "not found", "path": path}, 404)

    def _handle_solve(self):
        payload = self._read_json()
        if payload is _MISSING:
            return
        if not isinstance(payload, dict):
            self._send_json(
                {"error": "请求体必须是 JSON 对象 {stations, pipes}"}, 400)
            return

        audit_id = self.headers.get("X-Audit-Id") or payload.get("audit_id")
        if audit_id is not None and (not isinstance(audit_id, str) or
                                     not _SAFE_NAME.match(audit_id) or
                                     len(audit_id) > 64):
            self._send_json(
                {"error": "审计标识须为 <=64 字符的字母/数字/._- 组合"}, 400)
            return

        if audit_id is not None:
            try:
                hit = _store.lookup(audit_id, payload)
            except AuditError as exc:
                self._send_json(
                    {"error": exc.message, "conflict": exc.existing}, 409)
                return
            if hit is not None:
                self._send_json({
                    "audit_id": audit_id,
                    "replayed": True,
                    "status": hit["status"],
                    "payload_fingerprint": hit["payload_fingerprint"],
                    "result": hit["result"],
                }, 200, [("X-Idempotent-Replay", "true")])
                return

        try:
            result = solve_payload(payload)
        except ValidationError as exc:
            body = {"error": exc.message, "loc": exc.loc, "status": "invalid"}
            http_status, rec_status = 400, "invalid"
        except Exception as exc:  # 防御性：不吞掉状态
            self._send_json({"error": f"服务器内部错误: {exc!r}"}, 500)
            return
        else:
            body = result
            http_status, rec_status = 200, result["status"]

        if audit_id is not None:
            rec = _store.save(audit_id, payload, rec_status, body)
            self._send_json({
                "audit_id": audit_id,
                "replayed": False,
                "status": rec["status"],
                "payload_fingerprint": rec["payload_fingerprint"],
                "result": body,
            }, http_status)
        else:
            self._send_json(body, http_status)

    def _handle_embed(self):
        """实物批次编组嵌入：只接受 optimal 审计来源，冻结其站点、稳定
        管路顺序与每条已求得流量，对至多 6 批不可拆分样品做完整整数容量
        分配；同编组标识重传回放原结论，改换来源或批次拒绝(409)。"""
        payload = self._read_json()
        if payload is _MISSING:
            return
        if not isinstance(payload, dict):
            self._send_json(
                {"error": "请求体必须是 JSON 对象 {audit_id, group_id, batches}"},
                400)
            return

        audit_id = payload.get("audit_id")
        if (not isinstance(audit_id, str) or not _SAFE_NAME.match(audit_id)
                or len(audit_id) > 64):
            self._send_json(
                {"error": "来源审计标识 audit_id 须为 <=64 字符的字母/数字/._- 组合",
                 "loc": "audit_id"}, 400)
            return

        group_id = self.headers.get("X-Group-Id") or payload.get("group_id")
        if (not isinstance(group_id, str) or not _SAFE_NAME.match(group_id)
                or len(group_id) > 64):
            self._send_json(
                {"error": "编组标识 group_id 须为 <=64 字符的字母/数字/._- 组合",
                 "loc": "group_id"}, 400)
            return

        # 幂等键 = 来源审计 + 批次（编组标识本身不参与指纹）
        request = {"audit_id": audit_id, "batches": payload.get("batches")}
        try:
            hit = _groups.lookup(group_id, request)
        except GroupConflict as exc:
            self._send_json(
                {"error": exc.message, "conflict": exc.existing}, 409)
            return
        if hit is not None:
            self._send_json({
                "group_id": group_id,
                "audit_id": audit_id,
                "replayed": True,
                "status": hit["status"],
                "request_fingerprint": hit["request_fingerprint"],
                "result": hit["result"],
            }, 200, [("X-Idempotent-Replay", "true")])
            return

        record = _store.get(audit_id)
        if record is None:
            self._send_json(
                {"error": f"来源审计 {audit_id!r} 不存在", "audit_id": audit_id},
                404)
            return

        try:
            frozen = freeze_from_audit_record(record)
            batches = validate_batches(payload.get("batches"), frozen)
        except EmbedError as exc:
            self._send_json(
                {"error": exc.message, "loc": exc.loc, "status": "invalid"}, 400)
            return
        except Exception as exc:  # 防御性：不吞掉状态
            self._send_json({"error": f"服务器内部错误: {exc!r}"}, 500)
            return

        result = embed(frozen, batches)
        rec = _groups.save(group_id, request, frozen, batches,
                           result["status"], result)
        self._send_json({
            "group_id": group_id,
            "audit_id": audit_id,
            "replayed": False,
            "status": rec["status"],
            "request_fingerprint": rec["request_fingerprint"],
            "result": result,
        }, 200)


_MISSING = object()


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"calibration-flow listening on {host}:{port}, audit db {AUDIT_DB}, "
          f"group db {GROUP_DB}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
