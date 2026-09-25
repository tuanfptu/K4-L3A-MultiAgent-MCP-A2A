from __future__ import annotations

import asyncio
import json
import threading
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .cases import CaseSet, load_case_set
from .config import Settings
from .contracts import Contracts
from .mcp_gateway import EvidenceGateway, connect_gateway
from .trace import TraceWriter
from .workflow import solve_case

ASSET = Path(__file__).with_name("demo.html")
AGENTS_ASSET = Path(__file__).with_name("agents.html")
MAX_BODY_BYTES = 4096
RUN_LOCK = threading.Lock()


def _case_set(root: Path) -> CaseSet | None:
    try:
        return load_case_set(root)
    except (OSError, ValueError):
        return None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _trace_for_case(path: Path, case_id: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("case_id") == case_id:
            events.append(event)
    return events


def summary(root: Path) -> dict[str, Any]:
    case_set = _case_set(root)
    if case_set is None:
        return {
            "ready": False,
            "message": "Chưa có case-set.json và 100 input. Hãy chạy day09 validate-inputs.",
            "cases": [],
            "output_count": 0,
            "trace_count": 0,
        }
    cases = [
        {
            "case_id": case_id,
            "topic": next(
                (claim.get("topic", "") for claim in
                 case_set.cases[case_id].get("customer_request", {}).get("claims", [])
                 if isinstance(claim, dict)),
                "",
            ),
            "has_output": (root / "outputs" / f"{case_id}.json").is_file(),
        }
        for case_id in case_set.case_ids
    ]
    try:
        trace_count = sum(1 for line in (root / "traces" / "trace.jsonl").open(
            encoding="utf-8"
        ) if line.strip())
    except (OSError, UnicodeDecodeError):
        trace_count = 0
    return {
        "ready": True,
        "case_set_version": case_set.version,
        "cases": cases,
        "output_count": sum(case["has_output"] for case in cases),
        "trace_count": trace_count,
    }


def replay(root: Path, case_id: str) -> dict[str, Any]:
    case_set = _case_set(root)
    if case_set is None or case_id not in case_set.cases:
        raise ValueError("Case ID không thuộc bộ input đã xác thực")
    return {
        "mode": "replay",
        "case": case_set.cases[case_id],
        "output": _read_json(root / "outputs" / f"{case_id}.json"),
        "events": _trace_for_case(root / "traces" / "trace.jsonl", case_id),
        "evidence": [],
    }


class ObservedGateway:
    def __init__(self, gateway: EvidenceGateway) -> None:
        self.gateway = gateway
        self.evidence: list[dict[str, Any]] = []

    async def list_tools(self) -> list[str]:
        return await self.gateway.list_tools()

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        result = await self.gateway.call(tool_name, case_id=case_id, **arguments)
        self.evidence.append({
            "tool_name": tool_name,
            "domain": result["domain"],
            "evidence_ref": result["evidence_ref"],
            "data": result["data"],
            "warnings": result.get("warnings", []),
        })
        return result


async def _run_live(root: Path, case_id: str) -> dict[str, Any]:
    case_set = _case_set(root)
    if case_set is None or case_id not in case_set.cases:
        raise ValueError("Case ID không thuộc bộ input đã xác thực")
    settings = Settings.load(root)
    contracts = Contracts(root / "contracts" / "schemas")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    trace_path = root / "traces" / "demo" / f"{case_id}-{stamp}.jsonl"
    trace = TraceWriter(trace_path, contracts)
    case = case_set.cases[case_id]
    trace.emit(case_id=case_id, event_type="case_received", actor="coordinator")
    async with connect_gateway(settings.mcp_endpoint, settings.team_api_key, contracts) as gateway:
        observed = ObservedGateway(gateway)
        output = await solve_case(case, observed, trace)
        contracts.validate_output(output, "live demo output")
    if not observed.evidence or not output["evidence_refs"]:
        raise RuntimeError("MCP Gateway không trả evidence cho case này; hãy dùng Replay trace")
    trace.emit(case_id=case_id, event_type="case_finalized", actor="coordinator")
    return {
        "mode": "live",
        "case": case,
        "output": output,
        "events": _trace_for_case(trace_path, case_id),
        "evidence": observed.evidence,
    }


def run_live(root: Path, case_id: str) -> dict[str, Any]:
    if not RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("Một live demo khác đang chạy; hãy đợi hoàn tất")
    try:
        return asyncio.run(_run_live(root, case_id))
    finally:
        RUN_LOCK.release()


def make_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class DemoHandler(BaseHTTPRequestHandler):
        server_version = "L3ADemo/1.0"

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, value: dict[str, Any]) -> None:
            self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                self._send(200, ASSET.read_bytes(), "text/html; charset=utf-8")
            elif path in {"/agents", "/agents/", "/agents.html"}:
                self._send(200, AGENTS_ASSET.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/summary":
                self._json(200, summary(root))
            elif path.startswith("/api/case/"):
                case_id = path.removeprefix("/api/case/")
                try:
                    self._json(200, replay(root, case_id))
                except ValueError as exc:
                    self._json(404, {"error": str(exc)})
            else:
                self._json(404, {"error": "Không tìm thấy"})

        def do_POST(self) -> None:  # noqa: N802
            if urlparse(self.path).path != "/api/run":
                self._json(404, {"error": "Không tìm thấy"})
                return
            origin = self.headers.get("Origin")
            port = self.server.server_address[1]
            if origin and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                self._json(403, {"error": "Origin không được phép"})
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(415, {"error": "Content-Type phải là application/json"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > MAX_BODY_BYTES:
                    raise ValueError("Request body không hợp lệ")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or not isinstance(body.get("case_id"), str):
                    raise ValueError("Thiếu case_id")
                result = run_live(root, body["case_id"])
                self._json(200, result)
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})
            except RuntimeError as exc:
                self._json(409, {"error": str(exc)})
            except Exception:
                traceback.print_exc()
                self._json(502, {"error": "MCP Gateway không khả dụng; xem terminal để kiểm tra"})

        def log_message(self, format: str, *args: object) -> None:
            print(f"[demo] {self.address_string()} {format % args}")

    return DemoHandler


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    with ThreadingHTTPServer((host, port), make_handler(root.resolve())) as server:
        print(f"L3A demo: http://{host}:{port}")
        print("Press Ctrl+C to stop.")
        with suppress(KeyboardInterrupt):
            server.serve_forever()
