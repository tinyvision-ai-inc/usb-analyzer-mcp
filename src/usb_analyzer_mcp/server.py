"""MCP server exposing the Teledyne LeCroy USB Protocol Suite automation API
(Voyager / Mercury / Advisor USB protocol analyzers & exercisers) to agents."""

from __future__ import annotations

import asyncio
import os
from typing import Annotated, Any, Literal

import pywintypes
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from . import constants as C
from .analyzer import AnalyzerError, UsbAnalyzer, com_error_text
from .com_thread import ComThread

INSTRUCTIONS = """\
Controls a Teledyne LeCroy USB protocol analyzer (e.g. Voyager M310P) through the
USB Protocol Suite COM automation API on this Windows host.

Typical capture workflow:
  1. analyzer_info                      -> confirm the unit is connected (serial, HW, FW/BE versions)
  2. recording_options_build (optional) -> write a .rec file (mode, buffer, trigger, channels...)
  3. recording_start                    -> analyzer starts capturing
  4. ... exercise the device under test ...
  5. recording_stop (wait_for_trace)    -> returns a trace handle (integer)
     or recording_wait first when using triggers / snapshot buffers.
Analysis workflow on a trace handle (from recording_stop or trace_open):
  trace_summary, trace_get_packets, trace_find_packets, trace_errors,
  trace_export_csv (level=transactions/transfers gives decoded rows),
  trace_report (error / traffic summaries), trace_run_verification_script, trace_save.
All output files go to the server output directory (see analyzer_info.output_dir)
unless an absolute path is given. Packet numbers are zero-based.
Tools marked destructive drive the bus (VBUS, unplug/replug, exerciser traffic,
analyzer restart): only use them when the user asked for that.
com_invoke is a raw escape hatch to documented Automation API methods that have no
dedicated tool. Undocumented/restricted vendor members are disabled unless the
operator sets USB_ANALYZER_MCP_ENABLE_RESTRICTED=1.
"""

mcp = MCPServer(name="usb-analyzer", instructions=INSTRUCTIONS, version=__version__)

COM = ComThread()
AN = UsbAnalyzer()

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
BUS = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

TraceH = Annotated[int, Field(description="Trace handle returned by trace_open / recording_stop / trace_list")]
OutFile = Annotated[str | None, Field(description="Output file path. Relative paths go to the server output dir; omit for an auto-named file.")]
MaxChars = Annotated[int, Field(description="Maximum characters of file content to return inline (-1 = all)", ge=-1)]


async def run(fn, *args, **kwargs) -> Any:
    try:
        return await COM.run(fn, *args, **kwargs)
    except AnalyzerError as exc:
        raise ToolError(str(exc)) from exc
    except pywintypes.com_error as exc:
        raise ToolError(com_error_text(exc)) from exc


def _level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    if level.isdigit():
        return int(level)
    if level not in C.LEVELS:
        raise ToolError(f"Unknown level {level!r}; one of {sorted(C.LEVELS)}")
    return C.LEVELS[level]


LevelName = Annotated[
    str,
    Field(description="Decode level: packets, transactions, split_transactions, transfers, ptp_transactions, ptp_objects, ptp_sessions, scsi_operations, ptp_groups, obex (or its numeric id)"),
]

# =========================================================================== device


@mcp.tool(annotations=WRITE)
async def analyzer_connect(
    wait_for_device_s: Annotated[float, Field(ge=0, le=120, description="Seconds to wait for a unit to enumerate")] = 20,
) -> dict:
    """Start/attach to the USB Protocol Suite COM server and wait for an analyzer unit to be detected.
    Called implicitly by other tools; use explicitly after (re)plugging the analyzer."""
    return await run(AN.connect, wait_for_device_s)


@mcp.tool(annotations=RO)
async def analyzer_info() -> dict:
    """Analyzer identity and state: software / bus-engine / firmware versions, serial number,
    hardware model, SSP support, connected units, suite folders, output dir, open trace count."""
    return await run(AN.info)


@mcp.tool(annotations=RO)
async def analyzer_check_license(
    feature_title: Annotated[str, Field(description="Feature name exactly as shown in the suite's License dialog")],
) -> dict:
    """Check whether a licensed feature is available on the connected unit (GetLicensedCapability)."""
    return {"feature": feature_title, "licensed": await run(AN.is_licensed, feature_title)}


@mcp.tool(annotations=RO)
async def analyzer_events(clear: bool = False) -> dict:
    """Recent analyzer events (OnStatusReport recording/generation state changes, OnTraceCreated)
    captured by the server's event sink, plus the latest status per subsystem."""
    return await run(AN.events, clear)


