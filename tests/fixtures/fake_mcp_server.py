"""A minimal fake MCP server (stdio, JSON-RPC 2.0) used ONLY by the test suite
to prove the MCP client can connect and call tools. Not part of the product.

It implements just enough of the protocol: initialize, notifications/initialized
(ignored), tools/list, tools/call.
"""
import json
import sys


def main():
    tools = [{
        "name": "echo",
        "description": "Echo a message back.",
        "inputSchema": {"type": "object",
                        "properties": {"msg": {"type": "string"}},
                        "required": ["msg"]},
    }]

    def handle(msg):
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "fake-mcp", "version": "0.0.1"}}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
        if method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments", {})
            if name == "echo":
                text = f"echo: {args.get('msg', '')}"
                return {"jsonrpc": "2.0", "id": mid,
                        "result": {"content": [{"type": "text", "text": text}],
                                   "isError": False}}
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Skip notifications (no id).
        if "id" not in msg:
            continue
        resp = handle(msg)
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
