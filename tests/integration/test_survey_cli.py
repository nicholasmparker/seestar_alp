"""Real loopback HTTP and subprocess CLI checks; no hardware involved."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from device.night_survey import SurveyClient
from device.survey_safety import SurveyError

pytestmark = pytest.mark.integration


def test_native_contract_over_real_http():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            form = parse_qs(
                self.rfile.read(int(self.headers["Content-Length"])).decode()
            )
            seen.append(form)
            method = json.loads(form["Parameters"][0])["method"]
            reply = {
                "ErrorNumber": 0,
                "Value": json.dumps(
                    {
                        "generation": "http-fixture",
                        "after_sequence": 0,
                        "reply": {
                            "id": 7,
                            "code": 0,
                            "method": method,
                            "Timestamp": "1",
                            "result": [20, 180],
                        },
                    }
                ),
            }
            if method == "reject":
                reply["ErrorNumber"] = 1025
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = SurveyClient(server.server_port)
            assert client.native("scope_get_horiz_coord") == [20, 180]
            with pytest.raises(SurveyError, match="alp_action_rejected"):
                client.native("reject")
            assert seen[0]["Action"] == ["survey_native"]
            assert seen[0]["ClientTransactionID"] == ["1"]
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_cli_status_stop_and_missing_authority(tmp_path):
    run_id = "1" * 32
    directory = tmp_path / run_id
    directory.mkdir(mode=0o700)
    (tmp_path / "latest").write_text(run_id)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "sampled_cells": 1,
                "planned_cells": 1,
                "samples": [{"private_data": "must not reach status"}],
            }
        )
    )

    def cli(command, *extra):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "device.night_survey",
                command,
                "--root",
                str(tmp_path),
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

    result = cli("status")
    assert result.returncode == 0
    assert json.loads(result.stdout)["sampled_cells"] == 1
    assert "private_data" not in result.stdout
    result = cli("stop")
    assert result.returncode == 0
    assert (directory / "stop").exists()
    assert not ((directory / "stop").stat().st_mode & 0o077)
    result = cli("start")
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "physical_clearance_required"
