"""A stand-in for ``hermes acp`` with several sessions at once (V6 tests): speaks ACP JSON-RPC on stdio.

session/new returns a fresh id per call and remembers its cwd; prompts run concurrently (one thread each), like
Hermes' executor. Prompt text selects the behaviour:
    "write <name>"  -> creates <name> in the session's cwd, streams "wrote <name> in <cwd>"
    "slow"          -> waits for session/cancel, then stopReason "cancelled"
    "broken"        -> answers with Hermes' all-providers-failed wording (no tool calls)
    "permission"    -> asks session/request_permission and reports the chosen option
    anything else   -> echoes "echo: <text> (model <model>)"
session/set_model rejects model ids starting with "missing/".
"""

import json
import os
import sys
import threading

lock = threading.Lock()
sessions: dict = {}
cancels: dict = {}
responses: dict = {}
ready = threading.Condition()
counter = [0]


def send(obj):
    with lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def chunk(sid, text):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": {
        "sessionUpdate": "agent_message_chunk", "messageId": "m1", "content": {"type": "text", "text": text}}}})


def ask(sid):
    with lock:
        counter[0] += 1
        rid = 5000 + counter[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": "p1", "title": "npm run build", "kind": "execute",
                                       "rawInput": {"command": "npm run build", "description": "build"}},
        "options": [{"optionId": "yes", "kind": "allow_once"}, {"optionId": "no", "kind": "reject_once"}]}})
    with ready:
        ready.wait_for(lambda: rid in responses, timeout=10)
        return responses.pop(rid, {})


def prompt(req_id, params):
    sid, text = params["sessionId"], params["prompt"][0]["text"]
    stop = "end_turn"
    if text.startswith("write "):
        name = text.split(" ", 1)[1]
        with open(os.path.join(sessions[sid]["cwd"], name), "w") as fh:
            fh.write("written by " + sid)
        chunk(sid, f"wrote {name} in {sessions[sid]['cwd']}")
    elif text == "slow":
        chunk(sid, "working")
        cancels[sid].wait(10)
        stop = "cancelled"
    elif text == "broken":
        chunk(sid, "OpenRouter sent back an empty or broken reply 3 times. Your request was not processed.")
    elif text == "permission":
        outcome = ask(sid).get("result", {}).get("outcome", {})
        chunk(sid, f"permission={outcome.get('optionId') or outcome.get('outcome')}")
    else:
        chunk(sid, f"echo: {text} (model {sessions[sid].get('model') or 'main'})")
    send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": stop, "usage": {"totalTokens": 42}}})


def main():
    for line in sys.stdin:
        msg = json.loads(line)
        if "method" not in msg:  # a response to our permission request
            with ready:
                responses[msg["id"]] = msg
                ready.notify_all()
            continue
        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentInfo": {"name": "fake-team"}}})
        elif method == "session/new":
            with lock:
                counter[0] += 1
                sid = f"eng-{counter[0]}"
            sessions[sid], cancels[sid] = {"cwd": params["cwd"]}, threading.Event()
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": sid}})
        elif method == "session/set_mode":
            sessions[params["sessionId"]]["mode"] = params["modeId"]
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/set_model":
            if params["modelId"].startswith("missing/"):
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "model not found"}})
            else:
                sessions[params["sessionId"]]["model"] = params["modelId"]
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif method == "session/prompt":
            threading.Thread(target=prompt, args=(rid, params), daemon=True).start()
        elif method == "session/cancel":
            cancels.get(params.get("sessionId"), threading.Event()).set()


if __name__ == "__main__":
    main()
