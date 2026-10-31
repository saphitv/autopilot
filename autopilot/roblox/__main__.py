"""python -m autopilot.roblox: connect a Roblox Studio (through its MCP server) to the Autopilot brain.

    # a brain waiting for it (another terminal):  autopilot serve   or   autopilot play "<goal>"
    python -m autopilot.roblox --studio-mcp "ssh mini /Applications/RobloxStudio.app/Contents/MacOS/StudioMCP" \\
        --holds mini:~/dev/roblox/dont-tip
"""

import argparse
import json
import os
import socket

from ..bridge import brain_file
from .plugin import RobloxPlugin
from .studio_mcp import default_command


def brain_url():
    """The running `autopilot serve`'s address (its brain.json), else the default port."""
    try:
        with open(brain_file(), encoding="utf-8") as f:
            info = json.load(f)
        url, pid = info.get("url") or "", int(info.get("pid") or 0)
        if url.startswith(("ws://127.0.0.1:", "ws://localhost:")) and pid:
            os.kill(pid, 0)
            return url
    except (OSError, ValueError):
        pass
    return "ws://127.0.0.1:8765/v1/bridge"


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m autopilot.roblox", description=__doc__.split("\n")[0])
    p.add_argument("--url", help="the brain's WebSocket (default: the running `autopilot serve`, else port 8765)")
    p.add_argument("--studio-mcp", help="the StudioMCP command line (default: AUTOPILOT_STUDIO_MCP, else this "
                                        "machine's Studio)")
    p.add_argument("--place-id", help="the place to play when several Studios are open (AUTOPILOT_PLACE_ID)")
    p.add_argument("--project", help="the game's name in Autopilot's memory (default: the place's name)")
    p.add_argument("--holds", help="the repo whose holds.json records coordinate Studio (host:path over ssh, or a "
                                   "local path); none: no coordination")
    p.add_argument("--holder", default=f"{socket.gethostname()}-autopilot",
                   help="this bridge's name in the holds records")
    p.add_argument("--no-media", action="store_true", help="no recording or stills (screen captures)")
    p.add_argument("--once", action="store_true", help="exit when the brain disconnects (for `autopilot play`)")
    a = p.parse_args(argv)
    cmd = a.studio_mcp.split() if a.studio_mcp else default_command()
    plugin = RobloxPlugin(a.url or brain_url(), cmd, place_id=a.place_id or os.environ.get("AUTOPILOT_PLACE_ID"),
                          project=a.project, holds=a.holds, media=not a.no_media, holder=a.holder)
    try:
        plugin.run(once=a.once)
    except KeyboardInterrupt:
        plugin.shutdown()


if __name__ == "__main__":
    main()
