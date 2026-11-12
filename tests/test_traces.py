"""Trace analysis tools, exercised over a real stdio MCP session against the
sample trace shipped with USB Protocol Suite. Needs the suite installed; no
analyzer hardware required."""

from __future__ import annotations

import pytest
from mcp import Client

from conftest import SAMPLE_OPT, SAMPLE_TRACE, SUITE_DATA, payload, server_params

pytestmark = [pytest.mark.anyio, pytest.mark.suite]

if not SAMPLE_TRACE.exists():
    pytest.skip("USB Protocol Suite sample trace not installed", allow_module_level=True)

CHECK_TIMESTAMP_VSE = SUITE_DATA / "Scripts" / "VFScripts" / "Examples" / "CheckTimestamp.vse"


@pytest.fixture(scope="module")
async def session(tmp_path_factory):
    out = tmp_path_factory.mktemp("mcp_out")
    async with Client(server_params(out), read_timeout_seconds=300) as c:
        r = payload(await c.call_tool("trace_open", {"path": str(SAMPLE_TRACE)}))
        yield c, r["trace"], out


async def call(c, name, **args):
    return payload(await c.call_tool(name, args))


async def test_tool_catalog(session):
    c, _, _ = session
    names = {t.name for t in (await c.list_tools()).tools}
    for expected in (
        "analyzer_info", "recording_start", "recording_stop", "recording_options_build", "usb3_exerciser",
        "pd_exerciser", "trace_open", "trace_get_packets", "trace_find_packets", "trace_errors",
        "trace_export_csv", "trace_run_verification_script", "com_invoke",
    ):
        assert expected in names
    destructive = {t.name for t in (await c.list_tools()).tools if t.annotations and t.annotations.destructive_hint}
    assert {"usb_unplug_plug", "usb3_trainer_set_vbus", "analyzer_restart"} <= destructive


async def test_summary_and_list(session):
    c, h, _ = session
    s = await call(c, "trace_summary", trace=h)
    assert s["packet_count"] == 1663
    assert s["file_name"] == "errors.usb"
    assert s["decode_finished"]["all"] is True
    assert any(t["trace"] == h for t in await call(c, "trace_list"))
    # re-opening the same file reuses the handle
    assert (await call(c, "trace_open", path=str(SAMPLE_TRACE)))["trace"] == h


async def test_get_packets(session):
    c, h, _ = session
    r = await call(c, "trace_get_packets", trace=h, start=3, count=2, raw=True)
    p3, p4 = r["packets"]
    assert p3["type"] == "IN" and p4["type"] == "DATA0"
    assert p3["speed"] == "full_speed"  # from the suite's CSV decode, GetSpeed() can't tell FS from HS
    f = {x["name"]: x["value"] for x in p3["fields"]}
    # This legacy sample's field values come back bit-reversed from the API (see README), so only
    # check the field set; the CSV decode is checked in test_find_packets.
    assert {"Address", "Endpoint", "CRC5"} <= set(f)
    assert p4["raw_hex"].startswith("01 c3")
    assert "USB2 CRC16" in p4["errors"]


async def test_field_masking_to_bit_width(session):
    c, h, _ = session
    r = await call(c, "trace_get_packets", trace=h, start=9, count=1)
    frame = next(x for x in r["packets"][0]["fields"] if x["name"] == "Frame #")
    # the API returns 0x8000 here (junk above bit 10); values are masked to the field width
    assert frame["bits"] == 11 and int(frame["value"], 16) <= 0x7FF


async def test_packet_detail_and_field(session):
    c, h, _ = session
    d = await call(c, "trace_get_packet_detail", trace=h, packet=3)
    assert d["raw_bits"]["number_of_bits"] > 0
    listed = {x["name"]: x["value"] for x in d["fields"]}
    assert (await call(c, "trace_get_field", trace=h, packet=3, field="Endpoint"))["value"] == listed["Endpoint"]
    bad = await c.call_tool("trace_get_field", {"trace": h, "packet": 3, "field": "NOPE"})
    assert bad.is_error and "Available fields" in bad.content[0].text


async def test_find_packets(session):
    c, h, _ = session
    r = await call(c, "trace_find_packets", trace=h, types=["IN"], column_equals={"Ad": "2"}, max_results=5)
    assert r["matches"] and all(m["Type/PID"] == "IN" and m["Ad"] == "2" for m in r["matches"])
    e = await call(c, "trace_find_packets", trace=h, errors_only=True, max_results=3)
    assert len(e["matches"]) == 3
    paged = await call(c, "trace_find_packets", trace=h, max_scan=100, max_results=1000)
    assert paged["next_start"] == 100


