"""Studio coordination for a place several agents (and people) share: the `holds.json` records of a repo that keeps
them (Don't Tip!: scripts/studio-holds.py, .agents/skills/roblox-studio-workflow/references/coordination.md).

Before it starts a play session the bridge checks that nobody else holds `play-stop` or `scene` on that Studio, then
writes its own record claiming `play-stop`; after the session it marks the record released. The repo can be on another
machine: "mini:~/dev/roblox/dont-tip" runs the commands there over ssh.
"""

import json
import os
import shlex
import subprocess
import time

SHARED = {"play-stop", "scene"}


class HoldsError(Exception):
    pass


class Holds:
    def __init__(self, spec, holder, place_id="", studio_id=""):
        host, sep, repo = spec.partition(":")
        if not sep or "/" in host:  # a local path (no "host:")
            host, repo = None, spec
        self.host, self.repo = host, repo
        self.holder = holder
        self.place_id, self.studio_id = str(place_id or ""), str(studio_id or "")
        self.record = f"build/agent-handoffs/autopilot-{holder}/holds.json"

    def _run(self, script, stdin=None):
        if self.host:
            cmd = ["ssh", self.host, f"cd {self.repo} && {script}"]
        else:
            cmd = ["bash", "-c", f"cd {shlex.quote(os.path.expanduser(self.repo))} && {script}"]
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            raise HoldsError(f"holds: {script.split()[0]} failed: {p.stderr.strip()[:300]}")
        return p.stdout

    def claims(self):
        out = self._run("python3 scripts/studio-holds.py --repo .")
        return json.loads(out).get("claims") or []

    def blocking(self):
        """Other holders' live claims on play-stop or scene of this place."""
        out = []
        for c in self.claims():
            if c.get("holderThreadId") == self.holder or c.get("phase") == "released":
                continue
            if self.place_id and str(c.get("placeId") or "") not in ("", self.place_id):
                continue
            if SHARED & set(c.get("resources") or []):
                out.append(c)
        return out

    def _write(self, record):
        data = json.dumps(record, indent=2)
        self._run(f"mkdir -p {shlex.quote(os.path.dirname(self.record))} && cat > {shlex.quote(self.record)}",
                  stdin=data)

    def claim(self, reason):
        blocking = self.blocking()
        if blocking:
            who = "; ".join(f"{c.get('holderThreadId')} ({c.get('phase')}: {c.get('reason', '')[:80]})"
                            for c in blocking)
            raise HoldsError(f"Studio is held by another task: {who}. Ask it to release play-stop first.")
        self._write({"holderThreadId": self.holder, "placeId": self.place_id, "studioId": self.studio_id,
                     "resources": ["play-stop"], "phase": "testing", "reason": reason,
                     "testDependencyPaths": [], "claimedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def release(self, note="Play stopped, Studio back in Edit."):
        self._write({"holderThreadId": self.holder, "placeId": self.place_id, "studioId": self.studio_id,
                     "resources": [], "phase": "released", "reason": note, "testDependencyPaths": [],
                     "releasedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
