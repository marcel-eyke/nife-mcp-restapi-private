"""Exercise the real SDK transport without contacting an external REST API."""

import asyncio
import json
import socket
import subprocess
import sys

import pytest
from starlette.testclient import TestClient

from restapi_mcp_server.server import RestApiMCPServer
from restapi_mcp_server.rest_client import RestClient


@pytest.fixture
def server(tmp_path, monkeypatch):
    spec = tmp_path / "openapi.json"
    spec.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1"},
        "paths": {"/items/{id}": {"get": {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "integer"}}],
            "responses": {"200": {"description": "OK"}},
        }}},
    }))
    monkeypatch.setenv("OPENAPI_SPEC_URL", str(spec))
    monkeypatch.setenv("REST_API_BASE_URL", "http://api.invalid")
    monkeypatch.setenv("AUTH_TYPE", "none")
    monkeypatch.setenv("MCP_MODE", "http")
    monkeypatch.setenv("ENABLE_HTTP_ENDPOINT", "true")
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MCP_ALLOWED_ORIGINS", raising=False)
    server = RestApiMCPServer()
    assert asyncio.run(server.initialize())
    return server


def initialize(client, path="/mcp"):
    response = client.post(path, headers={"Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test-client", "version": "1"}},
    })
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert "tools" in result["capabilities"]
    headers = {
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": response.headers["Mcp-Session-Id"],
        "MCP-Protocol-Version": result["protocolVersion"],
    }
    notification = client.post(path, headers=headers, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert notification.status_code == 202
    assert not notification.content
    return headers


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_mcp_session_and_generated_tool(server, monkeypatch, path):
    calls = []

    def execute(self, **kwargs):
        calls.append(kwargs)
        return {"id": 7, "name": "Example"}

    monkeypatch.setattr(RestClient, "execute_request", execute)
    with TestClient(server.create_http_app(), base_url="http://localhost") as client:
        headers = initialize(client, path)
        response = client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        })
        tools = response.json()["result"]["tools"]
        tool = next(t for t in tools if t["name"].startswith("get_items"))
        response = client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": tool["name"], "arguments": {"id": 7}},
        })
        result = response.json()["result"]
        assert result["isError"] is False
        assert json.loads(result["content"][0]["text"])["data"]["id"] == 7
        assert calls == [{"method": "get", "path": "/items/7", "query_params": {}, "body": None}]
        # SDK validates required arguments before invoking the REST client.
        response = client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": tool["name"], "arguments": {}},
        })
        assert response.json()["result"]["isError"] is True
        assert len(calls) == 1
        assert client.delete(path, headers=headers).status_code == 200
        assert client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 5, "method": "tools/list",
        }).status_code == 404


def test_tool_failure_is_reported(server, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr(RestClient, "execute_request", fail)
    with TestClient(server.create_http_app(), base_url="http://localhost") as client:
        headers = initialize(client)
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "execute_custom_request", "arguments": {"method": "GET", "path": "/items"}},
        })
        result = response.json()["result"]
        assert result["isError"] is True
        assert "upstream unavailable" in result["content"][0]["text"]


def test_diagnostics_and_transport_validation(server):
    with TestClient(server.create_http_app(), base_url="http://localhost") as client:
        for path in ["/", "/health", "/metrics", "/schema", "/tools", "/api/endpoints"]:
            assert client.get(path).status_code == 200
        assert client.get("/").json()["mode"] == "http"
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        assert client.post("/mcp", headers=headers, content="{").status_code == 400
        assert client.post("/mcp", headers={**headers, "Origin": "https://evil.invalid"},
                           json={}).status_code == 403
        assert client.post("/mcp", headers={**headers, "Host": "evil.invalid"},
                           json={}).status_code == 421
        assert client.post("/mcp", headers={**headers, "Mcp-Session-Id": "unknown"},
                           json={}).status_code == 404
        assert client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }).status_code == 400


def test_external_host_can_be_configured(server, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://mcp.example.com")
    with TestClient(server.create_http_app(), base_url="https://mcp.example.com") as client:
        initialize(client)


def test_diagnostics_can_be_disabled(server):
    server.enable_http = False
    with TestClient(server.create_http_app(), base_url="http://localhost") as client:
        assert client.get("/health").status_code == 404
        initialize(client)


def test_stdio_cli_still_works(server):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "health_check"}},
    ]
    process = subprocess.run(
        [sys.executable, "-m", "restapi_mcp_server", "--mode", "stdio"],
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=15,
    )
    assert process.returncode == 0, process.stderr
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert responses[1]["result"]["tools"]
    assert "content" in responses[2]["result"]


@pytest.mark.asyncio
async def test_official_client_over_real_http(server):
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # Let the OS choose a free port and keep the socket reserved until Uvicorn
    # takes over, avoiding a race between selecting and opening the test port.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        http_server = uvicorn.Server(uvicorn.Config(
            server.create_http_app(), log_level="warning", timeout_graceful_shutdown=2,
        ))
        task = asyncio.create_task(http_server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not http_server.started:
                    if task.done():
                        await task
                        pytest.fail("HTTP server stopped before startup")
                    await asyncio.sleep(0.01)
                async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, session_id):
                    async with ClientSession(read, write) as client:
                        initialized = await client.initialize()
                        assert initialized.serverInfo.name == "restapi-mcp-server"
                        assert session_id()
                        tools = await client.list_tools()
                        assert any(tool.name == "health_check" for tool in tools.tools)
                        result = await client.call_tool("health_check", {})
                        assert not result.isError
                        assert json.loads(result.content[0].text)["schema_loaded"] is True
        finally:
            http_server.should_exit = True
            await asyncio.wait_for(task, timeout=10)