async def test_errors(session):
    c, h, _ = session
    r = await call(c, "trace_errors", trace=h, max_packets=10)
    cats = {(s["category"], s["channel"]): s["count"] for s in r["summary"]}
    assert cats[("Bad CRC5", "Ch0")] == 128
    assert cats[("Bad CRC16", "Ch0")] == 128
    assert r["error_packet_count"] > 10 and r["truncated"] is True
    assert r["error_packets"][0]["packet"] == 4 and "USB2 CRC16" in r["error_packets"][0]["errors"]


async def test_reports_and_exports(session):
    c, h, out = session
    es = await call(c, "trace_report", trace=h, kind="error_summary", max_chars=500)
    assert "Error report" in es["content"] and es["truncated"]
    ts = await call(c, "trace_report", trace=h, kind="traffic_summary")
    assert ts["size_bytes"] > 0
    fi = await call(c, "trace_report", trace=h, kind="file_info", max_chars=200)
    assert fi["output_file"].endswith(".html")
    tx = await call(c, "trace_export_text", trace=h, start=0, end=10)
    assert "Packet(3)" in tx["content"] and str(out) in tx["output_file"]
    csv_tr = await call(c, "trace_export_csv", trace=h, level="transactions", start=0, end=5, max_rows=3)
    assert "Transaction" in csv_tr["header"] and len(csv_tr["rows"]) == 3
    csv_xf = await call(c, "trace_export_csv", trace=h, level="transfers", max_rows=2)
    assert csv_xf["header"]
    twp = await call(c, "trace_export_transactions_with_packets", trace=h, start=0, end=5, max_rows=5)
    assert twp["total_rows"] > 0


async def test_save_and_display_options(session, tmp_path):
    c, h, _ = session
    saved = await call(c, "trace_save", trace=h, output_file=str(tmp_path / "part.usb"), start=0, end=100)
    assert (tmp_path / "part.usb").stat().st_size > 0, saved
    part = await call(c, "trace_open", path=str(tmp_path / "part.usb"))
    assert 0 < part["packet_count"] <= 101
    if SAMPLE_OPT.exists():
        await call(c, "trace_apply_display_options", trace=part["trace"], opt_file=str(SAMPLE_OPT))
    await call(c, "trace_close", trace=part["trace"])
    assert all(t["trace"] != part["trace"] for t in await call(c, "trace_list"))


async def test_power_info_and_goto(session):
    c, h, _ = session
    p = await call(c, "trace_power_info", trace=h, packet=3)
    assert p["valid"] is False  # sample trace has no power capture
    await call(c, "trace_goto", trace=h, level="packets", index=3)


@pytest.mark.skipif(not CHECK_TIMESTAMP_VSE.exists(), reason="example VSE not installed")
async def test_verification_script(session):
    c, h, _ = session
    r = await call(c, "trace_run_verification_script", trace=h, script=str(CHECK_TIMESTAMP_VSE))
    assert r["result"] in {"PASSED", "FAILED", "DONE", "PASSED_WARNING", "NOT_APPLICABLE", "PASSED_WITH_NOTE"}


async def test_reference_and_raw_com(session):
    c, h, _ = session
    topics = (await call(c, "api_reference"))["topics"]
    assert "usb2_packet_types" in topics
    ref = await call(c, "api_reference", topic="usb2_packet_types")
    assert ref["usb2_packet_types"]["4"] == "SETUP"
    members = (await call(c, "com_list_members", target=f"trace:{h}"))["members"]
    assert "ExportToCsv" in members
    assert "Scroll" not in members  # undocumented script interface is hidden by default
    r = await call(c, "com_invoke", target=f"trace:{h}", member="GetPacketsCount")
    assert r["result"] == 1663


async def test_restricted_members_refused_by_default(session):
    c, h, _ = session
    for name, args in (
        ("com_invoke", {"target": "analyzer", "member": "GetConnectedAnalyzerNumber"}),
        ("com_invoke", {"target": f"trace:{h}", "member": "ExtractRecordingOptions", "args": ["x.rec"]}),
        ("recording_options_build", {"settings": {48: 1}}),
        ("trace_power_info", {"trace": h, "packet": 3, "cc_line": 1}),
        ("pd_exerciser", {"action": "pause"}),
        ("recording_start_with_generation", {"rec_options_file": "x.rec"}),
    ):
        r = await c.call_tool(name, args)
        assert r.is_error, (name, args)
        assert "USB_ANALYZER_MCP_ENABLE_RESTRICTED" in r.content[0].text, r.content[0].text
    bad = await c.call_tool("trace_summary", {"trace": 999})
    assert bad.is_error and "Unknown trace handle" in bad.content[0].text
