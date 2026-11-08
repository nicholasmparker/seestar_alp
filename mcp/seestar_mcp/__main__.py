"""Run the Seestar MCP server: stdio by default, or loopback-only HTTP.

The streamable-http transport lets an agent on another host reach this server
through an SSH local forward. The server has no authentication of its own, so
that transport refuses any bind address other than loopback.
"""

import argparse
import ipaddress

from seestar_mcp.server import mcp


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="seestar-mcp")
    parser.add_argument(
        "--transport", choices=("stdio", "streamable-http"), default="stdio"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback bind address for streamable-http (default 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8769, help="port for streamable-http"
    )
    args = parser.parse_args(argv)
    if args.transport == "streamable-http":
        if not _is_loopback(args.host):
            parser.error(
                f"--host {args.host!r} is not a loopback address; "
                "the HTTP transport has no authentication"
            )
        if not 0 < args.port < 65536:
            parser.error(f"--port {args.port} is out of range")
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
