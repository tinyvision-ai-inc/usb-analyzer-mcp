"""Live tests against a connected analyzer (passive capture only: nothing here
drives VBUS, generates traffic or restarts the unit).

Run with:  uv run pytest -m hardware
Skipped automatically when no analyzer is detected."""

from __future__ import annotations

import asyncio
import os

import pytest
from mcp import Client

from conftest import payload, server_params

pytestmark = [pytest.mark.anyio, pytest.mark.hardware]


@pytest.fixture(scope="module")
async def hw(tmp_path_factory):
    out = tmp_path_factory.mktemp("hw_out")
    async with Client(server_params(out), read_timeout_seconds=600) as c:
        con = payload(await c.call_tool("analyzer_connect", {"wait_for_device_s": 30}))
        if not con["connected_analyzers"]:
            pytest.skip("no analyzer unit connected")
        yield c, out


async def call(c, name, **args):
    return payload(await c.call_tool(name, args))


async def test_info(hw):
    c, out = hw
    info = await call(c, "analyzer_info")
    assert isinstance(info["serial_number"], int)
    assert info["hardware"]["name"] != "unknown"
    assert info["serial_number"] in info["connected_analyzers"]
    assert info["output_dir"] == str(out)
    expected = os.environ.get("USB_ANALYZER_EXPECT_HW")  # e.g. "Voyager M310P"
    if expected:
        assert info["hardware"]["name"] == expected
    # License titles must match the suite's License dialog exactly; unknown ones are rejected.
    bad = await c.call_tool("analyzer_check_license", {"feature_title": "no such feature"})
    assert bad.is_error and "not being protected" in bad.content[0].text
    lic_name = os.environ.get("USB_ANALYZER_LICENSE_FEATURE")
    if lic_name:
        assert isinstance((await call(c, "analyzer_check_license", feature_title=lic_name))["licensed"], bool)


async def test_record_start_stop(hw, tmp_path):
    c, _ = hw
    ro = await call(
        c, "recording_options_build", output_file=str(tmp_path / "mcp_test.rec"),
        rec_mode="manual_trigger", buffer_size_mb=64, trace_file_name=str(tmp_path / "mcp_capture.usb"),
    )
    assert (tmp_path / "mcp_test.rec").exists(), ro
    st = await call(c, "recording_start", rec_options_file=ro["rec_options_file"])
    assert st["recording"]
    await asyncio.sleep(2)
    status = await call(c, "recording_status")
    assert status.get("done") is False, status
    await call(c, "recording_manual_trigger")
    trig = await call(c, "recording_wait", timeout_s=10, until=["triggered", "done"])
    assert trig["reached"], trig
    await asyncio.sleep(1)
    stopped = await call(c, "recording_stop")
    h = stopped["trace"]
    assert stopped["summary"]["packet_count"] >= 0
    saved = await call(c, "trace_save", trace=h, output_file=str(tmp_path / "saved.usb"))
    assert (tmp_path / "saved.usb").exists(), saved
    s = await call(c, "trace_summary", trace=h)
    assert s["path"], s
    pk = await call(c, "trace_find_packets", trace=h, max_results=5)
    assert pk["packet_count"] == s["packet_count"]
    ev = await call(c, "analyzer_events")
    assert any(e.get("subsystem") == "recording" for e in ev["log"]), ev
    await call(c, "trace_close", trace=h)


async def test_status_idle_after_stop(hw):
    c, _ = hw
    s = await call(c, "recording_status")
    assert s["done"] is True
