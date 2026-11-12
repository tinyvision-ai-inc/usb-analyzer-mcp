"""Interface constants (enum values and member names) of the USB Protocol Suite
customer automation API, as needed to call it, plus the documented-member allowlist."""

from __future__ import annotations

PROG_ID = "CATC.UsbTracer"

VERSION_TYPES = {"software": 0, "bus_engine": 1, "firmware": 2}

HARDWARE_TYPES = {
    0: "5K", 1: "2500", 2: "2500H", 3: "Advisor", 4: "Mobile HS", 5: "Mobile T2",
    6: "Voyager", 7: "Advisor T3", 8: "Voyager M3x", 9: "Voyager M310", 10: "reserved",
    11: "Mercury T2", 12: "reserved2", 13: "Voyager M310C", 14: "Mercury T2C",
    15: "Mercury T2P", 16: "Voyager M310P", 17: "Voyager M4x", 18: "Voyager M310e",
    19: "Voyager M480x", 20: "Voyager M4120",
}

# ERecModes
REC_MODES = {"snapshot": 0, "manual_trigger": 1, "event_trigger": 2}

# WaitForRecordingStatus desired_status bits
RSTAT = {"done": 0x4000, "triggered": 0x2000, "waiting_for_sync_rec": 0x1000}

# _IAnalyzerEvents::OnStatusReport
STATUS_SUBSYSTEMS = {1: "recording", 2: "generation"}
RECORDING_STATES = {
    -1: "idle", 0: "waiting_for_trigger", 1: "recording_triggered", 2: "uploading_data",
    3: "saving_data", 8: "recording_and_uploading",
}
GENERATION_STATES = {400: "generator_idle", 401: "generator_downloading", 402: "generating", 403: "generator_paused"}

# Transaction / decode levels (GotoUnit, ExportToCsv, IsLevelDecodeFinished)
LEVELS = {
    "packets": 0, "transactions": 1, "split_transactions": 2, "transfers": 3,
    "host_wa_segments": 4, "host_wa_transfers": 5, "device_wa_segments": 6, "device_wa_transfers": 7,
    "ptp_transactions": 8, "ptp_objects": 9, "ptp_sessions": 10, "scsi_operations": 11,
    "ptp_groups": 13, "obex": 14,
}

# ITrace::AnalyzerErrors error_type
TRACE_ERROR_TYPES = {
    "pid": 0x1, "crc5": 0x2, "crc16": 0x4, "packet_length": 0x8, "stuff_bit": 0x10,
    "eop": 0x20, "babble_start": 0x40, "babble_end_loa": 0x80, "frame_length": 0x100,
    "handshake_timeout": 0x200, "analyzer_internal": 0x400, "data_toggle": 0x800,
    "microframe": 0x1000, "short_byte": 0x2000,
}

# IUsbPacket::GetErrorsBitmap bit -> name
PACKET_ERROR_BITS = {
    1: "USB2 PID", 2: "USB2 CRC5", 3: "USB2 CRC16", 4: "USB2 Packet Length", 5: "USB2 Bit Stuff",
    6: "USB2 EOP", 7: "USB2 Babble Start", 8: "USB2 Babble End", 9: "USB2 Frame Length",
    10: "USB2 Turnaround/Timeout", 11: "USB2 Analyzer Internal", 12: "USB2 Data Toggle",
    13: "USB2 Frame/uFrame Number", 14: "USB2 Last Byte Incomplete", 15: "USB2 OTG Signal",
    16: "USB3 CRC5", 17: "USB3 CRC16", 18: "USB3 CRC32", 19: "USB3 Disparity", 20: "USB3 10 Bit Symbol",
    21: "USB3 Unknown Packet (IPS)", 22: "USB3 Framing Symbol", 23: "USB3 LC Data Symbol",
    24: "USB3 Bad Header Packet Length", 25: "USB3 Bad Data Length Field", 26: "USB3 SKP Symbol",
    27: "USB3 Control Endpoint Direction", 28: "USB3 Missed DPH", 29: "USB3 Missed DPP",
    30: "USB3 Setup DP", 31: "USB3 Sequence Number", 45: "USB3 TP Non-Zero Reserved",
    46: "USB3 Missed ITP", 47: "USB3 Late/Early ITP",
}

# Documented as 0=HS, 1=FS, 2=LS, 3=unknown, 4=SS, but suite 10.50 returns 0 for
# both FS and HS USB2 packets, so the CSV 'Sp' column is preferred when available.
PACKET_SPEEDS = {0: "usb2 (hs/fs)", 1: "full_speed", 2: "low_speed", 3: "unknown", 4: "super_speed"}
CSV_SPEEDS = {"H": "high_speed", "F": "full_speed", "L": "low_speed", "G1": "superspeed_gen1", "G2": "superspeed_plus_gen2"}

PACKET_CHANNELS = {
    0: "USB2 ch0", 1: "USB2 ch1", 2: "USB2 ch2", 3: "USB2 ch3",
    4: "USB3 Rx ch0", 5: "USB3 Tx ch0", 6: "USB3 Rx ch1", 7: "USB3 Tx ch1",
}