@mcp.tool(annotations=RO)
async def analyzer_is_restart_required(
    functionality: Annotated[Literal["usb4_tbt3_recording", "usb3_recording"], Field(description="Functionality to check")],
) -> dict:
    """Whether the analyzer must restart (reload bus engine) to use the given functionality (M4x family)."""
    return {"restart_required": await run(AN.is_restart_required, C.DEVICE_FUNCTIONALITY[functionality])}


@mcp.tool(annotations=BUS)
async def analyzer_restart(
    functionality: Literal["force_restart", "usb4_tbt3_recording", "usb3_recording"] = "force_restart",
    wait_for_device_s: Annotated[float, Field(ge=0, le=180)] = 60,
) -> dict:
    """Restart the analyzer hardware (optionally loading a specific bus engine). All trace handles are invalidated."""
    return await run(AN.restart_analyzer, C.DEVICE_FUNCTIONALITY[functionality], wait_for_device_s)


@mcp.tool(annotations=WRITE)
async def analyzer_set_persistent_decoding(enable: bool) -> dict:
    """Enable/disable persistent decoding (caches decode data next to traces in a .tmp folder; faster re-opens)."""
    await run(AN.set_persistent_decoding, enable)
    return {"persistent_decoding": enable}


@mcp.tool(annotations=BUS)
async def usb_unplug_plug() -> dict:
    """Break VBUS between host and device on the analyzer A/B ports for 1 second (simulated unplug/replug)."""
    await run(AN.gen, "UsbUnplugPlug")
    return {"ok": True}


@mcp.tool(annotations=BUS)
async def usb3_trainer_set_vbus(on: Annotated[bool, Field(description="True = VBUS on, False = VBUS off")]) -> dict:
    """Switch VBUS on/off in the USB3 trainer (exerciser) without running a script (SwitchVBus)."""
    await run(AN.gen, "SwitchVBus", 0 if on else 1)
    return {"vbus_on": on}


# =========================================================================== recording


@mcp.tool(annotations=WRITE)
async def recording_options_build(
    output_file: OutFile = None,
    base_file: Annotated[str | None, Field(description="Existing .rec file to start from (else current/reset options)")] = None,
    reset: Annotated[bool, Field(description="Reset to defaults first when no base_file is given")] = True,
    rec_mode: Literal["snapshot", "manual_trigger", "event_trigger"] | None = None,
    buffer_size_mb: Annotated[int | None, Field(ge=1, description="Recording buffer size in MB (SetBufferSizeEx)")] = None,
    buffer_size_bytes: Annotated[int | None, Field(ge=1, description="Recording buffer size in bytes (legacy SetBufferSize)")] = None,
    post_trigger_percent: Annotated[int | None, Field(ge=0, le=100)] = None,
    trigger_beep: bool | None = None,
    data_truncate_bytes: Annotated[int | None, Field(ge=108, description="Truncate packet data longer than this")] = None,
    auto_merge: bool | None = None,
    save_external_signals: bool | None = None,
    trace_file_name: Annotated[str | None, Field(description="Where the suite stores the recorded trace")] = None,
    filter_out: Annotated[bool | None, Field(description="Filter polarity: True = filter out, False = filter in")] = None,
    settings: Annotated[dict[int, int] | None, Field(description="RESTRICTED (needs USB_ANALYZER_MCP_ENABLE_RESTRICTED=1): raw ChangeUsbRecordingOptionSetting id->value pairs; ids are listed in the vendor manual")] = None,
    string_settings: Annotated[dict[int, str] | None, Field(description="RESTRICTED: raw string recording-option settings id->text")] = None,
    bypass_lru: Annotated[bool | None, Field(description="RESTRICTED: SetEnableBypassLRU")] = None,
) -> dict:
    """Create a recording options (.rec) file via the IUsbRecOptions API and save it. Pass the returned
    rec_options_file to recording_start / recording_make. Fine-grained triggers/filters are best authored in the
    suite GUI (Setup > Recording Options > Save) and used as base_file."""
    return await run(
        AN.build_recording_options, output_file or "", base_file, reset,
        None if rec_mode is None else C.REC_MODES[rec_mode], buffer_size_bytes, buffer_size_mb,
        post_trigger_percent, trigger_beep, data_truncate_bytes, auto_merge, save_external_signals,
        trace_file_name, filter_out, settings, string_settings, bypass_lru,
    )


