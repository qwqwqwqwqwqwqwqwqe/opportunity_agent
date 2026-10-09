from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

from .config import a2a_enabled, llm_api_base, llm_api_key, llm_model, opportunity_a2a_url
from .conversation_store import ConversationStore, DeletedConversation, RevisionConflict
from .lifecycle_agent import LifecycleAgent
from .session_service import SessionService
from .session_state import snapshot
from .resume_http import resume_http, service_for
from .chat_orchestrator import ChatOrchestratorAgent

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class OpportunityWebHandler(BaseHTTPRequestHandler):
    sessions: ClassVar[dict[str, LifecycleAgent]] = {}  # Compatibility only; disk is authoritative.
    store: ClassVar[ConversationStore] = ConversationStore()
    chat_orchestrator: ClassVar[ChatOrchestratorAgent | None] = ChatOrchestratorAgent() if a2a_enabled() else None

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if resume_http(self, "GET"):
                return
            if path in {"/", "/index.html", "/progress_ui.js", "/resume_ui.js", "/resume.css"}:
                filename = "index.html" if path == "/" else path.lstrip("/")
                content = (WEB_DIR / filename).read_bytes()
                self.send_response(HTTPStatus.OK)
                mime = "text/html" if filename.endswith("html") else "text/css" if filename.endswith("css") else "text/javascript"
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
            elif path == "/api/conversations":
                self._send_json(200, {"conversations": self.store.list()})
            elif path == "/api/a2a/health":
                endpoint = urlsplit(opportunity_a2a_url())
                health_url = f"{endpoint.scheme}://{endpoint.netloc}/health"
                try:
                    with urlopen(health_url, timeout=1.5) as response:  # noqa: S310 - configured loopback agent
                        detail = json.loads(response.read().decode("utf-8"))
                    self._send_json(200, {"available": True, "endpoint": opportunity_a2a_url(), "detail": detail})
                except Exception as exc:
                    self._send_json(503, {"available": False, "endpoint": opportunity_a2a_url(), "error": type(exc).__name__})
            elif path.startswith("/api/conversations/"):
                record = SessionService(self.store, self.chat_orchestrator).get(unquote(path.rsplit("/", 1)[-1]))
                if record:
                    self._send_json(200, record)
                else:
                    self._send_json(404, {"error": "conversation not found"})
            else:
                self.send_error(404)
        except DeletedConversation as exc:
            self._send_json(410, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(400 if path.startswith("/api/resume/") else 500, {"error": str(exc)})
        except (OSError, ValueError) as exc:
            self._send_json(500, {"error": str(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        try:
            if resume_http(self, "DELETE"):
                return
        except (ValueError, OSError) as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if not path.startswith("/api/conversations/"):
            self.send_error(404)
            return
        try:
            parts = [unquote(part) for part in path.split("/") if part]
            # /api/conversations/{session_id}/messages/{message_id}
            if len(parts) == 5 and parts[:2] == ["api", "conversations"] and parts[3] == "messages":
                if not parts[2] or not parts[4] or len(parts[4]) > 128:
                    raise ValueError("invalid message_id")
                result = SessionService(self.store, self.chat_orchestrator).delete_message(parts[2], parts[4])
                self._send_json(200, {"deleted": True, **result})
                return
            if len(parts) != 3 or parts[:2] != ["api", "conversations"]:
                self.send_error(404)
                return
            session_id = parts[2]
            deleted = self.store.delete(session_id)
            service_for(self.store).delete_session(session_id)
            self.sessions.pop(session_id, None)
            self._send_json(200, {"deleted": deleted})
        except (OSError, ValueError) as exc:
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path.startswith("/api/resume/"):
            try:
                if not resume_http(self, "POST"):
                    self.send_error(404)
            except DeletedConversation as exc:
                self._send_json(410, {"error": str(exc)})
            except RevisionConflict as exc:
                self._send_json(409, {"error": str(exc)})
            except (ValueError, TypeError, KeyError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception:
                self._send_json(500, {"error": "简历处理未完成，请重试或更换文件"})
            return
        if path not in {"/api/chat", "/api/onboarding", "/api/progress", "/api/timeline-update", "/api/roadmap/enrich",
                        "/api/roadmap/replan", "/api/official/research", "/api/conversations/import"} and not path.startswith("/api/confirmations/"):
            if path != "/api/a2a/retry":
                self.send_error(404)
                return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8 * 1024 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            session_id = payload.get("session_id")
            if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 128:
                raise ValueError("invalid session_id")
            service = SessionService(self.store, self.chat_orchestrator)
            if path == "/api/conversations/import":
                self._send_json(200, service.import_conversation(payload))
            elif path == "/api/a2a/retry":
                event_id = payload.get("event_id")
                if not isinstance(event_id, str) or not event_id:
                    raise ValueError("event_id is required")
                status, result = service.retry_a2a(session_id, event_id)
                self._send_json(status, result)
            else:
                status, result = service.execute(path, payload)
                self._send_json(status, result)
        except DeletedConversation as exc:
            self._send_json(410, {"error": str(exc)})
        except RevisionConflict as exc:
            self._send_json(409, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"处理未完成，原输入已保留：{type(exc).__name__}"})

    _state_snapshot = staticmethod(snapshot)

    def _send_json(self, status: int, payload: dict) -> None:
        if "profile" in payload:
            payload.update({
                "server_pid": os.getpid(), "server_llm_key_configured": bool(llm_api_key()),
                "server_modelscope_key_configured": bool(llm_api_key()),
                "server_llm_model": llm_model(), "server_llm_api_base": llm_api_base(),
                "a2a_enabled": self.chat_orchestrator is not None,
                "opportunity_a2a_url": opportunity_a2a_url(),
            })
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 8766), OpportunityWebHandler)
    print("Opportunity Agent UI: http://127.0.0.1:8766")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        resume_service = getattr(OpportunityWebHandler.store, "_resume_service", None)
        if resume_service:
            resume_service.close()


if __name__ == "__main__":
    main()
