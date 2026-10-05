"""标准库 HTTP API：月度活动统计对账后端服务。

启动：python3 -m reconciliation.api --db reconciliation.db --port 8080
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .models import ConflictError, DomainError, NotFoundError
from .service import ReconciliationService


def _build_routes(service: ReconciliationService) -> list:
    """(方法, 路径正则, 处理函数) 路由表。处理函数接收 body/query 与路径参数。"""
    return [
        ("POST", r"/venues",
         lambda body, query: service.add_venue(**body)),
        ("POST", r"/activities",
         lambda body, query: service.add_activity(**body)),
        ("POST", r"/check-ins",
         lambda body, query: service.add_check_in(**body)),
        ("POST", r"/venue-reports",
         lambda body, query: service.submit_venue_report(**body)),
        ("POST", r"/snapshots",
         lambda body, query: service.capture_snapshot()),
        ("POST", r"/calibers",
         lambda body, query: service.register_caliber(**body)),
        ("GET", r"/calibers",
         lambda body, query: {"calibers": service.list_calibers()}),
        ("POST", r"/adjustments",
         lambda body, query: service.record_adjustment(**body)),
        ("GET", r"/adjustments",
         lambda body, query: {"adjustments": service.list_adjustments(period=query.get("period"))}),
        ("POST", r"/runs",
         lambda body, query: service.create_run(**body)),
        ("GET", r"/runs/(?P<run_id>[\w-]+)",
         lambda body, query, run_id: service.get_run(run_id)),
        ("GET", r"/runs/(?P<run_id>[\w-]+)/differences",
         lambda body, query, run_id: {"differences": service.list_differences(run_id)}),
        ("POST", r"/differences/(?P<difference_id>[\w-]+)/confirm",
         lambda body, query, difference_id: service.confirm_difference(difference_id, **body)),
        ("POST", r"/runs/(?P<run_id>[\w-]+)/close",
         lambda body, query, run_id: service.close_run(run_id)),
        ("GET", r"/reports",
         lambda body, query: service.get_report(query["venue_id"], query["month"])),
        ("GET", r"/reports/(?P<report_id>[\w-]+)",
         lambda body, query, report_id: service.get_report_by_id(report_id)),
        ("GET", r"/reports/(?P<report_id>[\w-]+)/drilldown",
         lambda body, query, report_id: service.report_drilldown(report_id, query["metric"])),
        ("POST", r"/recalculations",
         lambda body, query: service.recalculate(**body)),
    ]


def create_server(service: ReconciliationService, host: str = "127.0.0.1",
                  port: int = 8080) -> ThreadingHTTPServer:
    routes = _build_routes(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "MonthlyReconciliation/0.1"

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _payload(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _handle(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                for route_method, pattern, func in routes:
                    if route_method != method:
                        continue
                    match = re.fullmatch(pattern, path)
                    if match:
                        body = self._payload() if method == "POST" else {}
                        self._send(200, func(body=body, query=query, **match.groupdict()))
                        return
                self._send(404, {"error": "接口不存在"})
            except NotFoundError as exc:
                self._send(404, {"error": str(exc)})
            except ConflictError as exc:
                self._send(409, {"error": str(exc)})
            except (ValueError, KeyError, DomainError) as exc:
                self._send(400, {"error": str(exc)})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="月度活动统计对账后端服务")
    parser.add_argument("--db", default="reconciliation.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    service = ReconciliationService(args.db)
    server = create_server(service, args.host, args.port)
    print(f"月度活动统计对账服务已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()


if __name__ == "__main__":
    main()