@mcp.tool(annotations=WRITE)
async def recording_options_apply(rec_options_file: str) -> dict:
    """Apply a .rec file to the analyzer before starting recording/exerciser (ApplyRecordingOptions)."""
    await run(AN.apply_recording_options, rec_options_file)
    return {"applied": rec_options_file}


@mcp.tool(annotations=WRITE)
async def recording_start(
    rec_options_file: Annotated[str | None, Field(description=".rec file; omit to use the suite's default options")] = None,
    keep_current_options: Annotated[bool, Field(description="Start without changing currently applied options")] = False,
) -> dict:
    """Start recording (non-blocking). Follow with recording_status / recording_wait / recording_stop."""
    await run(AN.start_recording, rec_options_file, keep_current_options)
    return {"recording": True, "status": await run(AN.recording_status)}


@mcp.tool(annotations=RO)
async def recording_status() -> dict:
    """Current recording state: triggered / done flags (GetRecordingStatus) plus last status events
    (state name and percent: buffer usage while recording, upload progress while uploading)."""
    return await run(AN.recording_status)


@mcp.tool(annotations=RO)
async def recording_wait(
    timeout_s: Annotated[float, Field(gt=0, le=3600)] = 30,
    until: Annotated[list[Literal["done", "triggered", "waiting_for_sync_rec"]], Field(description="Return when any of these states is reached")] = ["done"],  # noqa: B006
) -> dict:
    """Block until the recording reaches the desired state or the timeout expires (WaitForRecordingStatus)."""
    mask = 0
    for u in until:
        mask |= C.RSTAT[u]
    return await run(AN.wait_for_recording_status, int(timeout_s * 1000), mask)


@mcp.tool(annotations=WRITE)
async def recording_manual_trigger() -> dict:
    """Force a trigger (for recording options whose trigger mode is manual)."""
    await run(AN.manual_trigger)
    return {"triggered": True}


@mcp.tool(annotations=WRITE)
async def recording_stop(
    wait_for_trace: Annotated[bool, Field(description="Upload and return the trace handle (StopRecordingAndWaitForTrace)")] = True,
    abort_upload: Annotated[bool, Field(description="Only when wait_for_trace=false: discard the capture")] = False,
) -> dict:
    """Stop the current recording. With wait_for_trace (default) blocks until uploaded and returns a trace handle."""
    if wait_for_trace:
        e = await run(AN.stop_recording_and_wait_for_trace)
        return {"trace": e.handle, "path": e.path, "summary": await run(AN.trace_summary, e.handle)}
    await run(AN.stop_recording, abort_upload)
    return {"stopped": True, "abort_upload": abort_upload, "note": "If uploaded, the trace appears in trace_list via the OnTraceCreated event."}


@mcp.tool(annotations=WRITE)
async def recording_make(
    rec_options_file: Annotated[str | None, Field(description=".rec file (snapshot or trigger mode so the capture ends by itself)")] = None,
) -> dict:
    """Make a complete recording and return its trace handle. BLOCKS until the analyzer finishes on its own
    (buffer full / post-trigger complete) - use recording_start + recording_stop for open-ended captures."""
    e = await run(AN.make_recording, rec_options_file)
    return {"trace": e.handle, "path": e.path, "summary": await run(AN.trace_summary, e.handle)}


@mcp.tool(annotations=WRITE)
async def display_options_load(opt_file: Annotated[str, Field(description="Display options .opt file")]) -> dict:
    """Load display options (.opt) that apply to traces opened or recorded afterwards (hides filtered traffic)."""
    await run(AN.load_display_options, opt_file)
    return {"loaded": opt_file}


# =========================================================================== generation / exercisers


@mcp.tool(annotations=BUS)
async def usb2_generation(
    action: Literal["start", "stop"],
    gen_file: Annotated[str | None, Field(description=".utg traffic generation file (start)")] = None,
    intelliframe: Annotated[bool, Field(description="Generation mode: IntelliFrame (True) or bitstream (False)")] = False,
    force_reload: bool = False,
    loop_count: Annotated[int, Field(ge=-1, le=16381, description="-1 loops forever")] = 1,
) -> dict:
    """USB 2.0 traffic generation from a .utg file (StartGeneration / StopGeneration)."""
    if action == "stop":
        await run(AN.gen, "StopGeneration")
        return {"stopped": True}
    if not gen_file:
        raise ToolError("gen_file is required for start")
    mode = (1 if intelliframe else 0) | (0x80000000 if force_reload else 0)
    if mode >= 0x80000000:
        mode -= 1 << 32
    await run(AN.gen, "StartGeneration", os.path.abspath(gen_file), mode, loop_count)
    return {"started": True}


