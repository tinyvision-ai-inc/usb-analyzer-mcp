"""Synchronous wrapper around the USB Protocol Suite COM automation API.

Every method in this module must run on the COM thread (see com_thread.py);
the MCP layer takes care of that.
"""

from __future__ import annotations

import csv
import io
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pythoncom
import pywintypes
import win32com.client
from win32com.client import gencache

from . import constants as C
from .com_thread import pump_for


class AnalyzerError(RuntimeError):
    pass


# HRESULTs meaning the out-of-process UsbSuite.exe is gone / going away
RPC_DEAD = {
    0x800706BA,  # RPC_S_SERVER_UNAVAILABLE
    0x800706BE,  # RPC_S_CALL_FAILED
    0x800706BF,  # RPC_S_CALL_FAILED_DNE
    0x80010108,  # RPC_E_DISCONNECTED
    0x80010007,  # RPC_E_SERVER_DIED
    0x80010012,  # RPC_E_SERVER_DIED_DNE
}


# --------------------------------------------------------------------------- helpers


def com_error_text(exc: BaseException) -> str:
    if isinstance(exc, pywintypes.com_error):
        hr, msg, excepinfo, _ = exc.args
        if excepinfo and excepinfo[2]:
            return f"{excepinfo[2]} (hresult=0x{(excepinfo[5] or hr) & 0xFFFFFFFF:08X})"
        return f"{msg} (hresult=0x{hr & 0xFFFFFFFF:08X})"
    return str(exc)


def jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (tuple, list)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex(" ")
    if isinstance(v, pywintypes.TimeType):
        return v.isoformat()
    if hasattr(v, "_oleobj_") or type(v).__name__ in ("PyIDispatch", "PyIUnknown"):
        return f"<COM object {type(v).__name__}>"
    return repr(v)


def bcd_version(v: int) -> str:
    return f"{(v >> 8) & 0xFF:X}.{v & 0xFF:02X}"


def bytes_hex(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, (bytes, bytearray, memoryview)):
        return bytes(values).hex(" ")
    if isinstance(values, (int,)):
        return f"{values:02x}"
    return " ".join(f"{int(b) & 0xFF:02x}" for b in values)


def field_value_str(value: Any, bits: Any) -> str | None:
    """Fields <= 32 bits come back as a DWORD (upper bits may hold junk), larger ones as a byte array."""
    if value is None:
        return None
    if isinstance(value, (tuple, list, bytes, bytearray, memoryview)):
        return bytes_hex(value)
    iv = int(value) & 0xFFFFFFFF
    try:
        nb = int(bits)
        if 0 < nb < 32:
            iv &= (1 << nb) - 1
    except (TypeError, ValueError):
        pass
    return f"0x{iv:X}"


def read_text_file(path: Path, max_chars: int | None) -> tuple[str, bool]:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("mbcs", errors="replace")
    if max_chars is not None and max_chars >= 0 and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


@dataclass
class TraceEntry:
    handle: int
    obj: Any
    path: str
    origin: str
    opened_at: float = field(default_factory=time.time)


class _AnalyzerEvents:
    """Sink for _IAnalyzerEvents; instance attributes are set by UsbAnalyzer."""

    owner: "UsbAnalyzer"

    def OnTraceCreated(self, trace):  # noqa: N802 - COM event name
        try:
            self.owner._on_trace_created(trace)
        except Exception:  # never let an exception escape into COM
            pass

    def OnStatusReport(self, subsystem, state, percent_done):  # noqa: N802
        try:
            self.owner._on_status(int(subsystem), int(state), int(percent_done))
        except Exception:
            pass


# --------------------------------------------------------------------------- main wrapper


