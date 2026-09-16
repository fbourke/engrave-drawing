#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "bleak>=0.22",
#     "svgelements>=1.9",
# ]
# ///
"""
silhouette_ble - plot an SVG straight to a Silhouette Cameo 3 over Bluetooth LE.

Skips Silhouette Studio entirely, which matters here: Studio takes tens of minutes
and tens of gigabytes of RAM to import an engraving's worth of geometry.

    ./silhouette_ble.py probe                     # identify the cutter, no motion
    ./silhouette_ble.py plot out.svg              # plot it
    ./silhouette_ble.py plot out.svg --x 20 --y 20 --force 10
    ./silhouette_ble.py plot out.svg --dry-run    # costs, extents, no connection

Coordinates come from engrave.py's SVG, whose path data is already in millimetres.
--x/--y place the drawing's top-left on the page.

Quit Silhouette Studio first: it holds the BLE connection, and the cutter stops
advertising while connected.

--------------------------------------------------------------------- protocol
Established by probing CAMEO3-818462R, firmware V1.60. Other models will differ.

The Cameo 3 advertises BLE only - no Bluetooth Classic, no SPP - so the RFCOMM
path in inkscape-silhouette cannot reach it, and macOS never exposes it as a
/dev/cu.* serial device. Its vendor GATT service carries GPGL instead:

    6d92661d...   write            GPGL goes here. Write only; it never indicates.
    8dcf199a...   indicate,write   replies
    61490654...   indicate,write   replies

Writing to either indicating characteristic fails with "Invalid Attribute Value
Length" whatever the payload, so the write-only one is the data channel.

Replies are not bound to a channel by role. A bare busy/ready digit ('0' ready,
'1' moving, '2' unloaded) and a substantive answer like the FG version string have
each been seen on both indicating characteristics, so callers must distinguish
them by content. Binding a channel to a role looks right for a while and then
silently reports every query as a status.

Two limits decide how a job has to be framed:

  * The cutter silently discards any GPGL command longer than about 31 bytes. It
    acknowledges the BLE write and reports itself ready, so nothing surfaces as an
    error - the marks just go missing. Measured: 3 coordinate pairs execute, 4
    never do. Hence MAX_PAIRS.
  * Bursting packed writes loses commands too, so this sends one command per write.

Throughput is command-bound rather than byte-bound: ~31 commands/sec, steady over
a 30,000 command job with no dropouts. A 11k stroke engraving plots in ~16 minutes.

GPGL itself: 20 steps per mm, every command terminated with \\x03, and coordinates
emitted Y FIRST ("M<y>,<x>"). \\x1b\\x04 initializes the device and must start a job.
G returns "<y>, <x>, <tool><pen>" - pen 1 is down, 0 is up, which is the only
reliable way to tell whether a draw actually ran.
"""
import argparse, asyncio, math, re, sys, time

from bleak import BleakScanner, BleakClient

ETX = b"\x03"
INIT = b"\x1b\x04"                # initialize device
ENQ = b"\x1b\x05"                 # query status
SU_PER_MM = 20.0
MAX_PAIRS = 3                     # >3 coordinate pairs exceeds the ~31 byte limit
NAME_HINT = "CAMEO"
STD_SERVICES = ("00001800", "00001801", "0000180a", "0000180f")
STATUS = {b"0": "ready", b"1": "moving", b"2": "unloaded (no media)"}
# from the U query on this machine: 20320,5900 steps = 1016mm of feed, 295mm across
MAX_X_MM, MAX_Y_MM = 295.0, 1016.0


def su(mm):
    return int(round(mm * SU_PER_MM))


def cmd(s):
    return s.encode("ascii") + ETX


# ----------------------------------------------------------------- geometry
MM_PER_IN = 25.4


def dedupe(pts, tol=1e-9):
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > tol or abs(p[1] - out[-1][1]) > tol:
            out.append(p)
    return out


def load_svg(path, curve_res=0.1):
    """Every drawable path in the file, as polylines in millimetres.

    Parsed with svgelements rather than by regex, which matters for anything not
    produced by engrave.py: it applies group transforms, the viewBox and unit
    conversion, flattens curves, keeps subpaths apart, and picks up line/polyline/
    rect/circle elements. Scraping coordinates out of the d attribute instead
    quietly mangles all of those - a curve's control points become vertices, two
    subpaths get joined by a line that should not be drawn, and a transformed
    document plots at the wrong size. The web app's own paper-size export carries a
    group transform, so it is one of the files that needs this.

    Returns (polylines, width_mm, height_mm).
    """
    from svgelements import SVG, Path

    svg = SVG.parse(path, reify=True, ppi=96.0)
    W = float(svg.width) if svg.width else None
    H = float(svg.height) if svg.height else None

    segs = []
    for el in svg.elements():
        try:
            p = abs(Path(el))                 # Shape -> Path, transforms applied
        except Exception:
            continue
        if len(p) == 0:
            continue
        for sub in p.as_subpaths():
            pts = []
            for seg in Path(sub):
                nm = type(seg).__name__
                if nm == "Move":
                    continue
                if nm in ("Line", "Close"):
                    if seg.start is not None:
                        pts.append((seg.start.x, seg.start.y))
                    if seg.end is not None:
                        pts.append((seg.end.x, seg.end.y))
                    continue
                try:
                    L = seg.length(error=1e-3)
                except Exception:
                    L = 0.0
                n = max(4, min(400, int(L / max(curve_res, 1e-6))))
                for t in range(n + 1):
                    q = seg.point(t / n)
                    pts.append((q.x, q.y))
            pts = dedupe(pts)
            if len(pts) >= 2:
                segs.append(pts)

    k = MM_PER_IN / 96.0                      # user units are px at 96dpi
    segs = [[(x * k, y * k) for x, y in s] for s in segs]
    return segs, (W * k if W else None), (H * k if H else None)


