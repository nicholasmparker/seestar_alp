import pytest

from seestar_mcp import __main__ as entry
from seestar_mcp.server import mcp


@pytest.fixture
def runs(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "run", lambda transport: calls.append(transport))
    # main() mutates the shared FastMCP settings; restore them after each test.
    monkeypatch.setattr(mcp.settings, "host", mcp.settings.host)
    monkeypatch.setattr(mcp.settings, "port", mcp.settings.port)
    return calls


def test_default_is_stdio(runs):
    entry.main([])
    assert runs == ["stdio"]


def test_streamable_http_binds_loopback_port(runs):
    entry.main(["--transport", "streamable-http", "--port", "8769"])
    assert runs == ["streamable-http"]
    assert mcp.settings.host == "127.0.0.1"
    assert mcp.settings.port == 8769


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_streamable_http_accepts_loopback_hosts(runs, host):
    entry.main(["--transport", "streamable-http", "--host", host])
    assert runs == ["streamable-http"]
    assert mcp.settings.host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.131", "::", "watcher"])
def test_streamable_http_refuses_non_loopback_hosts(runs, host):
    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--transport", "streamable-http", "--host", host])
    assert excinfo.value.code == 2
    assert runs == []


@pytest.mark.parametrize("port", ["0", "65536"])
def test_streamable_http_refuses_invalid_port(runs, port):
    with pytest.raises(SystemExit):
        entry.main(["--transport", "streamable-http", "--port", port])
    assert runs == []


def test_http_mode_keeps_dns_rebinding_protection(runs):
    entry.main(["--transport", "streamable-http"])
    security = mcp.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
