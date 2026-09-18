"""造血干细胞捐献协同的运行入口。

- GET  /health：稳定的服务身份检查（保持向后兼容）。
- /api/*      ：协同后端 REST 接口（见 coordination.api）。

用法：
  python3 service.py --check           # 配置与领域自检
  python3 service.py --port 8000       # 启动服务
  python3 service.py --port 8000 --ledger data/ledger.jsonl
  python3 service.py --ledger data/ledger.jsonl \
      --timeline CASE-001 --tz Asia/Shanghai   # 导出监管复核时间线
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from coordination import SERVICE_ID, SERVICE_NAME
from coordination.events import Ledger
from coordination.service import CoordinationService, StubSmsChannel
from coordination.api import Api, ApiError


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_application(ledger_path: str = ""):
    """组装台账、应用服务与 HTTP 适配层。"""
    ledger = Ledger(ledger_path)
    channel = StubSmsChannel()  # 联调假通道；生产替换为真实短信/消息网关
    svc = CoordinationService(ledger, channel)
    return Api(svc)


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 协同业务接口。"""

    api = None  # 由 main 注入到类属性（测试可用 build_application 自行装配）

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, health_payload())
            return
        if parsed.path.startswith("/api/"):
            self._dispatch("GET", parsed, parse_qs(parsed.query), {})
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return
        self._dispatch("POST", parsed, parse_qs(parsed.query), body)

    def _dispatch(self, method, parsed, query, body):
        api = self.api or build_application(
            os.environ.get("LEDGER_PATH", ""))
        path_parts = [x for x in parsed.path.strip("/").split("/") if x]
        try:
            status, payload = api.handle(
                method, parsed.path, query, body, dict(self.headers))
        except ApiError as exc:
            self._send_json(exc.status,
                            {"error": exc.code, "message": exc.message})
            return
        except Exception as exc:  # 未预期错误不泄露堆栈
            self._send_json(500, {"error": "internal", "message": str(exc)})
            return
        self._send_json(status, payload)

    def log_message(self, *_args):
        return


def self_check():
    """领域自检：跑通一次最小 happy path 并验证哈希链。"""
    from coordination.models import Actor, Role
    from coordination.clock import Window

    ledger = Ledger("")
    svc = CoordinationService(ledger, StubSmsChannel())
    coord = Actor(Role.COORDINATOR, "coord-selfcheck")
    cid = svc.open_case(
        coord,
        donor_identity={"full_name": "自检供者", "contact_phone": "13800000000"},
        recipient_identity={"full_name": "自检受者"},
        parties={"donor": "D1", "recipient_hospital": "RH1",
                 "donor_center": "DC1", "courier": "C1"},
        case_id="CASE-SELFCHECK")
    assert health_payload()["service"] == SERVICE_ID
    assert Window.from_local("2026-09-20T08:00", "2026-09-20T14:00",
                             "Asia/Urumqi").duration_minutes() == 360
    assert ledger.verify_chain()["ok"]
    return {"case_id": cid, "events": len(ledger.all_events())}


def export_timeline(ledger_path: str, case_id: str, tz: str, as_json: bool):
    """从持久化台账导出某采集物的监管复核时间线。"""
    from coordination.audit import build_timeline, render_timeline_text
    from coordination.models import Role
    ledger = Ledger(ledger_path)
    try:
        timeline = build_timeline(ledger, case_id, viewer_tz=tz,
                                  viewer_role=Role.REGULATOR)
    except KeyError as exc:
        raise SystemExit(f"错误: {exc.args[0]}")
    if as_json:
        print(json.dumps(timeline, ensure_ascii=False, indent=2))
    else:
        print(render_timeline_text(timeline))
    return timeline


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ledger", default="", help="JSONL 台账持久化路径")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--timeline", default="",
                        help="导出指定病例/采集物的监管复核时间线后退出")
    parser.add_argument("--tz", default="Asia/Shanghai",
                        help="时间线展示时区（默认 Asia/Shanghai）")
    parser.add_argument("--json", action="store_true", help="时间线以 JSON 输出")
    args = parser.parse_args()
    if args.check:
        result = self_check()
        print(f"基础检查通过（{result['events']} 条领域事件，哈希链完好）")
        return
    if args.timeline:
        if not args.ledger:
            parser.error("--timeline 需要配合 --ledger 指定台账文件")
        export_timeline(args.ledger, args.timeline, args.tz, args.json)
        return
    Handler.api = build_application(args.ledger)
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
