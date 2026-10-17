"""A tiny stand-in for ``hermes acp`` used by the tests: speaks ACP JSON-RPC on stdio.

Prompt text selects the behaviour:
    "hello"       -> streams "Hel" + "lo!" and ends the turn
    "multi"       -> two assistant messages with different message ids
    "permission"  -> asks session/request_permission, reports the chosen option
    "edit"        -> asks an edit permission (new file) and reports the chosen option
    "slow"        -> waits for session/cancel, then ends with stopReason "cancelled"
    "crash"       -> exits the process mid-turn
    "silent"      -> ends the turn without any text
    "refuse"      -> answers stopReason "refusal" with no text
Sessions: "known-session" can be loaded; anything else loads as null.
"""

import json
import sys
import threading

write_lock = threading.Lock()
cancelled = threading.Event()
responses: dict = {}
response_ready = threading.Condition()
next_id = [1000]


def send(obj):
    with write_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def chunk(session_id, text, message_id="m1"):
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {"sessionUpdate": "agent_message_chunk", "messageId": message_id,
                       "content": {"type": "text", "text": text}},
        },
    })


def ask_permission(session_id, tool_call, options):
    next_id[0] += 1
    rid = next_id[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
          "params": {"sessionId": session_id, "toolCall": tool_call, "options": options}})
    with response_ready:
        response_ready.wait_for(lambda: rid in responses, timeout=10)
        return responses.pop(rid, None)


OPTIONS = [
    {"optionId": "allow_once", "kind": "allow_once", "name": "Allow once"},
    {"optionId": "allow_session", "kind": "allow_always", "name": "Allow for session"},
    {"optionId": "deny", "kind": "reject_once", "name": "Deny"},
]


def run_prompt(req_id, params):
    sid = params["sessionId"]
    text = params["prompt"][0]["text"]
    stop = "end_turn"
    if sid == "forgotten-session":  # loaded fine earlier, but Hermes no longer knows it (seen live)
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "refusal"}})
        return
    if text == "hello":
        chunk(sid, "Hel")
        chunk(sid, "lo!")
    elif text == "multi":
        chunk(sid, "First.", "m1")
        chunk(sid, "Second.", "m2")
    elif text == "permission":
        result = ask_permission(sid, {"toolCallId": "perm-1", "title": "rm -rf build", "kind": "execute",
                                      "rawInput": {"command": "rm -rf build", "description": "delete build dir"}},
                                OPTIONS)
        outcome = (result or {}).get("result", {}).get("outcome", {})
        chunk(sid, f"outcome={outcome.get('outcome')}:{outcome.get('optionId')}")
    elif text == "edit":
        result = ask_permission(sid, {"toolCallId": "edit-1", "title": "Approve edit: notes.txt", "kind": "edit",
                                      "content": [{"type": "diff", "path": params.get("_path", "notes.txt"),
                                                   "oldText": None, "newText": "hi"}]},
                                [OPTIONS[0], OPTIONS[2]])
        outcome = (result or {}).get("result", {}).get("outcome", {})
        chunk(sid, f"outcome={outcome.get('outcome')}:{outcome.get('optionId')}")
    elif text == "slow":
        cancelled.clear()
        chunk(sid, "Working")
        cancelled.wait(10)
        stop = "cancelled"
    elif text == "crash":
        chunk(sid, "about to crash")
        sys.stdout.flush()
        import os
        os._exit(3)
    elif text == "failing-tool":
        update = {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "web_extract: https://x.example",
                  "kind": "fetch", "status": "in_progress"}
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})
        failed = {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "failed",
                  "content": [{"type": "content", "content": {"type": "text", "text": "Error: HTTP 403 Forbidden"}}]}
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": failed}})
        chunk(sid, "The page refused access.")
    elif text == "silent":
        pass
    elif text == "refuse":
        stop = "refusal"
    else:
        chunk(sid, f"echo: {text}")
    send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": stop}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        if method is None:  # a response to one of our requests
            with response_ready:
                responses[msg["id"]] = msg
                response_ready.notify_all()
            continue
        params = msg.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": 1, "agentInfo": {"name": "fake-hermes", "version": "9.9.9"},
                "agentCapabilities": {"loadSession": True}}})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "new-session"}})
        elif method == "session/load":
            if params.get("sessionId") in ("known-session", "forgotten-session"):
                chunk("known-session", "replayed history")  # must be ignored by the client
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        elif method == "session/prompt":
            threading.Thread(target=run_prompt, args=(msg["id"], params), daemon=True).start()
        elif method == "session/cancel":
            cancelled.set()
        elif "id" in msg:
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "nope"}})


if __name__ == "__main__":
    main()
