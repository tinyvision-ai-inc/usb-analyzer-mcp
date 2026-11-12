# usb-analyzer-mcp

An [MCP](https://modelcontextprotocol.io) server that lets AI agents (Claude Code, Claude Desktop, or any MCP client)
drive a USB protocol analyzer through the automation API of **Teledyne LeCroy USB Protocol Suite**.

It was developed and tested against a Voyager M310P (USB 3.2) with USB Protocol Suite 10.50. It uses only the suite's
documented customer automation interface, so other analyzers that the suite supports should also work.

> **Not affiliated with Teledyne LeCroy.** This is an independent, community project. It is not developed, endorsed,
> or supported by Teledyne LeCroy, Inc. "Teledyne LeCroy", "LeCroy", "Voyager", "Mercury", "Advisor" and
> "USB Protocol Suite" are trademarks of their respective owners and are used here only to describe compatibility.

## What you need (and what this repo does *not* include)

* A licensed copy of **USB Protocol Suite** installed on Windows, and your own analyzer hardware. You get both from
  Teledyne LeCroy under their EULA. This repo contains **none** of their software, documentation, type libraries,
  sample scripts or traces. It talks to whatever suite you have installed, through the COM server that the suite registers.
* Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

Your use of the vendor software is governed by your own agreement with Teledyne LeCroy, including its single-site
license terms. This project's MIT license covers only the code in this repository.

## Install

```powershell
winget install astral-sh.uv
git clone https://github.com/tinyvision-ai-inc/usb-analyzer-mcp
cd usb-analyzer-mcp
uv sync
```

### Claude Code
The repo has a project-scoped [.mcp.json](.mcp.json). Open Claude Code in the checkout and approve the
`usb-analyzer` server. To use it from any project instead:

```powershell
claude mcp add usb-analyzer --scope user -- uv run --directory C:\path\to\usb-analyzer-mcp usb-analyzer-mcp
```

### Other MCP clients (e.g. Claude Desktop)
```json
{
  "mcpServers": {
    "usb-analyzer": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\usb-analyzer-mcp", "usb-analyzer-mcp"]
    }
  }
}
```

### HTTP transport (same lab only)
```powershell
uv run usb-analyzer-mcp --transport streamable-http --host 127.0.0.1 --port 8765
```
There is no authentication. Bind it to localhost or a trusted lab network you control. Do not use it to share one
licensed analyzer PC with users at other sites; check your vendor license first.

### Tailscale (tailnet-only access)
[scripts/serve-tailnet.ps1](scripts/serve-tailnet.ps1) binds the HTTP server to this machine's Tailscale IPv4 only,
so it never listens on LAN or public interfaces. Tailscale Funnel/Serve isn't used. One-time setup, from an elevated
PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-tailnet.ps1   # firewall: tailnet ranges only + logon task
Start-ScheduledTask -TaskName "usb-analyzer-mcp (tailnet)"
```

Agents on other tailnet machines then add `http://<machine>.<tailnet>.ts.net:8765/mcp`, e.g.
`claude mcp add --transport http usb-analyzer http://<machine>:8765/mcp`. Logs are written to
`%LOCALAPPDATA%\usb-analyzer-mcp\`. Remove the setup with `install-tailnet.ps1 -Uninstall`. Every device in the
tailnet can reach the server; use Tailscale ACLs to narrow that down.

## Tools

| Area | Tools |
|---|---|
| Device | `analyzer_connect`, `analyzer_info` (SW/BE/FW versions, serial, model, SSP), `analyzer_check_license`, `analyzer_events`, `analyzer_is_restart_required`, `analyzer_restart`, `analyzer_set_persistent_decoding` |
| Bus control ⚠ | `usb_unplug_plug` (1 s VBUS break), `usb3_trainer_set_vbus` |
| Recording | `recording_options_build` (writes a `.rec`: mode, buffer, post-trigger, truncation, …), `recording_options_apply`, `recording_start`, `recording_status`, `recording_wait`, `recording_manual_trigger`, `recording_stop` (returns a trace handle), `recording_make` (blocking), `display_options_load` |
| Exercisers ⚠ | `usb2_generation`, `usb3_exerciser`, `pd_exerciser`, `usb4_exerciser` (start/stop/resume/wait; scripts are authored in the vendor suite) |
| Trace analysis | `trace_open`, `trace_list`, `trace_close`, `trace_summary`, `trace_wait_decode`, `trace_get_packets`, `trace_get_packet_detail`, `trace_get_field`, `trace_find_packets`, `trace_errors`, `trace_power_info` |
| Reports & export | `trace_report` (error summary / traffic summary / file info), `trace_export_text`, `trace_export_csv` (packets, transactions, transfers, SCSI, PTP …), `trace_export_transactions_with_packets`, `trace_export_transfer_data`, `trace_export_visible_transactions_json`, `trace_export_usb4_tunneled`, `trace_save`, `trace_merge_files` |
| Scripting & view | `trace_run_verification_script` (with script variables), `trace_apply_display_options`, `trace_goto`, `trace_add_comment`, `trace_load_decoding_settings`, `import_simpass` |
| Reference / raw | `api_reference`, `com_list_members`, `com_invoke` (documented API members only, see below) |

⚠ These tools carry the MCP `destructive` annotation. The server's instructions tell agents to use them only when the user asks.

Traces are referred to by integer **handles**. Packet numbers are zero-based. Exports and reports go to the output
directory and are also returned inline, truncated to a limit. The default output directory is
`<suite data folder>\mcp_output`. Override it with `--output-dir` or `USB_ANALYZER_MCP_OUTPUT_DIR`.

### Documented API only, by default
The server sticks to the methods described in the vendor's public Automation API manual. Members that the manual
marks internal or restricted, or that it doesn't document at all, are refused. That covers raw recording-option
setting IDs, the compliance and test interfaces, unit binding, CC-line power queries, and API-driven PD pause.
An operator who has a reason to use them on their own licensed installation can turn them on:

```powershell
$env:USB_ANALYZER_MCP_ENABLE_RESTRICTED = "1"
```

### Example prompts
* "Which analyzer is connected, and what firmware does it run?"
* "Start a capture. I'll plug the board in. Then stop and tell me whether enumeration succeeded."
* "Open `C:\traces\fail.usb` and list every STALL and every packet with a CRC error, with timestamps."
* "Export the payload of the transfers on address 5, endpoint 1 IN to a binary file."

## How it works

```
MCP client ──stdio/HTTP──> MCPServer (asyncio)
                               │  every COM call is marshalled onto
                               ▼
                         ComThread (STA, pumps messages; receives analyzer events)
                               │  pywin32
                               ▼
                    USB Protocol Suite (out-of-process COM server)
                               │
                               ▼
                          analyzer hardware
```

* [src/usb_analyzer_mcp/com_thread.py](src/usb_analyzer_mcp/com_thread.py): one apartment-threaded COM thread. COM objects never leave it.
* [src/usb_analyzer_mcp/analyzer.py](src/usb_analyzer_mcp/analyzer.py): a synchronous wrapper that also holds the trace-handle registry.
* [src/usb_analyzer_mcp/server.py](src/usb_analyzer_mcp/server.py): the MCP tools.
* [src/usb_analyzer_mcp/constants.py](src/usb_analyzer_mcp/constants.py): interface enum values and the documented-member allowlist.

## Behaviour notes (observed with suite 10.50 + Voyager M310P)

* **Startup:** the first tool call starts the suite if it isn't running, then waits up to ~20 s for the unit (usually ~4 s). A suite that was started through automation exits when the server lets go of it. Save any work in the suite GUI before you start an automated session.
* **Reconnect:** if the suite is closed, the next call reconnects. Trace handles are reset when that happens.
* **Speed:** the API's packet speed value doesn't distinguish full speed from high speed, so `trace_get_packets` takes speed from the suite's CSV decode.
* **Errors:** the per-category error collection came back empty or failed on our test traces. `trace_errors` uses the suite's error-summary report plus the error-flagged packets instead. The collection is still available as `legacy_collection`.
* **Packet fields:** values from the field API are masked to the field width. On one older sample trace they didn't match the GUI decode, so treat `trace_export_csv` / `trace_export_text` as the authoritative decode.
* **Speed of access:** per-packet calls cost ~25–40 ms because the suite runs out of process. Search and error listing use the CSV export instead, at roughly 1 s per few thousand packets.
* **Licenses:** `analyzer_check_license` needs the exact feature title shown in the suite's License dialog.

## Tests

```powershell
uv run pytest -m suite      # trace analysis on a sample trace from your suite install (no hardware needed)
uv run pytest -m hardware   # passive capture on a connected analyzer (no VBUS / exerciser / restart)
```

The tests launch the real server over stdio and use the MCP client SDK. They read sample files from your own suite
installation and skip themselves if those files aren't there. Optional settings:
`USB_ANALYZER_EXPECT_HW="Voyager M310P"`, and `USB_ANALYZER_LICENSE_FEATURE=<title>` to check one real license.

## License

[MIT](LICENSE) for the code in this repository. The vendor software it automates is licensed separately by its owner.
