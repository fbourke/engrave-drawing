#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "ezdxf>=1.1",
#     "numpy>=1.24",
#     "svgelements>=1.9",
#     "shapely>=2.0",
# ]
# ///
"""
plotprep - turn SVGs (or messy DXFs) into DXFs that Silhouette Studio imports cleanly.

Studio's DXF importer silently drops SPLINE entities and is happiest with
DXF R12 polylines. Exports from Affinity/Illustrator are usually mostly splines,
often shattered into one entity per curve segment. This script flattens
everything to polylines, chains the fragments back into continuous paths,
simplifies, and writes R12.

    plotprep.py in.svg
    plotprep.py in.svg -o out.dxf --page 216x279 --clip
    plotprep.py messy.dxf --tol 0.08 --min-len 1.0
    plotprep.py *.svg --outdir ./dxf

Deps:  pip install ezdxf numpy svgelements
       (optional, only for --clip)  pip install shapely
"""
import argparse, math, os, sys
from collections import defaultdict

import logging

import numpy as np

logging.getLogger('ezdxf').setLevel(logging.ERROR)

try:
    import ezdxf
except ImportError:
    sys.exit("Missing dependencies. This script declares its own deps (PEP 723);\n"
             "run it through uv so they get installed automatically:\n"
             "    uv run ./plotprep.py <files>\n"
             "or, if the file is executable, just:  ./plotprep.py <files>\n"
             "(install uv with:  brew install uv)")

MM_PER_IN = 25.4
# DXF $INSUNITS -> mm
INSUNITS_MM = {0: 1.0, 1: MM_PER_IN, 2: MM_PER_IN * 12, 4: 1.0, 5: 10.0, 6: 1000.0}


# ---------------------------------------------------------------- geometry
def rdp(pts, eps):
    """Douglas-Peucker, iterative."""
    p = np.asarray(pts, dtype=float)
    n = len(p)
    if n < 3 or eps <= 0:
        return [tuple(v) for v in p]
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        seg = p[i1] - p[i0]
        L = math.hypot(*seg)
        if L == 0:
            d = np.hypot(*(p[i0 + 1:i1] - p[i0]).T)
        else:
            d = np.abs(seg[0] * (p[i0 + 1:i1, 1] - p[i0, 1])
                       - seg[1] * (p[i0 + 1:i1, 0] - p[i0, 0])) / L
        im = int(np.argmax(d)) + i0 + 1
        if d[im - i0 - 1] > eps:
            keep[im] = True
            stack += [(i0, im), (im, i1)]
    return [tuple(v) for v in p[keep]]


def dedupe(pts, tol=1e-9):
    out = []
    for p in pts:
        if not out or abs(out[-1][0] - p[0]) > tol or abs(out[-1][1] - p[1]) > tol:
            out.append((float(p[0]), float(p[1])))
    return out


def plen(pts):
    a = np.asarray(pts, dtype=float)
    return float(np.hypot(*np.diff(a, axis=0).T).sum()) if len(a) > 1 else 0.0


def chain(segs, tol):
    """Join fragments whose endpoints coincide into continuous polylines.

    Vector exporters routinely emit one entity per curve segment; without this
    you get thousands of 0.3mm paths and thousands of pen lifts.
    """
    if not segs:
        return []
    key = lambda p: (round(p[0] / tol), round(p[1] / tol))
    ends = defaultdict(list)
    for i, s in enumerate(segs):
        ends[key(s[0])].append(i)
        ends[key(s[-1])].append(i)
    used = [False] * len(segs)
    out = []
    for i in range(len(segs)):
        if used[i]:
            continue
        used[i] = True
        ch = list(segs[i])
        for forward in (True, False):
            while True:
                tip = ch[-1] if forward else ch[0]
                nxt = None
                for j in ends.get(key(tip), ()):
                    if used[j]:
                        continue
                    a, b = segs[j][0], segs[j][-1]
                    if key(a) == key(tip):
                        nxt = (j, False)
                        break
                    if key(b) == key(tip):
                        nxt = (j, True)
                        break
                if nxt is None:
                    break
                j, rev = nxt
                used[j] = True
                piece = segs[j][::-1] if rev else segs[j]
                if forward:
                    ch = ch + piece[1:]
                else:
                    ch = piece[::-1][1:][::-1] + ch if False else list(reversed(piece[1:])) + ch
        out.append(ch)
    return out