def _script(path: str | None) -> str:
    return os.path.abspath(path) if path else ""


@mcp.tool(annotations=BUS)
async def usb3_exerciser(
    action: Literal["start", "stop", "pause", "resume", "is_idle", "wait_idle", "reset_trainer"],
    script: Annotated[str | None, Field(description=".usb3g script (start). Omit to repeat the last script.")] = None,
    timeout_s: Annotated[float, Field(gt=0, le=3600)] = 30,
    device_mode: Annotated[bool, Field(description="reset_trainer: True = device trainer, False = host trainer")] = False,
) -> dict:
    """USB 3.x exerciser / trainer control: run .usb3g generation scripts, pause/resume, wait for completion,
    or reset the unit into host/device trainer mode."""
    if action == "start":
        await run(AN.gen, "StartUsb3Generation", _script(script))
    elif action == "stop":
        await run(AN.gen, "StopUsb3Generation")
    elif action == "pause":
        await run(AN.gen, "PauseUsb3Generation")
    elif action == "resume":
        await run(AN.gen, "ResumeUsb3Generation")
    elif action == "is_idle":
        return {"idle": bool(await run(AN.gen, "IsUsb3GenerationIdle"))}
    elif action == "wait_idle":
        return await run(AN.wait_gen, "WaitForUsb3GenerationIdle", int(timeout_s * 1000))
    elif action == "reset_trainer":
        await run(AN.gen, "ResetUsb3Trainer", 1 if device_mode else 0)
    return {"ok": True, "action": action}


@mcp.tool(annotations=BUS)
async def pd_exerciser(
    action: Literal["start", "stop", "pause", "resume", "is_idle", "wait_idle", "wait_pause", "wait_pause_tag"],
    script: Annotated[str | None, Field(description=".updg Power Delivery script (start). Omit to repeat the last script.")] = None,
    timeout_s: Annotated[float, Field(gt=0, le=3600)] = 30,
) -> dict:
    """USB Power Delivery exerciser control (.updg scripts). wait_idle 'done' is the PD_Stop() return value
    (256 for PD_Stop() without args, 0 = timeout)."""
    m = {"start": "StartPDGeneration", "stop": "StopPDGeneration", "pause": "PausePDGeneration", "resume": "ResumePDGeneration"}
    if action == "pause":
        try:
            AN.require_restricted("Pausing PD generation from the API (the manual only documents script-driven pauses)")
        except AnalyzerError as exc:
            raise ToolError(str(exc)) from exc
    if action == "start":
        await run(AN.gen, m[action], _script(script))
    elif action in m:
        await run(AN.gen, m[action])
    elif action == "is_idle":
        return {"idle": bool(await run(AN.gen, "IsPDGenerationIdle"))}
    elif action == "wait_idle":
        return await run(AN.wait_gen, "WaitForPDGenerationIdle", int(timeout_s * 1000))
    elif action == "wait_pause":
        return await run(AN.wait_gen, "WaitForPDGenerationPause", int(timeout_s * 1000))
    elif action == "wait_pause_tag":
        return await run(AN.wait_gen, "WaitForPDGenerationPauseTag", int(timeout_s * 1000))
    return {"ok": True, "action": action}


@mcp.tool(annotations=BUS)
async def usb4_exerciser(
    action: Literal["start", "stop", "resume", "wait_idle", "wait_pause_tag"],
    script: Annotated[str | None, Field(description=".usb4g script (start). Omit to repeat the last script.")] = None,
    timeout_s: Annotated[float, Field(gt=0, le=3600)] = 30,
) -> dict:
    """USB4 exerciser control (.usb4g scripts) - requires USB4-capable hardware (M4x family)."""
    if action == "start":
        await run(AN.gen, "StartUSB4Generation", _script(script))
    elif action == "stop":
        await run(AN.gen, "StopUSB4Generation")
    elif action == "resume":
        await run(AN.gen, "ResumeUSB4Generation")
    elif action == "wait_idle":
        return await run(AN.wait_gen, "WaitForUSB4GenerationIdle", int(timeout_s * 1000))
    elif action == "wait_pause_tag":
        return await run(AN.wait_gen, "WaitForUSB4GenerationPauseTag", int(timeout_s * 1000))
    return {"ok": True, "action": action}


