"""Isolated offline server used only by ui_progress_smoke.cjs."""
import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
for key in ("LLM_API_KEY", "MODELSCOPE_API_KEY"):
    os.environ.pop(key, None)
os.environ["OPPORTUNITY_AGENT_DISABLE_DOTENV"] = "1"

from opportunity_agent.conversation_store import ConversationStore
from opportunity_agent.web_app import OpportunityWebHandler


with tempfile.TemporaryDirectory(prefix="progress-ui-") as directory:
    OpportunityWebHandler.store = ConversationStore(Path(directory) / "conversations.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpportunityWebHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(json.dumps({"port": server.server_port}), flush=True)
    try:
        sys.stdin.readline()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
