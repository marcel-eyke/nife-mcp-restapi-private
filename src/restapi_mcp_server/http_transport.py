"""SDK-backed MCP Streamable HTTP transport for the existing REST tools."""

import json
import os
from contextlib import asynccontextmanager

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Route


def _allowed_values(name, defaults):
    return [value.strip() for value in os.getenv(name, defaults).split(',') if value.strip()]


def create_http_app(rest_server, diagnostic_routes=()):
    """Expose generated tools without duplicating endpoint execution logic."""
    mcp_server = Server("restapi-mcp-server", version="1.0.2")

    @mcp_server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=tool["name"],
                description=tool["description"],
                inputSchema=tool["inputSchema"],
            )
            for tool in rest_server.tools.values()
        ]

    @mcp_server.call_tool()
    async def call_tool(name, arguments):
        result = await rest_server.handle_tool_call(name, arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, indent=2))],
            isError="error" in result,
        )

    security = TransportSecuritySettings(
        allowed_hosts=_allowed_values(
            "MCP_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*,[::1],[::1]:*"
        ),
        allowed_origins=_allowed_values(
            "MCP_ALLOWED_ORIGINS", "http://localhost:*,http://127.0.0.1:*,http://[::1]:*"
        ),
    )
    manager = StreamableHTTPSessionManager(
        app=mcp_server,
        json_response=True,
        stateless=False,
        security_settings=security,
    )

    # A callable object is treated as an ASGI endpoint by Starlette. Register
    # both spellings so MCP POST requests never depend on redirect handling.
    class MCPTransport:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    transport = MCPTransport()

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route("/mcp", transport, methods=["GET", "POST", "DELETE"]),
            Route("/mcp/", transport, methods=["GET", "POST", "DELETE"]),
            *diagnostic_routes,
        ],
        lifespan=lifespan,
    )