@mcp.tool(annotations=BUS)
async def recording_start_with_generation(
    rec_options_file: str,
    gen_file_1: str | None = None,
    gen_file_2: str | None = None,
    gen_file_3: str | None = None,
    usb2_gen_mode: int = 0,
    usb2_gen_loop_count: int = 0,
) -> dict:
    """RESTRICTED (needs USB_ANALYZER_MCP_ENABLE_RESTRICTED=1; undocumented compliance interface): start recording
    and up to three generation scripts in one synchronized call. Otherwise use recording_start then the exerciser tool."""
    try:
        AN.require_restricted("recording_start_with_generation (StartRecordingAndRunGenerations)")
    except AnalyzerError as exc:
        raise ToolError(str(exc)) from exc
    await run(AN.gen, "StartRecordingAndRunGenerations", os.path.abspath(rec_options_file),
              _script(gen_file_1), _script(gen_file_2), _script(gen_file_3), usb2_gen_mode, usb2_gen_loop_count)
    return {"started": True}


# =========================================================================== traces


@mcp.tool(annotations=RO)
async def trace_open(path: Annotated[str, Field(description="Path to a .usb trace file")]) -> dict:
    """Open a trace file and return its handle plus summary (packet count, trigger, decode state)."""
    e = await run(AN.open_trace, path)
    return await run(AN.trace_summary, e.handle)


@mcp.tool(annotations=RO)
async def trace_list() -> list[dict]:
    """List trace handles held by this server (opened files and recordings)."""
    return await run(AN.list_traces)


@mcp.tool(annotations=WRITE)
async def trace_close(trace: TraceH) -> dict:
    """Close a trace and release its handle."""
    await run(AN.close_trace, trace)
    return {"closed": trace}


@mcp.tool(annotations=RO)
async def trace_summary(trace: TraceH) -> dict:
    """Trace name/path, packet count, trigger packet number and per-level decode status."""
    return await run(AN.trace_summary, trace)


@mcp.tool(annotations=RO)
async def trace_wait_decode(
    trace: TraceH,
    level: LevelName = "transactions",
    timeout_s: Annotated[float, Field(gt=0, le=600)] = 60,
) -> dict:
    """Wait until background decoding of a level finishes (needed before transaction/transfer exports on big traces).
    level='all' waits for all levels."""
    lv = -1 if level == "all" else _level(level)
    return await run(AN.wait_decode, trace, lv, timeout_s)


@mcp.tool(annotations=RO)
async def trace_get_packets(
    trace: TraceH,
    start: Annotated[int, Field(ge=0, description="First packet number (zero-based)")] = 0,
    count: Annotated[int, Field(ge=1, le=500)] = 20,
    fields: Annotated[bool, Field(description="Include decoded fields (name/value/bit width)")] = True,
    raw: Annotated[bool, Field(description="Include raw packet bytes as hex")] = False,
) -> dict:
    """Decode a range of packets: type (SOF/IN/DATA0/ACK/TS1/LFPS/...), timestamp & duration (ns), speed,
    channel, errors, marker, fields and optionally raw bytes. 'fields' come from IUsbPacket::GetAllFieldsValues
    (values masked to field width); on some traces multi-byte fields differ from the GUI decode, so treat
    trace_export_csv / trace_export_text as the authoritative protocol decode."""

    def work():
        total = int(AN.trace(trace).GetPacketsCountEx())
        end = min(total, start + count)
        packets = [AN.describe_packet(trace, n, fields, raw) for n in range(start, end)]
        if packets:
            AN.add_csv_speed(trace, packets)
        return {"packet_count": total, "packets": packets}

    return await run(work)


@mcp.tool(annotations=RO)
async def trace_get_packet_detail(
    trace: TraceH,
    packet: Annotated[int, Field(ge=0)],
    usb3_symbols: Annotated[bool, Field(description="Include USB3 scrambled bytes and 10-bit symbols (with RD/invalid flags)")] = False,
) -> dict:
    """Everything about one packet: decoded fields, raw bytes, exact bit length, optional USB3 symbol data."""

    def work():
        d = AN.describe_packet(trace, packet, True, True, usb3_symbols)
        AN.add_csv_speed(trace, [d])
        try:
            d["raw_bits"] = AN.raw_packet_bits(trace, packet)
        except AnalyzerError as exc:
            d["raw_bits_error"] = str(exc)
        return d

    return await run(work)


@mcp.tool(annotations=RO)
async def trace_get_field(trace: TraceH, packet: Annotated[int, Field(ge=0)], field: Annotated[str, Field(description="Field name as listed by trace_get_packets, e.g. 'ADDR', 'ENDP', 'CRC5'")]) -> dict:
    """Value of a single named field in a packet (GetFieldValue)."""
    return await run(AN.packet_field, trace, packet, field)


