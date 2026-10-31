"""A client of Roblox Studio's built-in MCP server (StudioMCP, JSON-RPC 2.0 over stdio, one message per line).

The server is a proxy to a running Studio: every tool but `list_roblox_studios` takes the `studio_id` of the Studio it
acts on. It can run on this machine or another one (a command such as `ssh mini /Applications/RobloxStudio.app/
Contents/MacOS/StudioMCP`): it only needs stdin/stdout.

Calls are thread-safe: each waits for its own answer, so several can be in flight at once.
"""

import itertools
import json
import os
import platform
import shlex
import subprocess
import sys
import threading
import time

MAC_STUDIO_MCP = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
MCP_PROTOCOL = "2025-06-18"


def default_command():
    """The StudioMCP command for this machine (AUTOPILOT_STUDIO_MCP overrides it, as a shell-quoted command line)."""
    cmd = os.environ.get("AUTOPILOT_STUDIO_MCP")
    if cmd:
        return shlex.split(cmd)
    if platform.system() == "Darwin":
        return [MAC_STUDIO_MCP]
    if platform.system() == "Windows":
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox", "Versions")
        try:
            for v in sorted(os.listdir(base), key=lambda d: os.path.getmtime(os.path.join(base, d)), reverse=True):
                exe = os.path.join(base, v, "StudioMCP.exe")
                if os.path.exists(exe):
                    return [exe]
        except OSError:
            pass
    raise StudioError("no StudioMCP here: set AUTOPILOT_STUDIO_MCP (e.g. \"ssh mini " + MAC_STUDIO_MCP + "\")")


class StudioError(Exception):
    """A tool answered with isError, or the server failed."""


