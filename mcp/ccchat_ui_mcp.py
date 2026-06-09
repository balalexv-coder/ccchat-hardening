#!/usr/bin/env python3
"""Minimal stdio MCP server exposing ask_user_choice for ccchat sessions.

Protocol: JSON-RPC 2.0 over stdio (MCP). Implements just enough: initialize, tools/list,
tools/call. The tool writes the question to /workspace/.ui/ask_<id>.json and blocks until ccchat
writes /workspace/.ui/ans_<id>.json (the user's click), then returns it as the tool result.
"""
import json
import os
import sys
import time
import uuid

UI = "/workspace/.ui"
os.makedirs(UI, exist_ok=True)

TOOLS = [{
    "name": "ask_user_choice",
    "description": ("Ask the user to pick from options shown as real buttons in the ccchat UI. "
                    "Use whenever you offer a set of choices. Returns the chosen option text. "
                    "Set multi_select for several; allow_custom to let them type their own."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "multi_select": {"type": "boolean"},
            "allow_custom": {"type": "boolean"},
        },
        "required": ["question", "options"],
    },
}]


def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _ask(args):
    qid = uuid.uuid4().hex[:10]
    req = {"id": qid, "question": args.get("question", ""),
           "options": args.get("options", []),
           "multi_select": bool(args.get("multi_select")),
           "allow_custom": bool(args.get("allow_custom"))}
    with open(os.path.join(UI, f"ask_{qid}.json"), "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    ans_path = os.path.join(UI, f"ans_{qid}.json")
    # block until ccchat writes the answer (user clicked); cap at 1h
    end = time.time() + 3600
    while time.time() < end:
        if os.path.exists(ans_path):
            try:
                with open(ans_path, encoding="utf-8") as f:
                    ans = json.load(f)
            except Exception:
                ans = {"answer": ""}
            os.remove(ans_path)
            try:
                os.remove(os.path.join(UI, f"ask_{qid}.json"))
            except OSError:
                pass
            return ans.get("answer", "")
        time.sleep(0.3)
    return "(no answer / timed out)"


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = req.get("id")
        method = req.get("method")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ccchat-ui", "version": "1.0"}}})
        elif method == "notifications/initialized":
            pass  # notification, no reply
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params") or {}
            if params.get("name") == "ask_user_choice":
                answer = _ask(params.get("arguments") or {})
                _send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": answer}]}})
            else:
                _send({"jsonrpc": "2.0", "id": mid,
                       "error": {"code": -32601, "message": "unknown tool"}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    main()