@mcp.tool(annotations=RO)
async def trace_find_packets(
    trace: TraceH,
    start: Annotated[int, Field(ge=0)] = 0,
    end: Annotated[int | None, Field(description="Last packet number to scan (inclusive)")] = None,
    types: Annotated[list[str] | None, Field(description="Packet type/PID names to match (case-insensitive), e.g. ['SETUP','STALL','NAK'] or ['LFPS','TS1']")] = None,
    errors_only: Annotated[bool, Field(description="Only packets the suite flags with an error")] = False,
    column_equals: Annotated[dict[str, str] | None, Field(description="Exact match on packet-CSV columns, e.g. {'Ad':'3','Ep':'1'} (numbers compare numerically)")] = None,
    text_contains: Annotated[str | None, Field(description="Substring anywhere in the row")] = None,
    max_results: Annotated[int, Field(ge=1, le=2000)] = 50,
    max_scan: Annotated[int, Field(ge=1, le=2000000, description="Packets to scan in this call; continue from next_start")] = 200000,
) -> dict:
    """Search packets by type / error / column filters using the suite's packet-level CSV decode
    (fast: ~1 s per 1000s of packets). Returns matching rows (non-empty columns only) and next_start
    to continue large traces."""
    return await run(AN.find_packets, trace, start, end, types, errors_only, column_equals, text_contains, max_results, max_scan)


@mcp.tool(annotations=RO)
async def trace_errors(
    trace: TraceH,
    start: Annotated[int, Field(description="First packet for the per-packet list (-1 = start)")] = -1,
    end: Annotated[int, Field(description="Last packet (-1 = end)")] = -1,
    max_packets: Annotated[int, Field(ge=0, le=5000, description="Max error packets decoded individually")] = 100,
    legacy_collection: Annotated[list[str] | None, Field(description=f"Also query ITrace::AnalyzerErrors for these USB2 categories {sorted(C.TRACE_ERROR_TYPES)} (unreliable in suite 10.x)")] = None,
) -> dict:
    """Errors in a trace: per-category/per-channel counts from the suite's error summary (USB2/USB3/USB4/PD
    categories), plus the list of error-flagged packets with their error bitmap names, type and timestamp."""
    mask = 0
    for name in legacy_collection or ():
        if name not in C.TRACE_ERROR_TYPES:
            raise ToolError(f"Unknown error type {name!r}")
        mask |= C.TRACE_ERROR_TYPES[name]
    return await run(AN.trace_errors, trace, start, end, max_packets, mask)


@mcp.tool(annotations=RO)
async def trace_power_info(
    trace: TraceH,
    packet: Annotated[int | None, Field(description="Packet number (or give time_ns)")] = None,
    time_ns: Annotated[float | None, Field(description="Timestamp in ns")] = None,
    cc_line: Annotated[int | None, Field(description="RESTRICTED (undocumented API): Type-C CC line number; omit for VBUS")] = None,
) -> dict:
    """VBUS (or CC line) voltage/current/power at a packet or time, if power was captured (Power Tracker)."""
    if packet is None and time_ns is None:
        raise ToolError("Give packet or time_ns")
    return await run(AN.power_info, trace, packet, time_ns, cc_line)


@mcp.tool(annotations=RO)
async def trace_report(
    trace: TraceH,
    kind: Literal["error_summary", "traffic_summary", "file_info"],
    output_file: OutFile = None,
    max_chars: MaxChars = 20000,
) -> dict:
    """Generate a suite report and return its content: error_summary (counts + packet numbers per error),
    traffic_summary (packet/transaction statistics), file_info (HTML trace information)."""
    return await run(AN.report, trace, kind, output_file, max_chars)


@mcp.tool(annotations=RO)
async def trace_export_text(
    trace: TraceH,
    start: Annotated[int, Field(description="First packet (-1 = trace start)")] = -1,
    end: Annotated[int, Field(description="Last packet (-1 = trace end)")] = -1,
    output_file: OutFile = None,
    max_chars: MaxChars = 20000,
) -> dict:
    """Export packets as the suite's human-readable text view (same layout as the GUI) and return it."""
    return await run(AN.export_text, trace, start, end, output_file, max_chars)


@mcp.tool(annotations=RO)
async def trace_export_csv(
    trace: TraceH,
    level: LevelName = "transactions",
    start: Annotated[int, Field(description="First unit at that level (-1 = start)")] = -1,
    end: Annotated[int, Field(description="Last unit (-1 = end)")] = -1,
    output_file: OutFile = None,
    max_rows: Annotated[int, Field(ge=0, le=5000)] = 200,
) -> dict:
    """Export a decode level (packets, transactions, transfers, SCSI operations, ...) to CSV and return the parsed
    header and rows. Best way to read decoded protocol (control transfers, descriptors, endpoints, data)."""
    return await run(AN.export_csv, trace, _level(level), start, end, output_file, max_rows)