def pen_travel(strokes):
    d = px = py = 0.0
    for s in strokes:
        d += math.hypot(s[0][0] - px, s[0][1] - py)
        px, py = s[-1]
    return d


def serpentine(strokes, band):
    """horizontal bands, alternating direction - see engrave.py's serpentine()"""
    def key(s):
        b = int(s[0][1] / band)
        return (b, s[0][0] if b % 2 == 0 else -s[0][0])
    return sorted(strokes, key=key)


def commands_for(strokes, ox, oy):
    """GPGL for the whole drawing, every command within the length limit"""
    out = []
    for s in strokes:
        out.append(cmd("M%d,%d" % (su(s[0][1] + oy), su(s[0][0] + ox))))
        rest = s[1:]
        for i in range(0, len(rest), MAX_PAIRS):
            seg = rest[i:i + MAX_PAIRS]
            out.append(cmd("D" + ",".join("%d,%d" % (su(y + oy), su(x + ox))
                                          for x, y in seg)))
    return out


# ----------------------------------------------------------------- transport
class Cameo:
    def __init__(self, client, write_uuid, notify_uuids):
        self.c = client
        self.w = write_uuid
        self.rx = {u: bytearray() for u in notify_uuids}

    async def start(self):
        for u, buf in self.rx.items():
            await self.c.start_notify(u, lambda _h, d, b=buf: b.extend(d))

    async def send(self, payload):
        await self.c.write_gatt_char(self.w, payload, response=True)

    async def query(self, payload, wait=2.0):
        """Every reply that arrives, keyed by channel.

        Do not assume a channel has a fixed role: the busy/ready byte and the
        substantive reply have both been observed on either characteristic, so the
        caller distinguishes them by content, not by which one answered.
        """
        for b in self.rx.values():
            b.clear()
        await self.send(payload)
        out = {}
        for _ in range(int(wait / 0.05)):
            await asyncio.sleep(0.05)
            out = {u[:8]: bytes(b).rstrip(ETX).decode(errors="replace").strip()
                   for u, b in self.rx.items() if b.endswith(ETX)}
            if out:
                break
        return out

    async def reply(self, payload):
        """the substantive answer, ignoring a bare status digit"""
        for v in (await self.query(payload)).values():
            if len(v) > 1 or not v.isdigit():
                return v
        return ""

    async def status(self):
        for v in (await self.query(ENQ, wait=1.5)).values():
            if v in ("0", "1", "2"):
                return STATUS[v.encode()]
        return "unknown"

    async def position(self):
        """'<y>, <x>, <tool><pen>'; the trailing pen digit is 1 when down"""
        return await self.reply(cmd("G")) or "?"

    async def wait_ready(self, timeout=300):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if await self.status() == "ready":
                return True
            await asyncio.sleep(0.3)
        return False

    async def begin(self, tool, speed, force):
        """initialize, then set up the tool. A job that skips the init leaves the
        cutter in its previous finished state."""
        await self.send(INIT)
        await asyncio.sleep(0.4)
        for c in (cmd("J%d" % tool), cmd("!%d,%d" % (speed, tool)),
                  cmd("FX%d,%d" % (force, tool)), cmd("FC0,1,%d" % tool)):
            await self.send(c)
            await asyncio.sleep(0.12)


async def connect(timeout=10.0):
    devs = await BleakScanner.discover(timeout=timeout)
    hits = [d for d in devs if (d.name or "").upper().startswith(NAME_HINT)]
    if not hits:
        print("No Cameo is advertising.\n"
              "Quit Silhouette Studio if it is running - it holds the connection and\n"
              "the cutter stops advertising while connected.", file=sys.stderr)
        return None
    return hits[0]


def pick_channels(client):
    """data channel = writable but never indicates; the indicating ones reply"""
    data, notifies = None, []
    for svc in client.services:
        if svc.uuid.lower().startswith(STD_SERVICES):
            continue
        for ch in svc.characteristics:
            p = set(ch.properties)
            if {"notify", "indicate"} & p:
                notifies.append(ch.uuid)
            elif {"write", "write-without-response"} & p and data is None:
                data = ch.uuid
    return data, notifies