class UsbAnalyzer:
    def __init__(self, output_dir: str | None = None, connect_timeout_s: float = 20.0) -> None:
        self._a: Any = None
        self._events: Any = None
        self._ro: Any = None
        self._traces: dict[int, TraceEntry] = {}
        self._next_handle = 1
        self._lock = threading.RLock()
        self._status_log: list[dict] = []
        self._last_status: dict[str, dict] = {}
        self._connect_timeout_s = connect_timeout_s
        self._output_dir_override = output_dir

    # ------------------------------------------------------------ connection

    @property
    def com(self) -> Any:
        if self._a is None:
            self.connect()
        return self._a

    def _server_alive(self) -> bool:
        try:
            self._a.GetConnectedAnalyzersSN()
            return True
        except pywintypes.com_error as exc:
            return (exc.args[0] & 0xFFFFFFFF) not in RPC_DEAD

    def _drop_dead_server(self) -> None:
        self._traces.clear()
        self._a = self._events = self._ro = None

    def connect(self, wait_for_device_s: float | None = None) -> dict:
        if self._a is not None and not self._server_alive():
            self._drop_dead_server()
            self._status_log.append({"time": time.time(), "warning": "COM server had exited; reconnected (trace handles reset)"})
        if self._a is None:
            deadline = time.monotonic() + 30
            while True:
                try:
                    self._a = gencache.EnsureDispatch(C.PROG_ID)
                    break
                except pywintypes.com_error as exc:
                    # A suite instance that is shutting down rejects calls for a few seconds.
                    if (exc.args[0] & 0xFFFFFFFF) in RPC_DEAD and time.monotonic() < deadline:
                        pump_for(1.0)
                        continue
                    raise AnalyzerError(
                        f"Unable to start/attach USB Protocol Suite COM server ({C.PROG_ID}): {com_error_text(exc)}"
                    ) from exc
            try:
                self._events = win32com.client.WithEvents(self._a, _AnalyzerEvents)
                self._events.owner = self
            except Exception as exc:  # events are best effort
                self._events = None
                self._status_log.append({"time": time.time(), "warning": f"event sink failed: {exc}"})
        wait = self._connect_timeout_s if wait_for_device_s is None else wait_for_device_s
        deadline = time.monotonic() + max(0.0, wait)
        sns: tuple = ()
        while True:
            try:
                sns = tuple(self._a.GetConnectedAnalyzersSN() or ())
            except pywintypes.com_error:
                sns = ()
            if sns or time.monotonic() >= deadline:
                break
            pump_for(0.5)
        return {"com_server": C.PROG_ID, "connected_analyzers": [int(s) for s in sns]}

    def disconnect(self) -> None:
        for h in list(self._traces):
            try:
                self.close_trace(h)
            except Exception:
                pass
        self._ro = None
        self._events = None
        self._a = None

    @staticmethod
    def require_restricted(what: str) -> None:
        if not C.restricted_enabled():
            raise AnalyzerError(
                f"{what} is outside the documented customer API (internal/undocumented in the vendor manual) "
                f"and is disabled. The operator can enable it on their own licensed installation with "
                f"{C.RESTRICTED_ENV}=1."
            )

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except pywintypes.com_error as exc:
            if (exc.args[0] & 0xFFFFFFFF) in RPC_DEAD:
                self._drop_dead_server()
                raise AnalyzerError(
                    "USB Protocol Suite COM server is gone (was the suite closed?). "
                    "The next call reconnects; trace handles were reset - reopen traces."
                ) from exc
            raise AnalyzerError(com_error_text(exc)) from exc

    # ------------------------------------------------------------ events

    def _on_trace_created(self, trace) -> None:
        obj = trace
        if not hasattr(obj, "GetName"):
            obj = win32com.client.Dispatch(trace)
        try:
            path = obj.GetFullPathAndName()
        except Exception:
            path = ""
        h = self._register_trace(obj, path, "recording_event")
        self._status_log.append({"time": time.time(), "event": "trace_created", "trace": h, "path": path})

    def _on_status(self, subsystem: int, state: int, percent: int) -> None:
        sub = C.STATUS_SUBSYSTEMS.get(subsystem, str(subsystem))
        names = C.RECORDING_STATES if subsystem == 1 else C.GENERATION_STATES
        rec = {
            "time": time.time(),
            "event": "status",
            "subsystem": sub,
            "state": state,
            "state_name": names.get(state, "unknown"),
            "percent_done": percent,
        }
        self._last_status[sub] = rec
        if not self._status_log or self._status_log[-1].get("state") != state or self._status_log[-1].get("subsystem") != sub:
            self._status_log.append(rec)
        del self._status_log[:-200]

    def events(self, clear: bool = False, pump_s: float = 0.2) -> dict:
        pump_for(pump_s)
        out = {"last_status": dict(self._last_status), "log": list(self._status_log)}
        if clear:
            self._status_log.clear()
        return out

    # ------------------------------------------------------------ output dir

    @property
    def output_dir(self) -> Path:
        base = self._output_dir_override or os.environ.get("USB_ANALYZER_MCP_OUTPUT_DIR")
        if not base:
            try:
                base = str(Path(self.com.ApplicationDataFolder) / "mcp_output")
            except Exception:
                base = str(Path(os.environ.get("PUBLIC", "C:/Users/Public")) / "Documents" / "usb_analyzer_mcp_output")
        p = Path(base)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def out_path(self, output_file: str | None, default_stem: str, ext: str) -> Path:
        if output_file:
            p = Path(output_file)
            if not p.is_absolute():
                p = self.output_dir / p
        else:
            p = self.output_dir / f"{default_stem}_{time.strftime('%Y%m%d_%H%M%S')}{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @staticmethod
    def _existing(path: str, what: str = "file") -> str:
        p = Path(path)
        if not p.exists():
            raise AnalyzerError(f"{what} not found: {path}")
        return str(p.resolve())

    # ------------------------------------------------------------ device info

    def info(self) -> dict:
        a = self.com
        res: dict[str, Any] = {}

        def grab(key, fn):
            try:
                res[key] = fn()
            except pywintypes.com_error as exc:
                res[key] = {"error": com_error_text(exc)}

        grab("software_version", lambda: bcd_version(a.GetVersion(0)))
        grab("bus_engine_version", lambda: bcd_version(a.GetVersion(1)))
        grab("firmware_version", lambda: bcd_version(a.GetVersion(2)))
        grab("serial_number", lambda: int(a.GetSerialNumber()))
        grab("connected_analyzers", lambda: [int(s) for s in (a.GetConnectedAnalyzersSN() or ())])

        def hw():
            code = int(a.GetHardwareInfo())
            return {"code": code, "name": C.HARDWARE_TYPES.get(code, "unknown")}

        grab("hardware", hw)
        grab("supports_usb31_ssp", lambda: bool(a.IsSupportSSP()))
        grab("persistent_decoding", lambda: bool(a.GetPersistentDecoding()))
        grab("application_folder", lambda: a.ApplicationFolder)
        grab("application_data_folder", lambda: a.ApplicationDataFolder)
        res["output_dir"] = str(self.output_dir)
        res["open_traces"] = len(self._traces)
        return res

    def is_licensed(self, feature_title: str) -> bool:
        return bool(self._call(self.com.GetLicensedCapability, feature_title))

    def is_restart_required(self, functionality: int) -> bool:
        res = self._call(self.com.IsRestartRequired, functionality)
        return bool(res)

    def restart_analyzer(self, functionality: int, wait_for_device_s: float) -> dict:
        self._call(self.com.RestartAnalyzer, functionality)
        # The manual says the analyzer object must be recreated after a restart.
        for h in list(self._traces):
            self._traces.pop(h, None)
        self._a = None
        self._events = None
        self._ro = None
        pump_for(2.0)
        return self.connect(wait_for_device_s)

    def set_persistent_decoding(self, enable: bool) -> None:
        self._call(self.com.SetPersistentDecoding, 1 if enable else 0)

    # ------------------------------------------------------------ recording

    def recording_status(self) -> dict:
        res: dict[str, Any] = {}
        try:
            trig, done = self.com.GetRecordingStatus()
            res.update(triggered=bool(trig), done=bool(done))
        except pywintypes.com_error as exc:
            res["error"] = com_error_text(exc)
        pump_for(0.05)
        res["last_events"] = dict(self._last_status)
        return res

    def start_recording(self, rec_options_file: str | None, keep_current_options: bool) -> None:
        if keep_current_options:
            arg = "DO_NOT_CHANGE_RECORDING_OPTIONS"
        elif rec_options_file:
            arg = self._existing(rec_options_file, "recording options file")
        else:
            arg = ""
        self._call(self.com.StartRecording, arg)

    def stop_recording(self, abort_upload: bool) -> None:
        self._call(self.com.StopRecording, 1 if abort_upload else 0)

    def stop_recording_and_wait_for_trace(self) -> TraceEntry:
        t = self._call(self.com.StopRecordingAndWaitForTrace)
        return self._register_dispatch(t, "recording")

    def make_recording(self, rec_options_file: str | None) -> TraceEntry:
        arg = self._existing(rec_options_file, "recording options file") if rec_options_file else ""
        t = self._call(self.com.MakeRecording, arg)
        return self._register_dispatch(t, "make_recording")

    def wait_for_recording_status(self, timeout_ms: int, desired: int) -> dict:
        spent, done = self._call(self.com.WaitForRecordingStatus, timeout_ms, desired)
        return {"reached": bool(done), "spent_ms": int(spent or 0)}

    def manual_trigger(self) -> None:
        self._call(self.com.IssueManualTrig)

    def apply_recording_options(self, rec_options_file: str) -> None:
        self._call(self.com.ApplyRecordingOptions, self._existing(rec_options_file, "recording options file"))

    def load_display_options(self, opt_file: str) -> None:
        self._call(self.com.LoadDisplayOptions, self._existing(opt_file, "display options file"))

    @property
    def rec_options(self) -> Any:
        if self._ro is None:
            self._ro = self._call(self.com.GetRecordingOptions)
        return self._ro

    def build_recording_options(
        self,
        output_file: str,
        base_file: str | None,
        reset: bool,
        rec_mode: int | None,
        buffer_size_bytes: int | None,
        buffer_size_mb: int | None,
        post_trigger_percent: int | None,
        trigger_beep: bool | None,
        data_truncate_bytes: int | None,
        auto_merge: bool | None,
        save_external_signals: bool | None,
        trace_file_name: str | None,
        filter_out: bool | None,
        settings: dict[int, int] | None,
        string_settings: dict[int, str] | None,
        bypass_lru: bool | None,
    ) -> dict:
        if settings:
            self.require_restricted("raw recording-option settings (ChangeUsbRecordingOptionSetting)")
        if string_settings:
            self.require_restricted("string recording-option settings (ChangeUsbRecordingOptionStringSettings)")
        if bypass_lru is not None:
            self.require_restricted("SetEnableBypassLRU")
        ro = self.rec_options
        applied: list[str] = []
        results: dict[str, Any] = {}
        if base_file:
            self._call(ro.Load, self._existing(base_file, "recording options file"))
            applied.append(f"Load({base_file})")
        elif reset:
            self._call(ro.Reset)
            applied.append("Reset()")
        steps = [
            ("SetRecMode", rec_mode),
            ("SetBufferSize", buffer_size_bytes),
            ("SetBufferSizeEx", buffer_size_mb),
            ("SetPostTriggerPercentage", post_trigger_percent),
            ("SetTriggerBeep", None if trigger_beep is None else int(trigger_beep)),
            ("SetDataTruncate", data_truncate_bytes),
            ("SetAutoMerge", None if auto_merge is None else int(auto_merge)),
            ("SetSaveExternalSignals", None if save_external_signals is None else int(save_external_signals)),
            ("SetTraceFileName", trace_file_name),
            ("SetFilterPolarity", None if filter_out is None else int(filter_out)),
            ("SetEnableBypassLRU", None if bypass_lru is None else int(bypass_lru)),
        ]
        for name, value in steps:
            if value is not None:
                self._call(getattr(ro, name), value)
                applied.append(f"{name}({value!r})")
        for k, v in (settings or {}).items():
            results[f"setting_{k}"] = self._call(ro.ChangeUsbRecordingOptionSetting, int(k), int(v))
            applied.append(f"ChangeUsbRecordingOptionSetting({k}, {v})")
        for k, v in (string_settings or {}).items():
            results[f"string_setting_{k}"] = self._call(ro.ChangeUsbRecordingOptionStringSettings, int(k), str(v))
            applied.append(f"ChangeUsbRecordingOptionStringSettings({k}, {v!r})")
        out = self.out_path(output_file, "recording_options", ".rec")
        self._call(ro.Save, str(out))
        return {"rec_options_file": str(out), "applied": applied, "setting_results": results}

    # ------------------------------------------------------------ generation / exerciser

    def gen(self, method: str, *args) -> Any:
        return self._call(getattr(self.com, method), *args)

    def wait_gen(self, method: str, timeout_ms: int) -> dict:
        res = self._call(getattr(self.com, method), timeout_ms)
        if len(res) == 3:  # spent, tag, done
            spent, tag, done = res
            return {"spent_ms": int(spent or 0), "tag": jsonable(tag), "done": jsonable(done)}
        spent, done = res
        return {"spent_ms": int(spent or 0), "done": jsonable(done)}

    # ------------------------------------------------------------ misc analyzer

    def merge_trace_files(self, f1: str, f2: str, out: str) -> str:
        outp = self.out_path(out, "merged", ".usb")
        self._call(self.com.MergeTraceFiles, self._existing(f1), self._existing(f2), str(outp))
        return str(outp)

    def import_simpass(self, path: str) -> None:
        self._call(self.com.ImportSimPass, self._existing(path, "SimPass file"))

    # ------------------------------------------------------------ traces

    def _register_trace(self, obj: Any, path: str, origin: str) -> int:
        with self._lock:
            for e in self._traces.values():
                if e.obj is obj:
                    return e.handle
            h = self._next_handle
            self._next_handle += 1
            self._traces[h] = TraceEntry(h, obj, path, origin)
            try:
                obj.SetTag(h)
            except Exception:
                pass
            return h

    def _register_dispatch(self, t: Any, origin: str) -> TraceEntry:
        if t is None:
            raise AnalyzerError("Suite returned no trace object")
        try:
            path = t.GetFullPathAndName()
        except Exception:
            path = ""
        h = self._register_trace(t, path, origin)
        return self._traces[h]

    def open_trace(self, path: str) -> TraceEntry:
        full = self._existing(path, "trace file")
        for e in self._traces.values():
            if e.path and os.path.normcase(e.path) == os.path.normcase(full):
                return e
        t = self._call(self.com.OpenFile, full)
        return self._register_dispatch(t, "open_file")

    def trace(self, handle: int) -> Any:
        e = self._traces.get(int(handle))
        if e is None:
            raise AnalyzerError(f"Unknown trace handle {handle}. Use list_traces / open_trace.")
        return e.obj

    def trace_entry(self, handle: int) -> TraceEntry:
        self.trace(handle)
        return self._traces[int(handle)]

    def list_traces(self) -> list[dict]:
        pump_for(0.05)
        return [
            {"trace": e.handle, "path": e.path, "origin": e.origin, "opened_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.opened_at))}
            for e in self._traces.values()
        ]

    def close_trace(self, handle: int) -> None:
        e = self._traces.pop(int(handle), None)
        if e is None:
            raise AnalyzerError(f"Unknown trace handle {handle}")
        try:
            e.obj.Close()
        except pywintypes.com_error:
            pass

    def trace_summary(self, handle: int) -> dict:
        t = self.trace(handle)
        e = self._traces[int(handle)]
        res: dict[str, Any] = {"trace": e.handle, "origin": e.origin}

        def grab(key, fn):
            try:
                res[key] = fn()
            except pywintypes.com_error as exc:
                res[key] = {"error": com_error_text(exc)}

        grab("name", t.GetName)
        grab("file_name", t.GetFullName)
        grab("path", t.GetFullPathAndName)
        grab("packet_count", lambda: int(t.GetPacketsCountEx()))
        grab("trigger_packet", lambda: int(t.GetTriggerPacketNumEx()))
        grab("tag", lambda: int(t.GetTag()))
        decode = {}
        for name in ("packets", "transactions", "split_transactions", "transfers", "scsi_operations"):
            try:
                decode[name] = bool(t.IsLevelDecodeFinished(C.LEVELS[name]))
            except pywintypes.com_error as exc:
                decode[name] = com_error_text(exc)
        try:
            decode["all"] = bool(t.IsLevelDecodeFinished(-1))
        except pywintypes.com_error as exc:
            decode["all"] = com_error_text(exc)
        res["decode_finished"] = decode
        if isinstance(res.get("path"), str) and res["path"]:
            e.path = res["path"]
        return res

    def wait_decode(self, handle: int, level: int, timeout_s: float) -> dict:
        t = self.trace(handle)
        start = time.monotonic()
        while True:
            done = bool(self._call(t.IsLevelDecodeFinished, level))
            if done or time.monotonic() - start >= timeout_s:
                return {"finished": done, "waited_s": round(time.monotonic() - start, 2)}
            pump_for(0.25)

    # packets -------------------------------------------------------------

    def describe_packet(self, handle: int, number: int, fields: bool, raw: bool, usb3_symbols: bool = False) -> dict:
        t = self.trace(handle)
        p = self._call(t.GetUsbPacketEx, int(number))
        d: dict[str, Any] = {"packet": int(number)}
        try:
            ptype, is_usb3, is_event = p.GetType()
            ptype = int(ptype)
            is_usb3 = bool(is_usb3)
            is_event = bool(is_event)
            table = C.USB3_PACKET_TYPES if is_usb3 else (C.USB2_EVENT_TYPES if is_event else C.USB2_PACKET_TYPES)
            d.update(type=table.get(ptype, f"type {ptype}"), type_code=ptype, usb3=is_usb3, bus_event=is_event)
        except pywintypes.com_error as exc:
            d["type_error"] = com_error_text(exc)
        for key, fn, conv in (
            ("timestamp_ns", p.GetTimestamp, float),
            ("duration_ns", p.GetDuration, float),
        ):
            try:
                d[key] = conv(fn())
            except pywintypes.com_error:
                pass
        try:
            sp = int(p.GetSpeed())
            d["speed"] = C.PACKET_SPEEDS.get(sp, str(sp))
        except pywintypes.com_error:
            pass
        try:
            ch = int(p.GetChannel())
            d["channel"] = C.PACKET_CHANNELS.get(ch, str(ch))
        except pywintypes.com_error:
            pass
        try:
            bm = int(p.GetErrorsBitmap() or 0)
            if bm:
                d["errors_bitmap"] = f"0x{bm:X}"
                d["errors"] = [name for bit, name in C.PACKET_ERROR_BITS.items() if bm >> bit & 1]
        except (pywintypes.com_error, TypeError, ValueError):
            pass
        try:
            m = p.GetMarker()
            if m:
                d["marker"] = m
        except pywintypes.com_error:
            pass
        if fields:
            try:
                count, names, values, sizes = p.GetAllFieldsValues()
                fl = []
                for i in range(int(count or 0)):
                    fl.append({"name": names[i], "value": field_value_str(values[i], sizes[i]), "bits": int(sizes[i])})
                d["fields"] = fl
            except pywintypes.com_error as exc:
                d["fields_error"] = com_error_text(exc)
        if raw:
            try:
                n, data = p.GetRawData()
                d["raw_len"] = int(n or 0)
                d["raw_hex"] = bytes_hex(data) if n else ""
            except pywintypes.com_error as exc:
                d["raw_error"] = com_error_text(exc)
        if usb3_symbols:
            try:
                n, data = p.GetUsb3ScrambledData()
                d["usb3_scrambled_hex"] = bytes_hex(data) if n else ""
            except pywintypes.com_error as exc:
                d["usb3_scrambled_error"] = com_error_text(exc)
            try:
                n, syms = p.GetUsb3TenBitData()
                out = []
                for s in (syms or ())[: int(n or 0)]:
                    s = int(s)
                    out.append({"symbol": f"0x{s & 0x3FF:03X}", "rd_error": bool(s & 0x10000), "invalid": bool(s & 0x20000), "rd": int(bool(s & 0x40000))})
                d["usb3_10bit_symbols"] = out
            except pywintypes.com_error as exc:
                d["usb3_10bit_error"] = com_error_text(exc)
        return d

    def add_csv_speed(self, handle: int, packets: list[dict]) -> None:
        """Overwrite the (unreliable) GetSpeed value with the suite's CSV 'Sp' decode."""
        nums = [p["packet"] for p in packets]
        try:
            header, rows = self._packet_csv(handle, min(nums), max(nums))
        except AnalyzerError:
            return
        if "Packet" not in header or "Sp" not in header:
            return
        pi, si = header.index("Packet"), header.index("Sp")
        sp = {int(r[pi]): r[si] for r in rows if len(r) > si and r[pi].strip().isdigit() and r[si]}
        for p in packets:
            code = sp.get(p["packet"])
            if code:
                p["speed"] = C.CSV_SPEEDS.get(code, code)

    def packet_field(self, handle: int, number: int, field_name: str) -> dict:
        p = self._call(self.trace(handle).GetUsbPacketEx, int(number))
        bits, value = self._call(p.GetFieldValue, field_name)
        if bits is None and value is None:
            _, names, _, _ = self._call(p.GetAllFieldsValues)
            raise AnalyzerError(f"Packet {number} has no field {field_name!r}. Available fields: {list(names or ())}")
        return {"packet": number, "field": field_name, "value": field_value_str(value, bits), "bits": jsonable(bits)}

    def raw_packet_bits(self, handle: int, number: int) -> dict:
        bits, data = self._call(self.trace(handle).GetPacketEx, int(number), None)
        return {"packet": number, "number_of_bits": int(bits), "bytes_hex": bytes_hex(data)}

    # Packet-level CSV export is ~40x faster than walking packets over COM, so
    # searching and error listing are built on it.

    def _packet_csv(self, handle: int, start: int, end: int) -> tuple[list[str], list[list[str]]]:
        t = self.trace(handle)
        tmp = self.out_path(None, f"_scan_trace{handle}_{start}_{end}_{os.getpid()}", ".csv")
        try:
            self._call(t.ExportToCsv, str(tmp), 0, start, end)
            text, _ = read_text_file(tmp, None)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return [], []
        return rows[0], rows[1:]

    @staticmethod
    def _row_dict(header: list[str], row: list[str]) -> dict:
        d = {}
        for i, v in enumerate(row):
            if v != "" and i < len(header):
                d[header[i] or f"col{i}"] = v
        return d

    @staticmethod
    def _row_has_error(header: list[str], row: list[str]) -> bool:
        for i, v in enumerate(row):
            name = header[i].lower() if i < len(header) else ""
            if "error" in v.lower() and "propagated" not in name:
                return True
            if v.endswith("-BAD"):
                return True
        return False

    def find_packets(
        self,
        handle: int,
        start: int,
        end: int | None,
        types: list[str] | None,
        errors_only: bool,
        column_equals: dict[str, str] | None,
        text_contains: str | None,
        max_results: int,
        max_scan: int,
    ) -> dict:
        t = self.trace(handle)
        total = int(self._call(t.GetPacketsCountEx))
        last = total - 1 if end is None else min(total - 1, end)
        scan_end = min(last, start + max_scan - 1)
        header, rows = self._packet_csv(handle, start, scan_end)
        type_cols = [i for i, h in enumerate(header) if "type" in h.lower() or h.lower() in ("pid", "packet type")]
        want = {x.lower() for x in types} if types else None
        col_idx = {h.lower(): i for i, h in enumerate(header)}
        filters = []
        for k, v in (column_equals or {}).items():
            if k.lower() not in col_idx:
                raise AnalyzerError(f"Unknown column {k!r}; CSV columns: {header}")
            filters.append((col_idx[k.lower()], v.strip().lower()))

        def eq(a: str, b: str) -> bool:
            a = a.strip().lower()
            if a == b:
                return True
            try:
                return int(a, 0) == int(b, 0)
            except ValueError:
                return False

        found = []
        for row in rows:
            if want is not None and not any(i < len(row) and row[i].strip().lower() in want for i in type_cols):
                continue
            if errors_only and not self._row_has_error(header, row):
                continue
            if filters and not all(i < len(row) and eq(row[i], v) for i, v in filters):
                continue
            if text_contains and text_contains.lower() not in ",".join(row).lower():
                continue
            found.append(self._row_dict(header, row))
            if len(found) >= max_results:
                break
        return {
            "matches": found,
            "scanned_range": [start, scan_end],
            "next_start": scan_end + 1 if scan_end < last else None,
            "packet_count": total,
            "columns": header,
            "note": "Rows come from the packet-level CSV view; grouped rows (e.g. '3 Packets(0-2)' SOF runs) have an empty Packet number. Use trace_get_packet_detail for full decode.",
        }

    def error_summary_counts(self, handle: int) -> list[dict]:
        out = self.out_path(None, f"_errsum_trace{handle}_{os.getpid()}", ".txt")
        try:
            self._call(self.trace(handle).ReportErrorSummary, str(out))
            text, _ = read_text_file(out, None)
        finally:
            try:
                out.unlink()
            except OSError:
                pass
        import re

        res = []
        pat = re.compile(r"^(?P<cat>\S.*?)(?: on channel (?P<ch>.+?))? \((?P<n>\d+)\):\s*$")
        for line in text.splitlines():
            m = pat.match(line.strip())
            if m and m.group("n") != "0":
                res.append({"category": m.group("cat"), "channel": m.group("ch"), "count": int(m.group("n"))})
        return res

    def trace_errors(self, handle: int, start: int, end: int, max_packets: int, legacy_mask: int) -> dict:
        res: dict[str, Any] = {"summary": self.error_summary_counts(handle)}
        header, rows = self._packet_csv(handle, start, end)
        pcol = header.index("Packet") if "Packet" in header else 0
        err_packets = []
        for row in rows:
            if self._row_has_error(header, row) and pcol < len(row) and row[pcol].strip().isdigit():
                err_packets.append(int(row[pcol]))
        res["error_packet_count"] = len(err_packets)
        detailed = []
        t = self.trace(handle)
        for n in err_packets[:max_packets]:
            entry: dict[str, Any] = {"packet": n}
            try:
                p = t.GetUsbPacketEx(n)
                bm = int(p.GetErrorsBitmap() or 0)
                entry["errors"] = [name for bit, name in C.PACKET_ERROR_BITS.items() if bm >> bit & 1] or None
                ptype, is_usb3, is_event = p.GetType()
                table = C.USB3_PACKET_TYPES if is_usb3 else (C.USB2_EVENT_TYPES if is_event else C.USB2_PACKET_TYPES)
                entry["type"] = table.get(int(ptype), str(ptype))
                entry["timestamp_ns"] = float(p.GetTimestamp())
            except (pywintypes.com_error, TypeError, ValueError) as exc:
                entry["detail_error"] = com_error_text(exc)
            detailed.append(entry)
        res["error_packets"] = detailed
        res["truncated"] = len(err_packets) > max_packets
        if legacy_mask:
            legacy = {}
            for name, bit in C.TRACE_ERROR_TYPES.items():
                if not legacy_mask & bit:
                    continue
                try:
                    coll = t.AnalyzerErrors(bit)
                    if not hasattr(coll, "Count"):
                        coll = win32com.client.Dispatch(coll)
                    count = int(coll.Count)
                    legacy[name] = {"count": count, "packets": [int(coll.Item(i)) for i in range(min(count, max_packets))]}
                except pywintypes.com_error as exc:
                    legacy[name] = {"error": com_error_text(exc)}
            res["analyzer_errors_collection"] = legacy
        return res

    def power_info(self, handle: int, packet: int | None, time_ns: float | None, cc_line: int | None) -> dict:
        t = self.trace(handle)
        if cc_line is not None:
            self.require_restricted("CC-line power info (GetCCPowerInfoBy*)")
            if packet is not None:
                valid, v, c, p = self._call(t.GetCCPowerInfoByPacket, int(packet), int(cc_line))
            else:
                valid, v, c, p = self._call(t.GetCCPowerInfoByTime, float(time_ns), int(cc_line))
        elif packet is not None:
            valid, v, c, p = self._call(t.GetPowerInfoByPacket, int(packet))
        else:
            valid, v, c, p = self._call(t.GetPowerInfoByTime, float(time_ns))
        return {"valid": bool(valid), "voltage_uV": jsonable(v), "current_uA": jsonable(c), "power_uW": jsonable(p)}

    # exports / reports ------------------------------------------------------

    def report(self, handle: int, kind: str, output_file: str | None, max_chars: int) -> dict:
        t = self.trace(handle)
        method, ext = {
            "file_info": ("ReportFileInfo", ".html"),
            "error_summary": ("ReportErrorSummary", ".txt"),
            "traffic_summary": ("ReportTrafficSummary", ".txt"),
        }[kind]
        out = self.out_path(output_file, f"trace{handle}_{kind}", ext)
        self._call(getattr(t, method), str(out))
        return self._file_result(out, max_chars)

    def _file_result(self, out: Path, max_chars: int) -> dict:
        if not out.exists():
            return {"output_file": str(out), "exists": False}
        text, truncated = read_text_file(out, max_chars)
        return {"output_file": str(out), "size_bytes": out.stat().st_size, "truncated": truncated, "content": text}

    def export_text(self, handle: int, start: int, end: int, output_file: str | None, max_chars: int) -> dict:
        out = self.out_path(output_file, f"trace{handle}_export", ".txt")
        self._call(self.trace(handle).ExportToText, str(out), start, end)
        return self._file_result(out, max_chars)

    def _csv_result(self, out: Path, max_rows: int) -> dict:
        if not out.exists():
            return {"output_file": str(out), "exists": False}
        text, _ = read_text_file(out, None)
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        header = rows[0] if rows else []
        body = rows[1:]
        return {
            "output_file": str(out),
            "total_rows": len(body),
            "header": header,
            "rows": body[:max_rows],
            "truncated": len(body) > max_rows,
        }

    def export_csv(self, handle: int, level: int, start: int, end: int, output_file: str | None, max_rows: int) -> dict:
        out = self.out_path(output_file, f"trace{handle}_level{level}", ".csv")
        self._call(self.trace(handle).ExportToCsv, str(out), level, start, end)
        return self._csv_result(out, max_rows)

    def export_transactions_with_packets_csv(self, handle: int, start: int, end: int, output_file: str | None, max_rows: int) -> dict:
        out = self.out_path(output_file, f"trace{handle}_transactions_packets", ".csv")
        self._call(self.trace(handle).ExportTransactionsWithPacketsToCsv, str(out), start, end)
        return self._csv_result(out, max_rows)

    def export_transfer_data(self, handle: int, address: int, endpoint: int, direction: int, level_from: int, unit_from: int,
                             level_to: int, unit_to: int, output_file: str | None, binary: bool, size_kb: int, max_chars: int) -> dict:
        out = self.out_path(output_file, f"trace{handle}_ep{address}_{endpoint}", ".bin" if binary else ".txt")
        self._call(self.trace(handle).ExportUSBTransferData, address, endpoint, direction, level_from, unit_from,
                   level_to, unit_to, str(out), 1 if binary else 0, size_kb)
        if binary:
            if not out.exists():
                return {"output_file": str(out), "exists": False}
            data = out.read_bytes()
            return {"output_file": str(out), "size_bytes": len(data), "head_hex": data[: max(0, max_chars // 3)].hex(" ")}
        return self._file_result(out, max_chars)

    def export_visible_transactions_json(self, handle: int, output_file: str | None, max_chars: int) -> dict:
        out = self.out_path(output_file, f"trace{handle}_visible_tra", ".json")
        self._call(self.trace(handle).ExportVisibleTraList, str(out))
        return self._file_result(out, max_chars)

    def export_usb4_tunneled(self, handle: int, protocol: int, output_file: str) -> str:
        out = self.out_path(output_file, f"trace{handle}_tunneled", "")
        self._call(self.trace(handle).ExportUSB4TunneledProtocolTraffic, str(out), protocol)
        return str(out)

    def save_trace(self, handle: int, output_file: str, start: int, end: int, exclude_hidden: bool, move: bool) -> str:
        t = self.trace(handle)
        out = self.out_path(output_file, f"trace{handle}", ".usb")
        if move:
            self._call(t.SaveAs, str(out))
            self._traces[int(handle)].path = str(out)
        else:
            self._call(t.SaveTraceAs, str(out), start, end, 1 if exclude_hidden else 0)
        return str(out)

    # verification scripts ---------------------------------------------------

    def run_verification_script(self, handle: int, script: str, set_vars: dict[str, Any] | None, get_vars: list[str] | None) -> dict:
        t = self.trace(handle)
        if not set_vars and not get_vars:
            r = int(self._call(t.RunVerificationScript, script))
            return {"result_code": r, "result": C.VS_RESULTS.get(r, "UNKNOWN")}
        eng = self._call(t.GetVScriptEngine, script)
        if not hasattr(eng, "RunVScript"):
            eng = win32com.client.Dispatch(eng)
        for k, v in (set_vars or {}).items():
            self._call(eng.SetScriptVar, k, v)
        r = int(self._call(eng.RunVScript))
        out = {"result_code": r, "result": C.VS_RESULTS.get(r, "UNKNOWN")}
        if get_vars:
            out["vars"] = {k: jsonable(self._call(eng.GetScriptVar, k)) for k in get_vars}
        return out

    # ------------------------------------------------------------ generic escape hatch

    def resolve_target(self, target: str) -> Any:
        target = target.strip().lower()
        if target == "analyzer":
            return self.com
        if target in ("recording_options", "rec_options"):
            return self.rec_options
        if target.startswith("trace:"):
            return self.trace(int(target.split(":", 1)[1]))
        if target.startswith("packet:"):
            _, h, n = target.split(":")
            return self._call(self.trace(int(h)).GetUsbPacketEx, int(n))
        raise AnalyzerError("target must be 'analyzer', 'recording_options', 'trace:<handle>' or 'packet:<handle>:<number>'")

    @staticmethod
    def target_kind(target: str) -> str:
        t = target.strip().lower()
        if t in ("rec_options", "recording_options"):
            return "recording_options"
        return t.split(":", 1)[0]

    def check_member_allowed(self, target: str, member: str) -> None:
        if C.restricted_enabled():
            return
        allowed = C.DOCUMENTED_MEMBERS.get(self.target_kind(target), set())
        if member not in allowed:
            raise AnalyzerError(
                f"{member!r} is not part of the documented Automation API for {self.target_kind(target)!r}. "
                f"Undocumented/restricted members are disabled; the operator can enable them on their own "
                f"licensed installation with {C.RESTRICTED_ENV}=1."
            )

    def invoke(self, target: str, member: str, args: list[Any], kind: str) -> Any:
        self.check_member_allowed(target, member)
        obj = self.resolve_target(target)
        try:
            if kind == "get":
                val = getattr(obj, member)
            elif kind == "put":
                setattr(obj, member, args[0])
                val = None
            else:
                val = getattr(obj, member)(*args)
        except pywintypes.com_error as exc:
            raise AnalyzerError(com_error_text(exc)) from exc
        if val is not None and (hasattr(val, "_oleobj_") or type(val).__name__ == "PyIDispatch"):
            if hasattr(val, "GetPacketsCountEx") or hasattr(val, "GetFullPathAndName"):
                e = self._register_dispatch(val, f"invoke:{member}")
                return {"trace": e.handle, "path": e.path}
            return f"<COM object {type(val).__name__}>"
        return jsonable(val)

    def describe_members(self, target: str) -> list[str]:
        obj = self.resolve_target(target)
        names = []
        cls = type(obj)
        for n in dir(cls):
            if n.startswith("_") or n in ("CLSID", "coclass_clsid"):
                continue
            names.append(n)
        props = list(getattr(obj, "_prop_map_get_", {}).keys())
        members = set(names) | set(props)
        if not C.restricted_enabled():
            members &= C.DOCUMENTED_MEMBERS.get(self.target_kind(target), set())
        return sorted(members)