# ---------------------------------------------------------------- readers
def read_svg(path, curve_res):
    """Returns (segments, width_mm, height_mm). svgelements applies transforms,
    viewBox and unit conversion for us."""
    try:
        from svgelements import SVG, Path, Shape, Group, Use
    except ImportError:
        sys.exit("need svgelements for SVG input; run via 'pipx run ./plotprep.py'")

    svg = SVG.parse(path, reify=True, ppi=96.0)
    W = float(svg.width) if svg.width else None
    H = float(svg.height) if svg.height else None

    segs = []
    for el in svg.elements():
        try:
            p = abs(Path(el))          # Shape -> Path, applies transform
        except Exception:
            continue
        if len(p) == 0:
            continue
        for sub in p.as_subpaths():
            sp = Path(sub)
            pts = []
            for seg in sp:
                nm = type(seg).__name__
                if nm in ("Move",):
                    continue
                if nm in ("Line", "Close"):
                    # straight: two points, not 4000 samples
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
    # user units here are px at 96dpi; convert to mm
    k = MM_PER_IN / 96.0
    segs = [[(x * k, y * k) for x, y in s] for s in segs]
    wmm = W * k if W else None
    hmm = H * k if H else None
    return segs, wmm, hmm


def read_dxf(path, curve_res):
    doc = ezdxf.readfile(path)
    ins = doc.header.get("$INSUNITS", 0)
    k = INSUNITS_MM.get(ins, 1.0)
    segs = []
    dropped = defaultdict(int)
    for e in doc.modelspace():
        t = e.dxftype()
        if t == "SPLINE":
            ct = e.construction_tool()
            n = max(4, min(2000, int(ct.max_t * 20) + 4))
            pts = [(p.x * k, p.y * k) for p in ct.points(np.linspace(0, ct.max_t, n))]
        elif t == "LWPOLYLINE":
            pts = [(p[0] * k, p[1] * k) for p in e.get_points()]
            if e.closed and pts and pts[0] != pts[-1]:
                pts.append(pts[0])
        elif t == "POLYLINE":
            pts = [(v.dxf.location.x * k, v.dxf.location.y * k) for v in e.vertices]
            if e.is_closed and pts and pts[0] != pts[-1]:
                pts.append(pts[0])
        elif t == "LINE":
            pts = [(e.dxf.start.x * k, e.dxf.start.y * k),
                   (e.dxf.end.x * k, e.dxf.end.y * k)]
        elif t in ("ARC", "CIRCLE", "ELLIPSE"):
            cx, cy = e.dxf.center.x * k, e.dxf.center.y * k
            if t == "CIRCLE":
                r = e.dxf.radius * k
                a0, a1 = 0.0, 360.0
            elif t == "ARC":
                r = e.dxf.radius * k
                a0, a1 = e.dxf.start_angle, e.dxf.end_angle
                if a1 <= a0:
                    a1 += 360.0
            else:
                dropped["ELLIPSE"] += 1
                continue
            n = max(8, int(abs(a1 - a0) / 360.0 * 2 * math.pi * r / max(curve_res, 1e-6)))
            n = min(n, 2000)
            ang = np.radians(np.linspace(a0, a1, n))
            pts = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in ang]
        else:
            dropped[t] += 1
            continue
        pts = dedupe(pts)
        if len(pts) >= 2:
            segs.append(pts)
    return segs, dropped


# ---------------------------------------------------------------- helpers
def drop_page_rect(segs, wmm, hmm, tol=0.5):
    """Remove a background rectangle covering the whole page - it isn't art,
    and a plotter will happily trace it."""
    if not wmm or not hmm:
        return segs, 0
    out, n = [], 0
    for s in segs:
        a = np.asarray(s)
        covers = (a[:, 0].min() <= tol and a[:, 0].max() >= wmm - tol
                  and a[:, 1].min() <= tol and a[:, 1].max() >= hmm - tol)
        if covers and len(rdp(s, 0.2)) <= 6:
            n += 1
            continue
        out.append(s)
    return out, n


def apply_clip(segs, clip_idx, segs_all):
    try:
        from shapely.geometry import Polygon, LineString
    except ImportError:
        sys.exit("--clip needs shapely; run via 'pipx run ./plotprep.py'")
    poly = Polygon(segs_all[clip_idx]).buffer(0)
    out = []
    for i, s in enumerate(segs):
        if i == clip_idx:
            continue
        r = LineString(s).intersection(poly)
        if r.is_empty:
            continue
        for g in getattr(r, "geoms", [r]):
            if g.geom_type == "LineString" and len(g.coords) >= 2:
                out.append([(x, y) for x, y in g.coords])
    return out