# ----------------------------------------------------------------- commands
async def do_probe(cam, client):
    for uuid, label in (("00002a24-0000-1000-8000-00805f9b34fb", "model"),
                        ("00002a29-0000-1000-8000-00805f9b34fb", "manufacturer")):
        try:
            v = bytes(await client.read_gatt_char(uuid)).rstrip(b"\x00").decode()
            print(f"{label:13}: {v}")
        except Exception:
            pass
    print(f"firmware     : {await cam.reply(cmd('FG'))}")
    print(f"usable area  : {await cam.reply(cmd('U'))} steps (/20 for mm)")
    print(f"status       : {await cam.status()}")
    print(f"head at      : {await cam.position()}")
    print("\nprobe only - nothing was moved.")


async def do_plot(cam, args, cmds):
    print(f"tool {args.tool}, speed {args.speed}, force {args.force}\n")
    await cam.begin(args.tool, args.speed, args.force)

    t0 = time.time()
    for i, payload in enumerate(cmds):
        await cam.send(payload)
        if i and i % 500 == 0:
            el = time.time() - t0
            rate = i / el
            print(f"   {i:6}/{len(cmds)}  {rate:5.1f} cmd/s  "
                  f"elapsed {el/60:4.1f}m  eta {(len(cmds)-i)/rate/60:4.1f}m",
                  flush=True)
    el = time.time() - t0
    print(f"\nsent {len(cmds)} commands in {el/60:.1f}m ({len(cmds)/el:.1f} cmd/s)")
    await cam.wait_ready()
    await cam.send(cmd("M0,0"))
    await cam.send(cmd("J0"))
    print("done.")


async def main(args):
    cmds = None
    if args.mode == "plot":
        strokes, wmm, hmm = load_svg(args.svg, args.curve_res)
        if not strokes:
            sys.exit(f"no paths found in {args.svg}")
        if args.limit:
            strokes = strokes[:args.limit]
        xs = [p[0] for s in strokes for p in s]
        ys = [p[1] for s in strokes for p in s]
        before = pen_travel(strokes)
        if not args.no_sort:
            strokes = serpentine(strokes, args.sort_band)
        after = pen_travel(strokes)
        cmds = commands_for(strokes, args.x, args.y)
        longest = max(len(c) for c in cmds)
        print(f"{len(strokes)} strokes -> {len(cmds)} commands, longest {longest}B")
        print(f"occupies x {args.x+min(xs):.0f}..{args.x+max(xs):.0f}mm, "
              f"y {args.y+min(ys):.0f}..{args.y+max(ys):.0f}mm")
        print(f"pen-up travel {before/1000:.1f}m -> {after/1000:.1f}m")
        print(f"transfer takes about {len(cmds)/31.0/60:.0f} min at ~31 cmd/s; "
              f"the cutter keeps drawing from its buffer after that")
        if longest > 31:
            print("WARNING: a command exceeds the 31B limit and will be dropped")
        if wmm and hmm:
            print(f"the file declares a {wmm:.0f}x{hmm:.0f}mm page - if the drawing is "
                  f"already placed on it, plot with --x 0 --y 0")
        x1, y1 = args.x + max(xs), args.y + max(ys)
        if x1 > MAX_X_MM or y1 > MAX_Y_MM:
            print(f"WARNING: extends to {x1:.0f}x{y1:.0f}mm, past the machine's usable "
                  f"{MAX_X_MM:.0f}x{MAX_Y_MM:.0f}mm - it will run off the media")
        if args.dry_run:
            return 0

    dev = await connect()
    if dev is None:
        return 1
    print(f"\nconnecting to {dev.name} ...", flush=True)
    async with BleakClient(dev) as client:
        w, n = pick_channels(client)
        if not w:
            print("no write-only vendor characteristic found", file=sys.stderr)
            return 1
        cam = Cameo(client, w, n)
        await cam.start()
        print(f"connected, mtu={getattr(client, 'mtu_size', '?')}\n")
        if args.mode == "probe":
            await do_probe(cam, client)
        else:
            await do_plot(cam, args, cmds)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["probe", "plot"])
    ap.add_argument("svg", nargs="?", help="svg to plot (mm coordinates)")
    ap.add_argument("--x", type=float, default=20.0, help="left edge on the page, mm")
    ap.add_argument("--y", type=float, default=20.0, help="top edge on the page, mm")
    ap.add_argument("--tool", type=int, default=1, help="tool holder, 1 or 2")
    ap.add_argument("--force", type=int, default=10, help="1-33; a pen wants 3-15")
    ap.add_argument("--speed", type=int, default=5, help="1-10")
    ap.add_argument("--sort-band", type=float, default=4.0,
                    help="height of a serpentine ordering band, mm")
    ap.add_argument("--no-sort", action="store_true",
                    help="plot in file order; engrave.py already sorts")
    ap.add_argument("--curve-res", type=float, default=0.1,
                    help="curve flattening resolution, mm")
    ap.add_argument("--limit", type=int, default=0, help="only the first N strokes")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.mode == "plot" and not a.svg:
        ap.error("plot needs an svg")
    sys.exit(asyncio.run(main(a)))
