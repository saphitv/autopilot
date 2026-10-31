"""Research helpers for probes: the codex's data tables and a grep over it without a model turn per look, and
measurements on the live game (the actors nearest the player, a teleport that sets up a measurement, a fight trial
that says whether and how fast a target can be beaten, an enemy's profile measured without fighting it, the first
function name a game accepts).

The probe namespace (autopilot/agents/session.py, Session._namespace) adds NAMES (name -> callable; names_for(game)
binds the codex helpers' root to the game's codex) and DOC goes into the agent's instructions (KIT.md's style). A real
run spent 47 model turns on greps over 580 KB of CSV data tables and never tested the goal's last step: these do that
work in one probe, so the agent's turns go to checking what the game does.

The codex helpers read memory/<project>/codex/ (autopilot/codex.py): a root is a path, an autopilot.codex.Codex, or a
game (its .codex); ROOT is the default when a call gives none. The game helpers wrap GameError with what was tried.
"""

import csv
import functools
import math
import os
import re
import time

from .kit import control
from .kit.game import GameError
from .kit.geom import Vec, yaw_of

ROOT = None  # the codex folder the helpers use when a call names no root (session.py may set it, or use names_for)
SWING_EVERY = 0.6  # seconds between fight_trial's presses (a melee cooldown's order)
CLOSER_AFTER = 5  # swings without a hit after which fight_trial steps closer once (to 0.6 of the reach)
WALK_TIMEOUT = 10.0  # seconds fight_trial gives the walk from the teleport's landing to the target
SAFE_DISTANCE = 1200.0  # units enemy_profile watches an enemy from
HEALTH_NAMES = ("Health", "CurrentHealth", "HP", "health", "Hp", "hp", "CurrentHP", "HealthPoints", "current_health",
                "Hitpoints", "HitPoints")
MAX_HEALTH_NAMES = ("MaxHealth", "HealthMax", "max_health", "MaxHP", "HPMax", "MaxHitPoints", "maxHealth")
GREP_MAX_FILE = 8_000_000  # bytes: a bigger codex file is skipped by grep (the asset lists are the biggest)


# -- the codex ---------------------------------------------------------------------------------------------

def _root(root=None):
    """The codex folder: root as given (a path, a Codex, a game), else ROOT."""
    root = root if root is not None else ROOT
    if hasattr(root, "codex"):  # (a game: its codex, or none yet)
        root = root.codex
    root = getattr(root, "root", root)  # (a Codex)
    if not root or not isinstance(root, (str, bytes, os.PathLike)):
        raise ValueError("no codex folder: give root= (the codex's path, game.codex) or use names_for(game)")
    if not os.path.isdir(root):
        raise FileNotFoundError(f"no codex at {root}")
    return root


def _rel(path, root):
    return os.path.relpath(path, root).replace(os.sep, "/")


def tables(root=None):
    """The data tables in the codex: [{"name", "path", "rows", "struct"}] for each CSV (Unreal's data and curve tables
    export as data/<asset>.csv; `struct`: the row struct's path from the .rowstruct.txt beside it, or None)."""
    root = _root(root)
    out = []
    for folder, _, names in os.walk(root):
        for name in sorted(names):
            if not name.lower().endswith(".csv"):
                continue
            full = os.path.join(folder, name)
            struct = None
            try:
                with open(full[:-4] + ".rowstruct.txt", encoding="utf-8", errors="replace") as f:
                    m = re.search(r"rows are\s+(\S+)", f.read())
                    struct = m.group(1) if m else None
            except OSError:
                pass
            try:
                with open(full, encoding="utf-8-sig", errors="replace", newline="") as f:
                    n = max(0, sum(1 for _ in csv.reader(f)) - 1)
            except OSError:
                n = 0
            out.append({"name": name[:-4], "path": _rel(full, root), "rows": n, "struct": struct})
    return sorted(out, key=lambda t: t["name"].lower())


def _table_file(table, root):
    """The CSV file of a table named by its name (the file's stem, case-insensitive), its codex-relative path, or a
    path on disk."""
    if os.path.isfile(table):
        return table
    candidate = os.path.join(root, table)
    for p in (candidate, candidate + ".csv"):
        if os.path.isfile(p):
            return p
    want = os.path.basename(table).lower().removesuffix(".csv")
    found = [t for t in tables(root) if t["name"].lower() == want]
    if len(found) == 1:
        return os.path.join(root, found[0]["path"])
    if found:
        paths = ", ".join(t["path"] for t in found)
        raise ValueError(f"{len(found)} tables are named {table}: give the path ({paths})")
    known = ", ".join(t["name"] for t in tables(root)) or "none in the codex"
    raise FileNotFoundError(f"no data table {table} (tables: {known})")


def _value(text):
    """A CSV cell as a number or boolean when it is one, else the text (a struct `(...)` stays text: see cell)."""
    s = text.strip()
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", s):
        return float(s)
    if s in ("True", "true", "TRUE"):
        return True
    if s in ("False", "false", "FALSE"):
        return False
    return text


