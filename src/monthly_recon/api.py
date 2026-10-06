"""HTTP API：基于标准库 http.server 的 JSON 接口。"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from .caliber import CaliberVersion
from .service import MonthlyReconService, ReconError


def make_dispatch(service: MonthlyReconService):
    """把 (method, path, query, body) 映射为 (status, payload)，便于直接测试。"""

    def dispatch(method: str, path: str, query: dict, body: dict):
        seg = [s for s in path.strip("/").split("/") if s]

        if method == "GET" and seg == ["health"]:
            return 200, {"status": "ok"}

        if seg == ["venues"]:
            if method == "GET":
                return 200, {"venues": [v.to_dict() for v in service.venues.values()]}
            if method == "POST":
                venue = service.register_venue(str(body.get("venue_id", "")), str(body.get("name", "")))
                return 201, venue.to_dict()

        if seg == ["calibers"]:
            if method == "GET":
                return 200, {"calibers": [c.to_dict() for c in service.calibers.values()]}
            if method == "POST":
                caliber = service.register_caliber(CaliberVersion.from_dict(body))
                return 201, caliber.to_dict()

        if seg == ["snapshots"]:
            if method == "GET":
                snaps = service.list_snapshots(query.get("venue_id"), query.get("month"))
                return 200, {"snapshots": [s.to_dict() for s in snaps]}
            if method == "POST":
                snapshot = service.submit_snapshot(
                    venue_id=str(body.get("venue_id", "")),
                    month=str(body.get("month", "")),
                    reported=body.get("reported", {}),
                    activities=body.get("activities", []),
                    sessions=body.get("sessions", []),
                    checkins=body.get("checkins", []),
                    submitted_at=body.get("submitted_at"),
                )
                return 201, snapshot.to_dict()

        if seg == ["reconciliations", "run"] and method == "POST":
            cycle = service.run_reconciliation(str(body.get("month", "")), body.get("caliber_version"))
            return 200, cycle.to_dict()

        if len(seg) >= 2 and seg[0] == "reconciliations":
            month = seg[1]
            if len(seg) == 2 and method == "GET":
                return 200, service.get_cycle(month).to_dict()
            if len(seg) == 3 and seg[2] == "confirm" and method == "POST":
                service.confirm(
                    month,
                    venue_id=str(body.get("venue_id", "")),
                    actor=str(body.get("actor", "")),
                    note=str(body.get("note", "")),
                )
                return 200, service.get_cycle(month).to_dict()
            if len(seg) == 3 and seg[2] == "close" and method == "POST":
                report = service.close_month(month, actor=str(body.get("actor", "")))
                return 201, report.to_dict()

        if seg == ["adjustments"]:
            if method == "GET":
                events = service.list_adjustments(query.get("month"))
                return 200, {"adjustments": [e.to_dict() for e in events]}
            if method == "POST":
                event = service.post_adjustment(
                    type=str(body.get("type", "")),
                    month=str(body.get("month", "")),
                    posted_by=str(body.get("posted_by", "")),
                    reason=str(body.get("reason", "")),
                    payload=body.get("payload", {}),
                    posted_at=body.get("posted_at"),
                )
                return 201, event.to_dict()

        if len(seg) >= 2 and seg[0] == "reports":
            month = seg[1]
            if len(seg) == 2 and method == "GET":
                revision = int(query["revision"]) if "revision" in query else None
                return 200, service.get_report(month, revision).to_dict()
            if len(seg) == 3 and seg[2] == "recalculate" and method == "POST":
                report = service.recalculate(
                    month,
                    caliber_version=body.get("caliber_version"),
                    as_of=body.get("as_of"),
                )
                return 201, report.to_dict()
            if len(seg) == 3 and seg[2] == "drilldown" and method == "GET":
                revision = int(query["revision"]) if "revision" in query else None
                return 200, service.drilldown(
                    month,
                    metric=str(query.get("metric", "")),
                    venue_id=query.get("venue_id"),
                    revision=revision,
                )

        raise ReconError(f"未找到接口：{method} {path}", 404)

    return dispatch


def create_server(service: MonthlyReconService, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    dispatch = make_dispatch(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MonthlyRecon/0.1"

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                self._send(400, {"error": "请求体不是合法 JSON"})
                return
            try:
                status, payload = dispatch(method, parsed.path, dict(parse_qsl(parsed.query)), body)
            except ReconError as exc:
                status, payload = exc.status, {"error": exc.message}
            except (ValueError, KeyError, TypeError) as exc:
                status, payload = 400, {"error": str(exc)}
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, format, *args) -> None:  # 保持测试输出安静
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.dispatch = dispatch  # 便于测试与调试
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="月度活动统计对账后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = create_server(MonthlyReconService(), args.host, args.port)
    print(f"月度活动统计对账服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