@mcp.tool(annotations=RO)
async def trace_export_transactions_with_packets(
    trace: TraceH,
    start: int = -1,
    end: int = -1,
    output_file: OutFile = None,
    max_rows: Annotated[int, Field(ge=0, le=5000)] = 200,
) -> dict:
    """Export USB transactions together with their packets to CSV (ExportTransactionsWithPacketsToCsv)."""
    return await run(AN.export_transactions_with_packets_csv, trace, start, end, output_file, max_rows)


@mcp.tool(annotations=RO)
async def trace_export_transfer_data(
    trace: TraceH,
    address: Annotated[int, Field(ge=0, le=127, description="Device address")],
    endpoint: Annotated[int, Field(ge=0, le=15)],
    direction: Literal["either", "out", "in"] = "either",
    level_from: Literal["packets", "transactions", "transfers"] = "transfers",
    unit_from: int = 0,
    level_to: Literal["packets", "transactions", "transfers"] = "transfers",
    unit_to: int = 2147483647,
    binary: Annotated[bool, Field(description="Write raw payload bytes instead of text")] = False,
    size_kb: Annotated[int, Field(ge=0, description="Max output size (binary)")] = 0,
    output_file: OutFile = None,
    max_chars: MaxChars = 20000,
) -> dict:
    """Extract the data payload of USB transfers for one device endpoint (ExportUSBTransferData)."""
    return await run(AN.export_transfer_data, trace, address, endpoint, C.TRANSFER_DIRECTIONS[direction],
                     C.LEVELS[level_from], unit_from, C.LEVELS[level_to], unit_to, output_file, binary, size_kb, max_chars)


@mcp.tool(annotations=RO)
async def trace_export_visible_transactions_json(trace: TraceH, output_file: OutFile = None, max_chars: MaxChars = 20000) -> dict:
    """Export the currently visible transactions list to JSON (ExportVisibleTraList)."""
    return await run(AN.export_visible_transactions_json, trace, output_file, max_chars)


@mcp.tool(annotations=RO)
async def trace_export_usb4_tunneled(trace: TraceH, protocol: Literal["pcie", "dp", "dp_aux", "usb3"], output_file: str) -> dict:
    """Export tunneled traffic of a USB4 trace to that protocol's native trace format."""
    return {"output_file": await run(AN.export_usb4_tunneled, trace, C.USB4_TUNNELED_PROTOCOLS[protocol], output_file)}


@mcp.tool(annotations=WRITE)
async def trace_save(
    trace: TraceH,
    output_file: OutFile = None,
    start: Annotated[int, Field(description="First packet (-1 = start)")] = -1,
    end: Annotated[int, Field(description="Last packet (-1 = end)")] = -1,
    exclude_hidden: Annotated[bool, Field(description="Drop packets hidden by display options")] = False,
    move: Annotated[bool, Field(description="SaveAs: move this trace handle to the new file (ignores range)")] = False,
) -> dict:
    """Save the trace (or a packet range) to a new .usb file."""
    return {"output_file": await run(AN.save_trace, trace, output_file, start, end, exclude_hidden, move)}


@mcp.tool(annotations=WRITE)
async def trace_apply_display_options(trace: TraceH, opt_file: str) -> dict:
    """Apply a display options (.opt) file to the trace; hidden traffic is then excluded from exports/saves."""

    def work():
        AN.trace(trace).ApplyDisplayOptions(AN._existing(opt_file, "display options file"))

    await run(work)
    return {"applied": opt_file}


@mcp.tool(annotations=WRITE)
async def trace_goto(
    trace: TraceH,
    time_ns: Annotated[float | None, Field(description="Jump trace view to this timestamp")] = None,
    level: Annotated[LevelName | None, Field(description="With index: jump to a unit at this level")] = None,
    index: int | None = None,
) -> dict:
    """Navigate the suite GUI trace view to a time or unit (visible to a human watching the screen)."""

    def work():
        t = AN.trace(trace)
        if time_ns is not None:
            t.GotoTime(float(time_ns))
        elif level is not None and index is not None:
            t.GotoUnit(_level(level), int(index))
        else:
            raise AnalyzerError("Give time_ns or level+index")

    await run(work)
    return {"ok": True}