USB3_PACKET_TYPES = {
    0: "Unknown", 1: "TSEQ", 2: "TSX", 3: "LFPS", 4: "Electrical Idle", 5: "IPS", 6: "LC", 7: "SKP",
    8: "HP", 9: "DPP", 10: "Logical Idle", 11: "TS1", 12: "TS2", 13: "DP", 14: "BRST", 15: "BERC",
    16: "BCNT", 17: "TERM", 18: "Deferred Packet", 19: "CP",
}
USB2_PACKET_TYPES = {
    -1: "Unknown", 0: "MDATA", 1: "DATA2", 2: "DATA1", 3: "DATA0", 4: "SETUP", 5: "SOF", 6: "IN",
    7: "OUT", 8: "STALL", 9: "NYET", 10: "NAK", 11: "ACK", 12: "PRE/ERR", 13: "PING", 14: "SPLIT",
    15: "EXT", 19: "LPM",
}
USB2_EVENT_TYPES = {
    -1: "Unknown", 0: "Chirp K", 1: "Chirp J", 2: "FS K on HS", 3: "FS J on HS", 4: "Suspend",
    5: "Resume", 6: "SE1", 7: "SE0", 27: "Connect", 28: "VBUS Voltage Change", 30: "Keep Alive",
    31: "Reset",
}

VS_RESULTS = {
    -5: "UNKNOWN", -4: "LICENSE_FAIL", -3: "PARSE_ERROR", -2: "SCRIPT_RUNNING", -1: "SCRIPT_NOT_FOUND",
    0: "FAILED", 1: "PASSED", 2: "DONE", 3: "PASSED_WARNING", 4: "NOT_APPLICABLE", 5: "PASSED_WITH_NOTE",
}

USB4_TUNNELED_PROTOCOLS = {"pcie": 0, "dp": 1, "dp_aux": 2, "usb3": 3}

TRANSFER_DIRECTIONS = {"either": 1, "out": 2, "in": 3}

# IUsbAnalyzer18::IsRestartRequired / RestartAnalyzer
DEVICE_FUNCTIONALITY = {"force_restart": 1, "usb4_tbt3_recording": 2, "usb3_recording": 4}


# --------------------------------------------------------------------------
# Members documented in the public Automation API manual. Anything else in the
# type library (compliance/test interfaces, members the manual marks internal or
# restricted) is only reachable when the operator opts in with
# USB_ANALYZER_MCP_ENABLE_RESTRICTED=1 on their own licensed installation.

RESTRICTED_ENV = "USB_ANALYZER_MCP_ENABLE_RESTRICTED"

DOCUMENTED_MEMBERS = {
    "analyzer": {
        "GetVersion", "GetSerialNumber", "OpenFile", "StartGeneration", "StopGeneration", "StartRecording",
        "StopRecording", "MakeRecording", "LoadDisplayOptions", "GetRecordingOptions", "UsbUnplugPlug",
        "ApplicationFolder", "ApplicationDataFolder", "StopRecordingAndWaitForTrace", "StartUsb3Generation",
        "StopUsb3Generation", "PauseUsb3Generation", "ResumeUsb3Generation", "IssueManualTrig",
        "GetRecordingStatus", "ResetUsb3Trainer", "IsUsb3GenerationIdle", "SwitchVBus", "MergeTraceFiles",
        "WaitForUsb3GenerationIdle", "WaitForRecordingStatus", "GetLicensedCapability", "GetHardwareInfo",
        "IsSupportSSP", "ApplyRecordingOptions", "SetPersistentDecoding", "GetPersistentDecoding",
        "StartPDGeneration", "StopPDGeneration", "IsPDGenerationIdle", "ResumePDGeneration",
        "WaitForPDGenerationIdle", "WaitForPDGenerationPause", "WaitForPDGenerationPauseTag",
        "StartUSB4Generation", "StopUSB4Generation", "ResumeUSB4Generation", "WaitForUSB4GenerationIdle",
        "WaitForUSB4GenerationPauseTag", "GetConnectedAnalyzersSN", "IsRestartRequired", "RestartAnalyzer",
        "ImportSimPass", "OpenUserFile", "CloseUserFile", "SeekUserFile", "GetUserFileSize", "WriteUserFile",
        "ReadUserFile", "CreateUserFolder",
    },
    "recording_options": {
        "Load", "Save", "Reset", "SetRecMode", "SetBufferSize", "SetPostTriggerPercentage", "SetTriggerBeep",
        "SetDataTruncate", "SetAutoMerge", "SetSaveExternalSignals", "SetTraceFileName", "SetFilterPolarity",
        "SetBufferSizeEx",
    },
    "trace": {
        "GetName", "ApplyDisplayOptions", "Save", "ExportToText", "Close", "ReportFileInfo", "ReportErrorSummary",
        "ReportTrafficSummary", "GetPacket", "GetPacketsCount", "GetTriggerPacketNum", "AnalyzerErrors", "SaveAs",
        "GotoTime", "ExportToCsv", "GotoUnit", "GetPowerInfoByTime", "GetPowerInfoByPacket", "GetFullName",
        "GetUsbPacket", "IsLevelDecodeFinished", "SetTag", "GetTag", "GetPacketEx", "GetPacketsCountEx",
        "GetTriggerPacketNumEx", "GetUsbPacketEx", "GetFullPathAndName", "ExportTransactionsWithPacketsToCsv",
        "SaveTraceAs", "ExportUSBTransferData", "ExportUSB4TunneledProtocolTraffic", "ExportVisibleTraList",
        "LoadDecodingSettings", "RunVerificationScript", "GetVScriptEngine", "AddComments",
    },
    "packet": {
        "GetTimestamp", "GetDuration", "GetSpeed", "GetChannel", "GetType", "GetFieldValue", "GetAllFieldsValues",
        "GetMarker", "GetErrorsBitmap", "GetRawData", "GetUsb3ScrambledData", "GetUsb3TenBitData",
    },
}


def restricted_enabled() -> bool:
    import os

    return os.environ.get(RESTRICTED_ENV, "").strip().lower() in ("1", "true", "yes", "on")
