from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SUITE_DATA = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Documents" / "LeCroy" / "USB Protocol Suite"
SAMPLE_TRACE = SUITE_DATA / "Automation" / "wsh" / "Input" / "errors.usb"
SAMPLE_REC = SUITE_DATA / "Automation" / "wsh" / "Input" / "test_ro.rec"
SAMPLE_OPT = SUITE_DATA / "Automation" / "wsh" / "Input" / "test_do.opt"


def server_params(output_dir: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "usb_analyzer_mcp", "--output-dir", str(output_dir)],
        cwd=str(ROOT),
    )


def payload(result) -> dict | list:
    """Structured content of a CallToolResult (falls back to parsing text)."""
    assert not result.is_error, result.content[0].text if result.content else result
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc) == {"result"}:
            return sc["result"]
        return sc
    return json.loads(result.content[0].text)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