@mcp.tool(annotations=RO)
async def trace_run_verification_script(
    trace: TraceH,
    script: Annotated[str, Field(description="Verification script name (in Scripts\\VFScripts) or full path to a .vse file")],
    set_vars: Annotated[dict[str, Any] | None, Field(description="Script globals to set before running (int/str/list)")] = None,
    get_vars: Annotated[list[str] | None, Field(description="Script globals to read back after running")] = None,
) -> dict:
    """Run a Verification Script Engine (.vse, CSL) script over the trace. Result: PASSED/FAILED/DONE/... ."""
    return await run(AN.run_verification_script, trace, script, set_vars, get_vars)


@mcp.tool(annotations=WRITE)
async def trace_add_comment(trace: TraceH, comment: str) -> dict:
    """Append text to the trace's Comment field (Trace Information)."""
    await run(lambda: AN.trace(trace).AddComments(comment))
    return {"ok": True}


@mcp.tool(annotations=WRITE)
async def trace_load_decoding_settings(trace: TraceH, xml_file: str) -> dict:
    """Load saved decoding settings (.xml) into a trace (USB4 traces; decoding must be finished)."""
    await run(lambda: AN.trace(trace).LoadDecodingSettings(AN._existing(xml_file)))
    return {"ok": True}


@mcp.tool(annotations=WRITE)
async def trace_merge_files(file_1: str, file_2: str, output_file: OutFile = None) -> dict:
    """Merge two trace files into one (files must not be open)."""
    return {"output_file": await run(AN.merge_trace_files, file_1, file_2, output_file or "")}


@mcp.tool(annotations=WRITE)
async def import_simpass(simpass_file: str) -> dict:
    """Import a SimPass file into the suite."""
    await run(AN.import_simpass, simpass_file)
    return {"ok": True}


# =========================================================================== reference & raw access


REFERENCE = {
    "hardware_types": C.HARDWARE_TYPES,
    "levels": C.LEVELS,
    "trace_error_types": C.TRACE_ERROR_TYPES,
    "packet_error_bits": C.PACKET_ERROR_BITS,
    "packet_speeds": C.PACKET_SPEEDS,
    "packet_channels": C.PACKET_CHANNELS,
    "usb3_packet_types": C.USB3_PACKET_TYPES,
    "usb2_packet_types": C.USB2_PACKET_TYPES,
    "usb2_event_types": C.USB2_EVENT_TYPES,
    "verification_results": C.VS_RESULTS,
    "recording_states": C.RECORDING_STATES,
    "generation_states": C.GENERATION_STATES,
}


@mcp.tool(annotations=RO)
async def api_reference(topic: Literal[tuple(REFERENCE)] | None = None) -> dict:  # type: ignore[valid-type]
    """Enumerations used by the analyzer API (packet types, error bits, levels, recording option setting ids, ...)."""
    if topic is None:
        return {"topics": list(REFERENCE)}
    return {topic: {str(k): v for k, v in REFERENCE[topic].items()}}


ComTarget = Annotated[str, Field(description="'analyzer', 'recording_options', 'trace:<handle>' or 'packet:<handle>:<number>'")]


@mcp.tool(annotations=RO)
async def com_list_members(target: ComTarget = "analyzer") -> dict:
    """List the COM methods/properties available on an automation object (from UsbAutomation.tlb)."""
    return {"target": target, "members": await run(AN.describe_members, target)}


@mcp.tool(annotations=BUS)
async def com_invoke(
    target: ComTarget,
    member: Annotated[str, Field(description="Method or property name, e.g. 'GetConnectedAnalyzerNumber'")],
    args: Annotated[list[Any] | None, Field(description="Positional arguments")] = None,
    kind: Literal["call", "get", "put"] = "call",
) -> dict:
    """Raw access to a documented Automation API method not wrapped by a dedicated tool. Returned trace objects
    are registered and returned as handles. Use with care."""
    return {"result": await run(AN.invoke, target, member, list(args or []), kind)}


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--output-dir", default=None, help="Directory for exports/reports (env USB_ANALYZER_MCP_OUTPUT_DIR)")
    ns = p.parse_args()
    if ns.output_dir:
        os.environ["USB_ANALYZER_MCP_OUTPUT_DIR"] = ns.output_dir
    try:
        if ns.transport == "stdio":
            mcp.run("stdio")
        else:
            mcp.run(ns.transport, host=ns.host, port=ns.port)
    finally:
        try:
            COM.call(AN.disconnect)
        except Exception:
            pass
        COM.stop()


if __name__ == "__main__":
    main()