class StudioMCP:
    def __init__(self, command=None, timeout=60.0, log=None):
        self.command = list(command or default_command())
        self.timeout = timeout
        self.log = log  # callable(str) for a line per call, or None
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self.proc = None
        self.closed = None
        self.server_info = None
        self.studio_id = None

    # -- the process -----------------------------------------------------------------------------------

    def start(self):
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._read, daemon=True, name="studio-mcp-read").start()
        threading.Thread(target=self._drain_stderr, daemon=True, name="studio-mcp-err").start()
        init = self._rpc("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                        "clientInfo": {"name": "autopilot-roblox", "version": "0.1"}})
        self.server_info = init.get("serverInfo")
        self._notify("notifications/initialized")
        return self

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._fail("closed")

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def _send(self, msg):
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        with self._write_lock:
            if self.closed:
                raise StudioError(f"StudioMCP is gone ({self.closed})")
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def _rpc(self, method, params=None, timeout=None):
        rid = next(self._ids)
        ev = threading.Event()
        slot = [ev, None]
        with self._lock:
            self._pending[rid] = slot
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            if not ev.wait(timeout or self.timeout):
                raise StudioError(f"{method}: no answer in {timeout or self.timeout:.0f}s")
        finally:
            with self._lock:
                self._pending.pop(rid, None)
        msg = slot[1]
        if msg is None:
            raise StudioError(f"StudioMCP is gone ({self.closed})")
        if "error" in msg:
            raise StudioError(f"{method}: {msg['error'].get('message')}")
        return msg.get("result") or {}

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            rid = msg.get("id")
            if rid is None or "method" in msg:  # a notification or a server request: nothing we answer
                continue
            with self._lock:
                slot = self._pending.get(rid)
            if slot:
                slot[1] = msg
                slot[0].set()
        self._fail(f"exited with {self.proc.poll()}")

    def _drain_stderr(self):
        for line in self.proc.stderr:
            if self.log:
                self.log("studio-mcp: " + line.decode(errors="replace").rstrip())

    def _fail(self, why):
        self.closed = self.closed or why
        with self._lock:
            for slot in self._pending.values():
                slot[0].set()

    # -- tools -----------------------------------------------------------------------------------------

    def call_raw(self, tool, args=None, timeout=None):
        """The tool's result: {"content": [...], "isError": bool}."""
        t = time.monotonic()
        res = self._rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout)
        if self.log:
            self.log(f"{tool} {1000 * (time.monotonic() - t):.0f} ms")
        return res

    def call(self, tool, args=None, timeout=None, studio=True):
        """A tool's text (its text contents joined), StudioError when it answers isError. studio: add studio_id."""
        args = dict(args or {})
        if studio:
            args.setdefault("studio_id", self.studio())
        res = self.call_raw(tool, args, timeout)
        text = "\n".join(c.get("text", "") for c in res.get("content") or [] if c.get("type") == "text")
        if res.get("isError"):
            raise StudioError(f"{tool}: {text}")
        return text

    def studios(self):
        return json.loads(self.call("list_roblox_studios", studio=False) or "{}").get("studios") or []

    def studio(self, place_id=None):
        """The studio_id to act on: AUTOPILOT_STUDIO_ID, else the one open on place_id (AUTOPILOT_PLACE_ID), else the
        only one open."""
        if self.studio_id:
            return self.studio_id
        want = os.environ.get("AUTOPILOT_STUDIO_ID")
        place = str(place_id or os.environ.get("AUTOPILOT_PLACE_ID") or "")
        # A fresh proxy finds the running Studios over a few seconds (first with no name): wait for named ones
        deadline = time.monotonic() + 15
        while True:
            found = [s for s in self.studios() if s.get("name")]
            if found or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        if want:
            self.studio_id = want
        elif place:
            match = [s for s in found if f"placeId: {place})" in s.get("name", "")]
            if not match:
                raise StudioError(f"no Studio open on place {place} (open: {[s.get('name') for s in found]})")
            self.studio_id = match[0]["id"]
        elif len(found) == 1:
            self.studio_id = found[0]["id"]
        else:
            raise StudioError(f"{len(found)} Studios open: set AUTOPILOT_PLACE_ID or AUTOPILOT_STUDIO_ID")
        return self.studio_id

    def state(self):
        """{"mode": "Edit"|"Play"|..., "datamodels": [...], "focused": ...} from get_studio_state's text."""
        text = self.call("get_studio_state")
        out = {"raw": text}
        for line in text.splitlines():
            k, _, v = line.strip("- ").partition(":")
            v = v.strip()
            if k.startswith("Current Studio Mode"):
                out["mode"] = v
            elif k.startswith("Available DataModels"):
                out["datamodels"] = [d.strip() for d in v.split(",") if d.strip()]
            elif k.startswith("Focused DataModel"):
                out["focused"] = v
        return out

    def luau(self, code, datamodel="Client", timeout=None):
        """Run Luau in a datamodel and return what it returned, as text (execute_luau's output)."""
        return self.call("execute_luau", {"code": code, "datamodel_type": datamodel}, timeout)

    def luau_json(self, code, datamodel="Client", timeout=None):
        """Run Luau whose last statement returns HttpService:JSONEncode(...) and decode it. The JSON is found in the
        tool's text (which may wrap it), so the code must return exactly one JSON value."""
        text = self.luau(code, datamodel, timeout)
        return decode_json_text(text)

    def screenshot(self, capture_id="AutopilotView"):
        """The edit-time viewport as (mime type, base64 data), from screen_capture's image content."""
        res = self.call_raw("screen_capture", {"studio_id": self.studio(), "capture_id": capture_id})
        if res.get("isError"):
            raise StudioError("screen_capture: " + " ".join(c.get("text", "") for c in res.get("content") or []))
        for c in res.get("content") or []:
            if c.get("type") == "image":
                return c.get("mimeType", "image/png"), c.get("data", "")
        raise StudioError("screen_capture: no image in the answer")


def decode_json_text(text):
    """The JSON value in execute_luau's text output (it may come with a prefix such as a log or 'Result:')."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener in ("{", "["):
        i = text.find(opener)
        if i >= 0:
            closer = "}" if opener == "{" else "]"
            j = text.rfind(closer)
            if j > i:
                try:
                    return json.loads(text[i:j + 1])
                except ValueError:
                    continue
    raise StudioError(f"no JSON in execute_luau's output: {text[:300]!r}")


if __name__ == "__main__":  # python -m autopilot.roblox.studio_mcp: list the Studios and their state
    with StudioMCP(log=lambda s: print(s, file=sys.stderr)) as mcp:
        print(mcp.server_info)
        for s in mcp.studios():
            print(s)
        print(mcp.state())
