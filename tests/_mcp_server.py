"""Real SDK server for transport checks. No accounts, hardware, or model calls."""
import argparse
import asyncio
import base64
import io
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int)
parser.add_argument("--transport", default="stdio")
parser.add_argument("--counter")
args = parser.parse_args()
server = MCPServer("fixture", instructions="Call get_value before write_slowly.")


@server.tool()
def get_value(value: str) -> dict:
    """Return the supplied value."""
    return {"value": value}


@server.tool()
async def write_slowly() -> str:
    """Record a write, then delay the response."""
    path = Path(args.counter)
    count = int(path.read_text()) if path.exists() else 0
    path.write_text(str(count + 1))
    await asyncio.sleep(2)
    return "written"


@server.tool()
def get_image() -> CallToolResult:
    """Return an image without a text fallback."""
    from PIL import Image
    image = Image.new("RGB", (24, 24), "blue")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return CallToolResult(content=[ImageContent(type="image", data=base64.b64encode(buffer.getvalue()).decode(), mime_type="image/png")])


if args.transport == "stdio":
    server.run()
else:
    server.run(transport=args.transport, host="127.0.0.1", port=args.port)
