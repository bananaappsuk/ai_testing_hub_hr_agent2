"""Local browser chat for the HR policy agent.

The equivalent of the n8n chat trigger's hosted chat page. Binds to localhost
only — it carries no authentication and speaks to the policy document with your
service account, so it is not safe to expose on a network.

    python -m hr_agent.web
"""

from __future__ import annotations

import os
import threading
import uuid

from flask import Flask, jsonify, render_template, request

from hr_agent import config
from hr_agent.agent import HRPolicyAgent

app = Flask(__name__)

# One agent per browser session, so conversations do not bleed into each other.
_sessions: dict[str, HRPolicyAgent] = {}
_lock = threading.Lock()


def _agent_for(session_id: str) -> HRPolicyAgent:
    with _lock:
        agent = _sessions.get(session_id)
        if agent is None:
            agent = HRPolicyAgent()
            _sessions[session_id] = agent
        return agent


@app.get("/")
def index():
    return render_template(
        "index.html",
        model=config.AGENT_MODEL,
        variant=config.PROMPT_VARIANT,
    )


@app.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    session_id = payload.get("session_id") or str(uuid.uuid4())

    if not message:
        return jsonify(error="Empty message."), 400

    try:
        reply = _agent_for(session_id).ask(message)
    except Exception as exc:  # shown in the UI rather than a blank failure
        app.logger.exception("agent error")
        return jsonify(error=f"{type(exc).__name__}: {exc}"), 500

    return jsonify(reply=reply, session_id=session_id)


@app.post("/reset")
def reset():
    session_id = (request.get_json(silent=True) or {}).get("session_id")
    with _lock:
        _sessions.pop(session_id, None)
    return jsonify(ok=True)


def main() -> None:
    port = int(os.environ.get("WEB_PORT", "8000"))
    print(f"HR policy agent ({config.AGENT_MODEL}) → http://127.0.0.1:{port}")
    # host is pinned to loopback deliberately; see the module docstring.
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
