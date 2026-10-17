"""Worker-side MCP: let one persistent teammate send a narrow message to
another named teammate, without routing through the lead.

Started once per worker by the bridge with the session's dashboard base URL
as argv[1]; every worker gets the same `team_message` tool.
"""
import json
import sys
import urllib.request

tool = {
    "name": "team_message",
    "description": "Send a concise message to another named teammate. It steers their active task or opens a follow-up on their existing thread. Use only when requested or necessary to coordinate; never send to yourself.",
    "inputSchema": {"type": "object", "properties": {
        "name": {"type": "string"}, "message": {"type": "string"}},
        "required": ["name", "message"]},
    "annotations": {"readOnlyHint": False, "destructiveHint": False,
                    "openWorldHint": False}}


def handle(msg, base, origin):
    if "id" not in msg:
        return None
    method = msg.get("method")
    params = msg.get("params", {})
    try:
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "codex-team-bus", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": [tool]}
        elif method == "ping":
            result = {}
        elif method == "tools/call":
            try:
                if params["name"] != "team_message":
                    raise ValueError("Unknown tool")
                args = params["arguments"]
                payload = json.dumps({"op": "send", "name": args["name"],
                                      "message": args["message"]}).encode()
                req = urllib.request.Request(
                    base + "action", data=payload,
                    headers={"Content-Type": "application/json", "Origin": origin})
                with urllib.request.urlopen(req, timeout=50) as response:
                    ack = response.read().decode()
                result = {"content": [{"type": "text", "text": ack}]}
            except Exception as e:
                result = {"isError": True,
                          "content": [{"type": "text", "text": str(e)}]}
        else:
            raise ValueError("Unknown method")
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32603, "message": str(e)}}


def main():
    base = sys.argv[1]
    origin = base.split("/")[0] + "//" + base.split("/")[2]
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = handle(msg, base, origin)
        if reply is not None:
            print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