def _read_rows(path):
    """(the row-name column's key, the rows as dicts) of a table's CSV. Unreal's export heads the row-name column
    `---`: it is given as "Name" (or "RowName" when the struct has a Name field of its own)."""
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return "Name", []
        header = [h.strip() for h in header]
        key = header[0]
        if key in ("---", "", "Name", "RowName"):
            key = "Name" if "Name" not in header[1:] else "RowName"
            header[0] = key
        rows = []
        for line in reader:
            if not line or not any(c.strip() for c in line):
                continue
            row = {h: _value(c) for h, c in zip(header, line)}
            row[key] = str(line[0]).strip()
            rows.append(row)
    return key, rows


def _matches(where, name, row):
    if where is None:
        return True
    if isinstance(where, dict):
        return all(str(v).lower() in str(row.get(k, "")).lower() for k, v in where.items())
    where = str(where)
    try:
        return re.search(where, name, re.I) is not None
    except re.error:
        return where.lower() in name.lower()


def rows(table, where=None, root=None):
    """The rows of a data table as dicts (column -> value: numbers as numbers, a nested `(Key=Value,...)` struct as its
    text: `cell` pulls a field out of it). table: its name (DT_Enemies), its codex path or a file. where: a regex (or
    substring) the row's name must match, or {column: substring} every column must contain."""
    path = _table_file(table, _root(root))
    key, all_rows = _read_rows(path)
    return [r for r in all_rows if _matches(where, r[key], r)]


def row(table, name, root=None):
    """One row of a data table by its name (exact, then case-insensitive); says which names are close when none is."""
    path = _table_file(table, _root(root))
    key, all_rows = _read_rows(path)
    for r in all_rows:
        if r[key] == name:
            return r
    for r in all_rows:
        if r[key].lower() == str(name).lower():
            return r
    near = [r[key] for r in all_rows if str(name).lower() in r[key].lower()][:8]
    raise KeyError(f"no row {name} in {os.path.basename(path)}" + (f" (close: {', '.join(near)})" if near else
                                                                   f" ({len(all_rows)} rows)"))


def _parse_struct(text, i=0):
    """Unreal's text form of a value: `(A=1,B="s",C=(X=1,Y=2),D=((Item="x"),(Item="y")))` -> dicts, lists, numbers and
    strings. Returns (value, index after it)."""
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    if i >= n:
        return "", i
    if text[i] == "(":
        i += 1
        items, keyed = [], True
        while True:
            while i < n and text[i] in " \t\r\n,":
                i += 1
            if i >= n:
                break
            if text[i] == ")":
                i += 1
                break
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", text[i:])
            if m:
                value, i = _parse_struct(text, i + m.end())
                items.append((m.group(1), value))
            else:
                value, i = _parse_struct(text, i)
                items.append((None, value))
                keyed = False
        if keyed and items:
            return dict(items), i
        return [v for _, v in items], i
    if text[i] == '"':
        j, out = i + 1, []
        while j < n and text[j] != '"':
            if text[j] == "\\" and j + 1 < n:
                j += 1
            out.append(text[j])
            j += 1
        return "".join(out), j + 1
    # a bare value: up to the next top-level comma or closing bracket (a quoted or bracketed part is kept whole)
    j, depth = i, 0
    while j < n:
        c = text[j]
        if c == '"':
            j += 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        j += 1
    raw = text[i:j].strip()
    if raw == "None":
        return None, j
    return _value(raw), j


def cell(value, key=None):
    """A field out of a struct cell's text (`(Health=30,Drops=((Item="Sword",Chance=0.5)),Pos=(X=1,Y=2))`): `cell(v,
    "Pos.X")` -> 1, `cell(v, "Drops.0.Item")` -> "Sword" (dotted keys walk nested structs and lists); `cell(v)` is the
    whole value as dicts, lists, numbers and strings. A value that isn't a struct is given back as it is."""
    if not isinstance(value, str):
        parsed = value
    else:
        parsed, _ = _parse_struct(value)
    if key in (None, ""):
        return parsed
    here = parsed
    for part in str(key).split("."):
        if isinstance(here, dict):
            if part in here:
                here = here[part]
                continue
            match = [k for k in here if k.lower() == part.lower()]
            if not match:
                raise KeyError(f"no {part} in {list(here)[:12]}")
            here = here[match[0]]
        elif isinstance(here, list) and re.fullmatch(r"\d+", part) and int(part) < len(here):
            here = here[int(part)]
        else:
            raise KeyError(f"{key}: no {part} in {str(here)[:80]}")
    return here


def grep(pattern, where="", root=None, limit=50):
    """Lines matching a regex across the codex (or its subfolder `where`: "blueprints", "data", "source"), as
    "path:line: text" (paths from the codex's top, text cut to 200 characters), up to `limit`; the last line says when
    there were more. Case-sensitive unless the pattern says (?i)."""
    root = _root(root)
    base = os.path.join(root, where) if where else root
    if not os.path.isdir(base):
        if os.path.isfile(base):
            files = [base]
        else:
            raise FileNotFoundError(f"no folder {where} in the codex")
    else:
        files = [os.path.join(f, n) for f, _, names in os.walk(base) for n in sorted(names)]
    rx = re.compile(pattern)
    out, more = [], 0
    for path in sorted(files):
        try:
            if os.path.getsize(path) > GREP_MAX_FILE:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        if len(out) < limit:
                            out.append(f"{_rel(path, root)}:{i}: {line.rstrip()[:200]}")
                        else:
                            more += 1
        except OSError:
            continue
    if more:
        out.append(f"... {more} more matches: narrow the pattern or where=")
    return out