# ---------------------------------------------------------------- main
def convert(path, args):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".svg":
        segs, wmm, hmm = read_svg(path, args.curve_res)
        dropped = {}
        # SVG y-axis points down; DXF up
        if hmm:
            segs = [[(x, hmm - y) for x, y in s] for s in segs]
        else:
            ymax = max(max(y for _, y in s) for s in segs)
            segs = [[(x, ymax - y) for x, y in s] for s in segs]
    elif ext == ".dxf":
        segs, dropped = read_dxf(path, args.curve_res)
        wmm = hmm = None
    else:
        print(f"  skip {path}: not .svg or .dxf", file=sys.stderr)
        return

    n_in = len(segs)
    if not segs:
        print(f"  {path}: nothing to convert", file=sys.stderr)
        return

    if not args.keep_page_rect:
        segs, nrect = drop_page_rect(segs, wmm, hmm)
    else:
        nrect = 0

    if args.clip:
        # largest-area closed path is assumed to be the mask
        areas = []
        for i, s in enumerate(segs):
            a = np.asarray(s)
            if np.hypot(*(a[0] - a[-1])) < args.chain_tol and len(s) > 8:
                x, y = a[:, 0], a[:, 1]
                areas.append((abs(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)) / 2, i))
        if areas:
            areas.sort()
            segs = apply_clip(segs, areas[-1][1], segs)

    # measure immediately before/after chaining so the figure isolates chaining
    len_in = sum(plen(s) for s in segs)
    chains = chain(segs, args.chain_tol) if not args.no_chain else segs
    len_out = sum(plen(c) for c in chains)

    doc = ezdxf.new(args.dxf_version)
    msp = doc.modelspace()
    kept = pts = 0
    for c in chains:
        a = np.asarray(c)
        closed = len(c) > 3 and np.hypot(*(a[0] - a[-1])) < args.chain_tol
        if closed:
            c = c[:-1]
        s = [(round(x, 3), round(y, 3)) for x, y in rdp(c, args.tol)]
        if len(s) < 2:
            continue
        if plen(s) < args.min_len:
            continue
        msp.add_polyline2d(s, close=bool(closed))
        kept += 1
        pts += len(s)

    out = args.output or os.path.join(
        args.outdir or os.path.dirname(path) or ".",
        os.path.splitext(os.path.basename(path))[0] + "-plot.dxf")
    doc.saveas(out)

    keptpct = 100 * len_out / len_in if len_in else 0
    print(f"  {os.path.basename(path)} -> {os.path.basename(out)}")
    print(f"     {n_in} input paths -> {kept} polylines, {pts} pts"
          + (f", page rect dropped" if nrect else ""))
    print(f"     path length preserved through chaining: {keptpct:.1f}%")
    if dropped:
        print("     ignored entities: "
              + ", ".join(f"{k}x{v}" for k, v in dropped.items()))
    if keptpct < 99.0 and not args.no_chain:
        print("     ^ under 100% means geometry was lost; try --chain-tol larger",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(
        description="SVG/DXF -> Silhouette-Studio-safe DXF (R12 polylines, mm).",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--output", help="output file (single input only)")
    ap.add_argument("--outdir", help="output directory")
    ap.add_argument("--tol", type=float, default=0.05,
                    help="simplification tolerance in mm (default 0.05)")
    ap.add_argument("--curve-res", type=float, default=0.1,
                    help="curve flattening resolution in mm (default 0.1)")
    ap.add_argument("--chain-tol", type=float, default=0.02,
                    help="endpoint join tolerance in mm (default 0.02)")
    ap.add_argument("--min-len", type=float, default=0.0,
                    help="drop paths shorter than this, mm (default 0 = keep all). "
                         "Use AFTER checking the chaining report, or you will "
                         "delete real fragments.")
    ap.add_argument("--clip", action="store_true",
                    help="clip everything to the largest closed path (honours a "
                         "clipPath-style mask); needs shapely")
    ap.add_argument("--keep-page-rect", action="store_true",
                    help="keep a full-page background rectangle")
    ap.add_argument("--no-chain", action="store_true")
    ap.add_argument("--dxf-version", default="R12",
                    help="R12 (default, safest for Studio) or R2000")
    args = ap.parse_args()

    if args.output and len(args.inputs) > 1:
        sys.exit("-o works with a single input; use --outdir for batches")
    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)
    for p in args.inputs:
        convert(p, args)


if __name__ == "__main__":
    main()
