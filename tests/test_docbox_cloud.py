"""DOCBOX Cloud auth against a synthetic HTTPS API, including real MCP calls."""
import asyncio
import json
import logging
import os
from pathlib import Path
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from restapi_mcp_server.rest_client import RestClient
from restapi_mcp_server.server import RestApiMCPServer
from test_http_transport import initialize

API_KEY = 'SYNTHETIC-DOCBOX-KEY'
CLOUD_ID = 'SYNTHETIC-CLOUD-ID'


@pytest.fixture(scope='module')
def api(tmp_path_factory):
    directory = tmp_path_factory.mktemp('docbox-tls')
    cert, key = directory / 'cert.pem', directory / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.path, dict(self.headers)))
            if self.path.endswith('/redirect'):
                self.send_response(302)
                self.send_header('Location', f'https://127.0.0.1:{self.server.server_port}/api/v2/steal')
                self.end_headers()
                return
            valid = self.headers.get('API-Key') == API_KEY and self.headers.get('Cloud-ID') == CLOUD_ID
            self.send_response(200 if valid else 401)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            # Deliberately echo credentials on failure: errors must suppress them.
            body = {'archives': []} if valid else {'error': API_KEY, 'cloud': CLOUD_ID}
            self.wfile.write(json.dumps(body).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f'https://127.0.0.1:{server.server_port}/api/v2', cert, observed
    server.shutdown()
    worker.join()
    server.server_close()


def client(api, **overrides):
    base, cert, _ = api
    result = RestClient(base, {'type': 'docbox_cloud', 'value': API_KEY,
                               'cloud_id': CLOUD_ID, **overrides})
    result.session.trust_env = False
    result.session.verify = str(cert)
    return result


def test_both_headers_on_real_https_requests(api):
    base, _, observed = api
    result = client(api).get('/archivestructure')
    assert result == {'archives': []}
    path, headers = observed[-1]
    assert path == '/api/v2/archivestructure'
    assert headers['API-Key'] == API_KEY
    assert headers['Cloud-ID'] == CLOUD_ID


def test_no_credential_override_foreign_origin_or_redirect(api):
    rest = client(api)
    with patch.object(rest.session, 'request') as request:
        for header in ['api-key', 'CLOUD-ID', 'Host', 'Authorization']:
            with pytest.raises(ValueError, match='server-owned'):
                rest.get('/archivestructure', headers={header: 'override'})
        for url in ['https://elsewhere.invalid/api/v2/archivestructure',
                    api[0].replace('https:', 'http:') + '/archivestructure']:
            with pytest.raises(ValueError, match='configured HTTPS origin'):
                rest.get(url)
        request.assert_not_called()
    count = len(api[2])
    with pytest.raises(Exception, match='redirect refused'):
        rest.get('/redirect')
    assert len(api[2]) == count + 1
    assert api[2][-1][0].endswith('/redirect')


def test_missing_invalid_and_rejected_credentials_do_not_leak(api, caplog):
    for missing in [{'value': ''}, {'cloud_id': ''}, {'cloud_id': CLOUD_ID + '\r\nInjected: value'}]:
        with pytest.raises(ValueError) as error:
            client(api, **missing)
        assert API_KEY not in str(error.value) and CLOUD_ID not in str(error.value)
    with pytest.raises(ValueError, match='HTTPS base URL'):
        RestClient('http://example.invalid/api/v2', {'type': 'docbox_cloud', 'value': API_KEY, 'cloud_id': CLOUD_ID})
    caplog.set_level(logging.INFO)
    with pytest.raises(Exception, match='HTTP 401') as error:
        client(api, value='SYNTHETIC-WRONG-KEY').get('/archivestructure')
    assert API_KEY not in str(error.value) + caplog.text
    assert CLOUD_ID not in str(error.value) + caplog.text
    # TLS verification stays enabled and the local CA is not implicitly trusted.
    untrusted = client(api)
    untrusted.session.verify = True
    with pytest.raises(Exception, match='TLS error'):
        untrusted.get('/archivestructure')


@pytest.mark.parametrize('auth_type', ['docbox_cloud', 'apikey'])
def test_generated_tool_through_sdk_mcp_transport(api, tmp_path, monkeypatch, auth_type):
    monkeypatch.setenv('REST_API_BASE_URL', api[0])
    monkeypatch.setenv('AUTH_TYPE', auth_type)
    monkeypatch.setenv('API_KEY_VALUE', API_KEY)
    monkeypatch.setenv('CLOUD_ID', CLOUD_ID)
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', str(api[1]))
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'openapi': '3.0.0', 'info': {'title': 'Synthetic DOCBOX', 'version': '1'},
                               'paths': {'/archivestructure': {'get': {'responses': {'200': {'description': 'OK'}}}}}}))
    # Optionally exercise the entire real prepared DOCBOX spec from the stack.
    monkeypatch.setenv('OPENAPI_SPEC_URL', os.environ.get('DOCBOX_TEST_OPENAPI_SPEC', str(spec)))
    server = RestApiMCPServer()
    assert asyncio.run(server.initialize())
    assert server._handle_health_check({})['auth_configured'] is True
    assert API_KEY not in json.dumps(server.tools) and CLOUD_ID not in json.dumps(server.tools)
    with TestClient(server.create_http_app(), base_url='http://localhost') as mcp:
        headers = initialize(mcp)
        response = mcp.post('/mcp', headers=headers, json={
            'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
            'params': {'name': 'get_archivestructure', 'arguments': {}},
        })
        result = response.json()['result']
        assert result['isError'] is False
        assert json.loads(result['content'][0]['text'])['data'] == {'archives': []}
    monkeypatch.setenv('CLOUD_ID', '')
    missing = RestApiMCPServer()
    # Use explicit Cloud mode so absence never silently selects inhouse API-key auth.
    monkeypatch.setenv('AUTH_TYPE', 'docbox_cloud')
    assert asyncio.run(missing.initialize())
    assert missing._handle_health_check({})['status'] == 'not_ready'
    assert missing._handle_health_check({})['auth_configured'] is False
    calls = len(api[2])
    error = asyncio.run(missing.handle_tool_call('get_archivestructure', {}))
    assert 'error' in error and 'CLOUD_ID' in error['error']
    assert len(api[2]) == calls