# -- the live game -----------------------------------------------------------------------------------------

def _game(e, what):
    return GameError(f"{what}: {e}")


def nearest(game, cls, n=5, within=None):
    """Actors of a class (or of a list of paths) sorted by distance from the player: [(path, Vec, distance)], the
    player's own pawn left out, the first n, those within `within` units when given."""
    try:
        actors = list(cls) if isinstance(cls, (list, tuple, set)) else game.actors(cls)
        me = game.me()
        locs = game.locs([a for a in actors if a != game.pawn])
    except GameError as e:
        raise _game(e, f"nearest({cls!r})") from None
    found = sorted(((a, v, me.dist(v)) for a, v in locs.items()), key=lambda t: t[2])
    if within is not None:
        found = [t for t in found if t[2] <= within]
    return [(a, v, round(d, 1)) for a, v, d in found[:n]]


def _landing(game, actor, near, distance):
    """Where to land `actor` next to the actor `near`: `distance` short of its box center, on the side facing the
    actor now, feet at the level of the target's box bottom; and the yaw that faces it."""
    center, ext = game.bounds(near)
    if center is None:
        center, ext = game.loc(near), Vec(0, 0, 0)
    try:
        at = game.loc(actor)
        _, mine = game.bounds(actor)
    except GameError:
        at, mine = center + Vec(1, 0, 0), Vec(0, 0, 0)
    side = (at - center).flat
    side = side.unit if side.length > 1e-3 else Vec(1, 0, 0)
    point = center + side * float(distance)
    point = Vec(point.x, point.y, center.z - (ext.z if ext else 0.0) + (mine.z if mine else 0.0) + 5.0)
    return point, yaw_of(center - point)


def teleport(game, to, actor=None, why="research"):
    """Move the player's pawn (or `actor`) to `to`: a Vec/point, an actor path (then 150 units short of it, on the side
    facing it, facing it) or a dict ({"near": path, "distance": 300}, or a point as {"X","Y","Z"}). A direct change to
    the game, not play: for setting up a measurement in a probe (reported with `why`, like game.set). Unreal:
    K2_TeleportTo; Unity: the Transform's position; Godot: global_position. Returns {"actor", "asked", "at", "landed":
    whether it is within 100 of where it was asked, "note"?}."""
    actor = actor or game.pawn
    if not actor:
        raise GameError("teleport: no player pawn now (dead? a menu?): give actor=")
    yaw = None
    try:
        if isinstance(to, dict) and ("near" in to or "actor" in to):
            point, yaw = _landing(game, actor, to.get("near") or to.get("actor"), to.get("distance", 150))
        elif isinstance(to, str):
            point, yaw = _landing(game, actor, to, 150)
        else:
            point = Vec.of(to)
        if point is None:
            raise GameError(f"teleport: no point in {to!r}")
        name = getattr(game.engine, "name", "")
        if name == "unreal":
            rot = game.engine.rotation_to_native(yaw if yaw is not None else 0.0)
            game.call(actor, "K2_TeleportTo", DestLocation=game.to_native(point), DestRotation=rot)
            if game.writes and game.writes[-1].get("call") == "K2_TeleportTo":
                game.writes[-1]["why"] = why
            if yaw is not None and actor == game.pawn and game.controller:
                try:  # (the view too: the pawn's rotation isn't the controller's)
                    game.call(game.controller, "SetControlRotation", NewRotation=rot)
                except GameError:
                    pass
        elif name == "unity":
            game.set(actor, "position", game.to_native(point), why=why)
        elif name == "godot":
            game.set(actor, "global_position", game.to_native(point), why=why)
        elif name == "roblox":
            game.set(actor, "Pivot", game.to_native(point), why=why)
        else:
            raise GameError(f"teleport: no way to move an actor in a {name or 'unknown'} game yet")
        time.sleep(0.1)
        at = game.loc(actor)
    except GameError as e:
        raise _game(e, f"teleport({to!r})") from None
    out = {"actor": actor, "asked": point.rounded(), "at": at.rounded(), "landed": at.dist(point) <= 100.0}
    if not out["landed"]:
        out["note"] = (f"the game put it {at.dist(point):.0f} from where it was asked (blocked by collision, or the "
                       "game's own movement component moved it back: try a point a little further out)")
    return out


def _health_prop(game, obj, prefer=None):
    """A health property's name on obj: `prefer`, else the first of HEALTH_NAMES that reads as a number, else a
    numeric property of game.describe whose name says health/hp; None when none."""
    names = [prefer] if prefer else list(HEALTH_NAMES)
    values = game.batch([lambda n=n: game.get(obj, n) for n in names])
    for n, v in zip(names, values):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return n
    if prefer:
        return None
    try:
        props = [p.get("Name") for p in (game.describe(obj) or {}).get("Properties", []) if isinstance(p, dict)]
    except GameError:
        return None
    likely = [p for p in props if p and re.search(r"health|\bhp\b|hitpoint", p, re.I) and not re.search(
        r"max|regen|percent|ratio|bar|widget|text", p, re.I)]
    values = game.batch([lambda n=n: game.get(obj, n) for n in likely])
    for n, v in zip(likely, values):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return n
    return None


def _number(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _actor(game, target):
    """An actor path from `target`: as given when it is in the level, else the nearest actor of that class name."""
    try:
        game.loc(target)  # (an actor in the level: a class name or path has no location)
        return target
    except GameError:
        near = nearest(game, target, n=1)
        if not near:
            raise GameError(f"no actor {target} in the level now") from None
        return near[0][0]


def _max_health(game, obj, prop=None):
    """The object's maximum health, best effort: the first of MAX_HEALTH_NAMES that reads as a number (and a
    `Max<prop>` for the health property's own name); None when none."""
    names = list(MAX_HEALTH_NAMES)
    if prop:
        names = [f"Max{prop}", f"{prop}Max"] + [n for n in names if n not in (f"Max{prop}", f"{prop}Max")]
    for n, v in zip(names, game.batch([lambda n=n: game.get(obj, n) for n in names])):
        if _number(v) is not None:
            return _number(v)
    return None


def _stance(game, pawn, target):
    """Where the player stands against the target: {"distance": across the floor between their box centers, "height":
    the target's box bottom above the player's feet (negative: below)}; None when a box can't be read."""
    mine, theirs = game.batch([lambda: game.bounds(pawn), lambda: game.bounds(target)])
    if isinstance(mine, GameError) or isinstance(theirs, GameError):
        return None
    (cp, ep), (ct, et) = mine, theirs
    if cp is None or ct is None:
        return None
    ep, et = ep or Vec(0, 0, 0), et or Vec(0, 0, 0)
    return {"distance": round(cp.dist2d(ct)), "height": round((ct.z - et.z) - (cp.z - ep.z))}


def _heal(game, pawn, prop, value, why):
    """Write the player's health back to `value` (reported, like every write): True when the read-back agrees, False
    when the game refused or the value didn't take (a health kept on a component, or clamped by the game)."""
    try:
        game.set(pawn, prop, value, why=why)
        back = _number(game.get(pawn, prop))
    except GameError:
        return False
    return back is not None and abs(back - value) <= 0.5


def _alive(game, pawn, health):
    """Whether the player is still there after a measurement: its health above 0 (when read) and its pawn in place."""
    if health is not None and health <= 0:
        return False
    try:
        return bool(game.context(fresh=True).get("character"))
    except GameError:
        return True


def _pause(game, seconds):
    """Wait `seconds` of game time, a tick at a time, asking the game's clock each tick: a probe holds no keys, so
    nothing else brings the clock in between (control.wait would wait 0.4 s before asking). Bounded in real time."""
    t0 = game.t
    end = time.monotonic() + 3 * seconds + 1
    while time.monotonic() < end:
        yield
        try:
            game.context(fresh=True)
        except GameError:
            pass
        if game.t - t0 >= seconds:
            break
    return control.Result(True, "waited")


def fight_trial(game, target, key="LeftMouseButton", seconds=20, reach=None, health=None, every=SWING_EVERY,
                heal=True):
    """A measurement, in a probe: get in reach of `target` (an actor path, or a class name: its nearest actor):
    teleported near it (a direct change, reported), then walked the last stretch so the player stands on the game's own
    ground; look at it and press `key` every `every` seconds for up to `seconds`, reading its health and the player's
    each swing. When the first swings land nothing it steps closer once (0.6 of the reach) and swings again. Returns
    {"target", "swings", "hits" (swings that lowered its health), "damage_per_hit", "target_health": [start, end],
    "player_health": [start, end], "player_health_max", "damage_taken", "seconds", "killed", "player_dead", "stance":
    {"distance" across the floor, "height": the target's box bottom above the player's feet} where the swings were made,
    "closer": the stance after stepping closer (None when it didn't), "healed", "verdict" (plain words: whether the
    fight can be won at this rate), "health_props", "aim", "walked"}. reach: the game's melee reach, how near to stand
    (120); health: the health property's name when the usual ones (Health, CurrentHealth, HP...) aren't the game's;
    heal: write the player's health back to what it was before the trial (reported), so the next measurement doesn't
    start hurt. A player the trial killed is not revived: the result says so (the level must be restarted)."""
    within = float(reach or 120)
    try:
        target = _actor(game, target)
        pawn = game.pawn
        if not pawn:
            raise GameError("no player pawn now")
        land = teleport(game, {"near": target, "distance": within + 200.0}, why=f"fight trial against {target}")
        t_prop, p_prop = _health_prop(game, target, health), _health_prop(game, pawn, health)
        p_max = _max_health(game, pawn, p_prop) if p_prop else None
        read = lambda: game.batch([lambda: game.get(target, t_prop) if t_prop else None,
                                   lambda: game.get(pawn, p_prop) if p_prop else None,
                                   lambda: game.exists(target)])
        t0_health, p0_health, _ = read()
        t_health, p_health = _number(t0_health), _number(p0_health)
        counts = {"swings": 0, "hits": 0, "dealt": 0.0, "killed": False, "aim": None, "last_t": t_health,
                  "last_p": p_health, "took": 0.0, "stance": None, "closer": None, "walked": None, "dead": False}

        def swing():
            """One press and its reading: a Result when the trial is over (target down, player dead), else None."""
            game.press(key)
            counts["swings"] += 1
            yield from _pause(game, every)
            t_now, p_now, alive = read()
            t_now, p_now = _number(t_now), _number(p_now)
            if counts["last_t"] is not None and t_now is not None and t_now < counts["last_t"]:
                counts["hits"] += 1
                counts["dealt"] += counts["last_t"] - t_now
            if counts["last_p"] is not None and p_now is not None and p_now < counts["last_p"]:
                counts["took"] += counts["last_p"] - p_now
            counts["last_t"], counts["last_p"] = (t_now if t_now is not None else counts["last_t"],
                                                  p_now if p_now is not None else counts["last_p"])
            if alive is not True or (t_now is not None and t_now <= 0):
                counts["killed"] = True
                return control.Result(True, "target down")
            if p_now is not None and p_now <= 0:
                counts["dead"] = True
                return control.Result(False, "the player died")
            return None

        def trial():
            walked = yield from control.walk_to(game, target, within=within, timeout=WALK_TIMEOUT)
            counts["walked"] = walked.why
            counts["aim"] = (yield from control.look_at(game, target, timeout=1.5))
            counts["stance"] = _stance(game, pawn, target)
            started = game.t
            end = time.monotonic() + float(seconds)
            stepped = False
            while time.monotonic() < end:
                over = yield from swing()
                if over is not None:
                    over.data["seconds"] = game.t - started
                    return over
                # Nothing landed from here: once, closer (the game's reach may be shorter than `reach`, or the ground
                # between puts the target above or below the swing), then on until the time is up
                if (not stepped and not counts["hits"] and counts["swings"] >= CLOSER_AFTER
                        and end - time.monotonic() >= 3 * every):
                    stepped = True
                    paused = time.monotonic()
                    yield from control.walk_to(game, target, within=0.6 * within, timeout=min(5.0, WALK_TIMEOUT))
                    yield from control.look_at(game, target, timeout=1.5)
                    counts["closer"] = _stance(game, pawn, target)
                    end += time.monotonic() - paused  # (the walk doesn't eat the swings' time)
            return control.Result(False, "time up", {"seconds": game.t - started})

        t_start = time.monotonic()
        result = control.run(trial(), timeout=float(seconds) + 2 * WALK_TIMEOUT + 5.0, game=game)
    except GameError as e:
        raise _game(e, f"fight_trial({target!r})") from None
    played = round(result.data.get("seconds") if isinstance(getattr(result, "data", None), dict) and
                   result.data.get("seconds") is not None else time.monotonic() - t_start, 1)
    hits, dealt, took = counts["hits"], counts["dealt"], counts["took"]
    per_hit = round(dealt / hits, 2) if hits else None
    dead = counts["dead"] or not _alive(game, pawn, counts["last_p"])
    healed = None
    if heal and not dead and p_prop and p_health is not None and counts["last_p"] != p_health:
        healed = _heal(game, pawn, p_prop, p_health, "research: undo the trial's damage")
    out = {"target": target, "swings": counts["swings"], "hits": hits, "damage_per_hit": per_hit,
           "target_health": [t_health, counts["last_t"]], "player_health": [p_health, counts["last_p"]],
           "player_health_max": p_max, "damage_taken": round(took, 2), "seconds": played, "killed": counts["killed"],
           "player_dead": dead, "stance": counts["stance"], "closer": counts["closer"], "healed": healed,
           "health_props": {"target": t_prop, "player": p_prop}, "aim": getattr(counts["aim"], "why", None),
           "landed": land.get("landed"), "walked": counts["walked"], "stopped": result.why}
    out["verdict"] = _verdict(out, every)
    return out


def _at(stance):
    """"D units and H height" of a stance."""
    if not stance:
        return "an unread distance"
    return f"{stance['distance']} units and {stance['height']} height"


def _verdict(r, every):
    """Plain words on a fight trial's numbers."""
    words = _fight_words(r, every)
    if r.get("player_dead"):
        words += ("; the player died: the level must be restarted before the next measurement (run_harness restarts "
                  "it by default)")
    elif r.get("healed") is True:
        words += f"; the player's health was written back to {r['player_health'][0]:g}"
    elif r.get("healed") is False:
        words += (f"; the player is left at {r['player_health'][1]:g} health (its {r['health_props']['player']} "
                  "couldn't be written back)")
    return words


def _fight_words(r, every):
    if r["killed"]:
        return (f"killed: {r['hits']} hits of {r['swings']} swings in {r['seconds']} s; the player lost "
                f"{r['damage_taken']:g} health meanwhile")
    if r["health_props"]["target"] is None:
        return ("no health property found on the target (tried Health, CurrentHealth, HP... and its described "
                "properties): give health=<name>, or it may keep health on a component")
    if not r["hits"]:
        if r.get("closer"):
            return (f"no hit from {_at(r.get('stance'))}; closer ({r['closer']['distance']}) neither: not aimed (aim: "
                    f"{r['aim']}), the wrong key, a needed item or mode, an invulnerable target, or a reach under "
                    f"{r['closer']['distance']}")
        return (f"no swing of {r['swings']} changed its health from {_at(r.get('stance'))}: out of reach, not aimed "
                f"(aim: {r['aim']}), the wrong key, a needed item or mode, or an invulnerable target")
    left = r["target_health"][1] or 0.0
    rate = r["damage_per_hit"] * r["hits"] / max(r["seconds"], every)  # damage per second at this rate
    to_kill = left / rate if rate > 0 else math.inf
    hits_left = math.ceil(left / r["damage_per_hit"])
    words = (f"{hits_left} more hits ({r['damage_per_hit']:g} each) would kill it, about {to_kill:.0f} s more at "
             f"this rate ({r['hits']} hits in {r['swings']} swings over {r['seconds']} s)")
    player = r["player_health"][1]
    if r["damage_taken"] > 0 and player is not None and r["seconds"] > 0:
        taken_rate = r["damage_taken"] / r["seconds"]
        to_die = player / taken_rate if taken_rate > 0 else math.inf
        lost = f"lost {r['damage_taken']:g} in {r['seconds']} s, {player:g} left"
        if to_die < to_kill:
            words += f"; but the player would die first, in about {to_die:.0f} s ({lost})"
        else:
            words += f"; the player would outlast it ({lost})"
    elif r["damage_taken"] == 0:
        words += "; the player took no damage"
    return words


def _player_speed(game, pawn):
    """The player's walking speed, best effort: kit.control's movement() when it has it, else the pawn's
    CharacterMovement MaxWalkSpeed; None when neither says."""
    speed = None
    if hasattr(control, "movement"):
        try:
            speed = _number((control.movement(game) or {}).get("walk_speed"))
        except (GameError, TypeError, AttributeError):
            speed = None
    if speed is None:
        for comp in ("CharacterMovement", "CharMoveComp"):
            try:
                speed = _number(game.get(f"{pawn}.{comp}", "MaxWalkSpeed"))
            except GameError:
                continue
            if speed is not None:
                break
    return speed


def _rates(samples, window=0.2):
    """From enemy_profile's samples [(t, target Vec, player Vec)]: (the target's top speed, its top speed toward the
    player), over pairs of samples at least `window` seconds apart."""
    speed = approach = 0.0
    j = 0
    for i, (t0, tp0, me0) in enumerate(samples):
        while j < len(samples) and samples[j][0] - t0 < window:
            j += 1
        if j >= len(samples):
            break
        t1, tp1, me1 = samples[j]
        dt = t1 - t0
        speed = max(speed, tp0.dist(tp1) / dt)
        approach = max(approach, (tp0.dist2d(me0) - tp1.dist2d(me1)) / dt)
    return round(speed), round(approach)


def enemy_profile(game, target, seconds=20, safe_distance=None, heal=True):
    """A measurement, in a probe: what an enemy does, watched from `safe_distance` (1200) away without fighting it:
    the player is teleported there (a direct change, reported), looks at it, and for `seconds` its place and health
    and the player's are read each tick. Returns {"target", "seconds", "start_distance", "min_distance", "comes"
    (whether it came at the player), "approach_speed" (units/s toward the player, its best), "speed" (its top
    displacement per second), "player_speed" (the player's walk speed, to compare), "attack_range" (the distance at the
    first drop of the player's health), "attack_period" (seconds between drops), "damage_per_hit", "hits",
    "damage_taken", "target_health": [start, end], "regen_per_s" (its health rising on its own over the window; 0 when
    it didn't: to see regeneration after damage, run fight_trial first), "player_health": [start, end],
    "player_health_max", "stopped", "healed", "health_props", "summary" (plain words)}. The player is kept alive:
    under 40% health the watch stops and it is teleported back to `safe_distance`; heal: its health is written back
    afterwards (reported)."""
    safe = float(safe_distance or SAFE_DISTANCE)
    try:
        target = _actor(game, target)
        pawn = game.pawn
        if not pawn:
            raise GameError("no player pawn now")
        teleport(game, {"near": target, "distance": safe}, why=f"enemy profile of {target}")
        t_prop, p_prop = _health_prop(game, target), _health_prop(game, pawn)
        p_max = _max_health(game, pawn, p_prop) if p_prop else None
        read = lambda: game.batch([lambda: game.loc(target), lambda: game.me(),
                                   lambda: game.get(target, t_prop) if t_prop else None,
                                   lambda: game.get(pawn, p_prop) if p_prop else None])
        tp, me, th, ph = read()
        if isinstance(tp, GameError) or isinstance(me, GameError):
            raise tp if isinstance(tp, GameError) else me
        t_health, p_health = _number(th), _number(ph)
        floor = 0.4 * (p_max if p_max else (p_health or 0.0))
        samples = []  # (time, target at, player at)
        healths = []  # (time, target health, player health)
        state = {"stopped": "time up"}

        def watch():
            end = time.monotonic() + float(seconds)
            t_last, w_last = game.t, time.monotonic()
            clock = 0.0
            while time.monotonic() < end:
                tp, me, th, ph = read()
                if isinstance(tp, GameError):
                    state["stopped"] = "target gone"
                    break
                if isinstance(me, GameError):
                    state["stopped"] = "the player's pawn is gone"
                    break
                # (the game's clock, or real time while the game's stands still: a paused game, a loading screen)
                t_now, w_now = game.t, time.monotonic()
                clock += (t_now - t_last) if t_now > t_last else (w_now - w_last)
                t_last, w_last = t_now, w_now
                samples.append((clock, tp, me))
                healths.append((clock, _number(th), _number(ph)))
                p_now = _number(ph)
                if p_now is not None and p_now <= 0:
                    state["stopped"] = "the player died"
                    break
                if p_now is not None and floor and p_now < floor:
                    state["stopped"] = f"the player's health fell under 40% ({p_now:g}): teleported back to {safe:g}"
                    teleport(game, {"near": target, "distance": safe}, why=f"enemy profile of {target}: back to safety")
                    break
                if len(samples) == 1:  # (facing it, once the first reading is in: a fast one moves meanwhile)
                    yield from control.look_at(game, target, timeout=1.5)
                yield
            return control.Result(True, state["stopped"])

        control.run(watch(), timeout=float(seconds) + 10.0, game=game)
    except GameError as e:
        raise _game(e, f"enemy_profile({target!r})") from None
    out = _profile(target, samples, healths, t_health, p_health)
    out.update({"player_health_max": p_max, "player_speed": _player_speed(game, pawn), "stopped": state["stopped"],
                "health_props": {"target": t_prop, "player": p_prop}})
    p_last = out["player_health"][1]
    dead = not _alive(game, pawn, p_last)
    out["player_dead"] = dead
    out["healed"] = None
    if heal and not dead and p_prop and p_health is not None and p_last is not None and p_last != p_health:
        out["healed"] = _heal(game, pawn, p_prop, p_health, "research: undo the profile's damage")
    out["summary"] = _profile_summary(out)
    return out


def _profile(target, samples, healths, t_health, p_health):
    """enemy_profile's numbers from its samples."""
    seconds = round(samples[-1][0], 1) if samples else 0.0
    distances = [tp.dist2d(me) for _, tp, me in samples]
    start_d = round(distances[0]) if distances else None
    min_d = round(min(distances)) if distances else None
    speed, approach = _rates(samples)
    comes = bool(distances) and start_d - min_d > max(200.0, 0.25 * start_d)
    # the player's health drops: hits (the distance then, the damage, and the time between them); the target's rises
    # over the window (what it had before the teleport's sleep isn't the window's)
    hits, t_rise, last_t, last_p = [], 0.0, healths[0][1] if healths else t_health, p_health
    for i, (t, th, ph) in enumerate(healths):
        if last_p is not None and ph is not None and ph < last_p:
            hits.append((t, last_p - ph, distances[i]))
        if last_t is not None and th is not None and th > last_t:
            t_rise += th - last_t
        last_t = th if th is not None else last_t
        last_p = ph if ph is not None else last_p
    gaps = [b[0] - a[0] for a, b in zip(hits, hits[1:])]
    return {"target": target, "seconds": seconds, "start_distance": start_d, "min_distance": min_d, "comes": comes,
            "approach_speed": approach, "speed": speed,
            "attack_range": round(hits[0][2]) if hits else None,
            "attack_period": round(sum(gaps) / len(gaps), 2) if gaps else None,
            "damage_per_hit": round(sum(h[1] for h in hits) / len(hits), 2) if hits else None, "hits": len(hits),
            "damage_taken": round(sum(h[1] for h in hits), 2), "target_health": [t_health, last_t],
            "regen_per_s": (round(t_rise / seconds, 2) if seconds else 0.0) if t_health is not None else None,
            "player_health": [p_health, last_p]}


def _profile_summary(r):
    """Plain words on an enemy's profile."""
    parts = []
    player = r.get("player_speed")
    walks = f" (the player walks {player:g})" if player else ""
    if r["comes"]:
        words = f"comes at the player at {r['approach_speed']:g}/s{walks}"
        if player:
            words += ": can be out-run" if r["approach_speed"] < 0.9 * player else ": can't be out-run"
        parts.append(words)
    else:
        parts.append(f"doesn't come at the player in {r['seconds']:g} s from {r['start_distance']} away (passive "
                     f"until hit?); moves at {r['speed']:g}/s at most{walks}")
    if r["hits"]:
        words = f"hits for {r['damage_per_hit']:g}"
        if r["attack_period"] is not None:
            words += f" every {r['attack_period']:g} s"
        words += f" from {r['attack_range']} away"
        parts.append(words)
    else:
        parts.append(f"never attacked (nearest {r['min_distance']})")
    if r["regen_per_s"] is None:
        parts.append("no health property found on it")
    elif r["regen_per_s"] > 0:
        parts.append(f"health regenerates {r['regen_per_s']:g}/s")
    else:
        parts.append("health doesn't regenerate on its own")
    if r.get("player_dead"):
        parts.append("the player died: the level must be restarted (run_harness restarts it by default)")
    elif r["stopped"] != "time up":
        parts.append(f"stopped early: {r['stopped']}")
    return "; ".join(parts)


def call_any(game, obj, names, **params):
    """Call the first of `names` the game accepts on obj (the same parameters for each): {"called": name, "result":
    what it returned}; raises GameError with every refusal when none is. For a function whose name the code hints at
    in several ways (GiveItem, AddItem, AddToInventory...). A name that changes state is reported as a direct
    change."""
    errors = {}
    for name in names:
        try:
            return {"called": name, "result": game.call(obj, name, **params)}
        except GameError as e:
            errors[name] = str(e)
    raise GameError(f"call_any({obj}): none accepted: " + "; ".join(f"{n}: {e}" for n, e in errors.items()))


# -- for the probe namespace ---------------------------------------------------------------------------------

NAMES = {"tables": tables, "rows": rows, "row": row, "cell": cell, "grep": grep, "nearest": nearest,
         "teleport": teleport, "fight_trial": fight_trial, "enemy_profile": enemy_profile, "call_any": call_any}


def names_for(game=None, root=None):
    """NAMES with the codex helpers' root bound to the game's codex (or `root`), so a probe calls rows("DT_X") with no
    root. Without either, NAMES as they are (ROOT, or root= per call)."""
    try:
        root = _root(root if root is not None else game)
    except (ValueError, FileNotFoundError):
        return dict(NAMES)
    out = dict(NAMES)
    for name in ("tables", "rows", "row", "grep"):
        out[name] = functools.wraps(NAMES[name])(functools.partial(NAMES[name], root=root))
    return out


DOC = """## Research helpers (in probes)

- `tables()`: the codex's data tables: [{"name", "path", "rows", "struct"}]. `rows("DT_Enemies", where="Goblin")`: a
  table's rows as dicts (column -> value; numbers as numbers; a nested `(Key=Value,...)` struct as its text); `where`:
  a regex over the row's name, or {column: substring}. `row("DT_Enemies", "Goblin")`: one row by its name.
  `cell(value, "Drops.0.Chance")`: a field out of a struct's text (`cell(value)`: the whole struct as dicts and lists).
- `grep(r"ApplyDamage", where="blueprints", limit=50)`: "path:line: text" of the lines matching a regex across the
  codex (or one of its folders): many looks in one probe, not one model turn each.
- `nearest(game, "BP_Goblin", n=5, within=3000)`: actors of a class by distance from the player: [(path, Vec,
  distance)].
- `teleport(game, to, actor=None, why="research")`: move the player's pawn (or `actor`) to a Vec, or next to an actor
  path (150 short of it, facing it; {"near": path, "distance": 300} for another distance). A direct change, only to set
  up a measurement in a probe (never in a harness: play is Jev's, through inputs); every one is reported.
- `fight_trial(game, target, key="LeftMouseButton", seconds=20, reach=None, heal=True)`: a measurement: get in reach
  of the target (an actor path or a class name: its nearest; teleported near it, then walked the last stretch so the
  player stands on the game's own ground), look at it, press `key` every 0.6 s, reading its health and the player's
  each swing; when the first swings land nothing, once closer (0.6 of the reach) -> {swings, hits, damage_per_hit,
  target_health, player_health, damage_taken, killed, player_dead, stance: {distance, height: the target's box bottom
  above the player's feet}, closer, healed, verdict...}. Tells, before the harness is written, whether the fight can be
  won and how long it takes (health="Prop" names the health property when Health/CurrentHealth/HP aren't the game's).
  heal=True writes the player's health back to what it was (reported), so the next measurement doesn't start hurt; a
  player the trial killed stays dead (player_dead: restart the level).
- `enemy_profile(game, target, seconds=20, safe_distance=None)`: a measurement without a fight: the player watches the
  target from 1200 away (teleported there) for `seconds` -> {comes (whether it came at the player), approach_speed,
  speed, player_speed, attack_range (the distance at the first drop of the player's health), attack_period,
  damage_per_hit, regen_per_s (its health rising on its own; after damage: run fight_trial first), summary...}. The
  player is kept alive (under 40% health it is teleported back) and healed afterwards.
- `call_any(game, obj, ["GiveItem", "AddItem", "AddToInventory"], Item=..., Amount=1)`: the first function name the game
  accepts -> {"called", "result"}; GameError with every refusal when none is."""
