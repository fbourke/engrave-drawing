#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = [
#     "numpy>=1.24",
#     "pillow>=10.0",
#     "scipy>=1.10",
# ]
# ///
"""
engrave - turn a photo into pen-plottable stipple + hatch line art.

The look: strokes follow the image's edge-tangent flow (so hatching curves
around forms), stroke density rises with darkness, and stroke length falls
to zero in light areas - which is what turns hatching into stippling. It's
one continuous mechanism, not two separate modes.

Pipeline:
  1. tone      - luminance -> darkness in [0,1], gamma corrected
  2. flow      - structure tensor, smoothed; minor eigenvector = isophote
                 direction (the direction along which tone changes least)
  3. seeding   - variable-radius Poisson disk; radius shrinks where dark
  4. tracing   - RK2 streamlines through the flow field, length ~ darkness
  5. output    - SVG in mm, ready for plotprep.py / inkcut

    ./engrave.py photo.jpg -o out.svg --width 200
    ./engrave.py photo.jpg --detail 1.5 --contrast 1.3 --flow-blur 6
"""
import argparse, math, os, sys

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, gaussian_filter1d, map_coordinates


# ----------------------------------------------------------------- tone
def load_tone(path, max_px, contrast, gamma, invert, white_point=0.0):
    im = Image.open(path).convert("L")
    w, h = im.size
    s = min(1.0, max_px / max(w, h))
    if s < 1.0:
        im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32) / 255.0
    if invert:
        a = 1.0 - a
    t = 1.0 - a                                   # darkness
    # contrast about the midpoint, then gamma
    t = np.clip((t - 0.5) * contrast + 0.5, 0, 1)
    t = np.power(t, gamma)
    # white point: everything below it becomes exactly zero, and zero means no
    # ink at all (poisson_seeds refuses to seed there). The rest is rescaled so
    # the tonal range still spans 0..1 instead of being shifted darker.
    if white_point > 0:
        t = np.clip((t - white_point) / max(1e-6, 1.0 - white_point), 0, 1)
    return t.astype(np.float32)


# ----------------------------------------------------------------- flow
def edge_tangent_flow(tone, sigma, passes=3):
    """Structure tensor -> minor eigenvector field (isophote tangents).

    Smoothing the *tensor* rather than the angle avoids the wraparound
    problem: directions are a line field (theta == theta+pi), so averaging
    angles directly is wrong. The tensor handles that automatically.
    """
    gx = gaussian_filter(tone, 1.0, order=(0, 1))
    gy = gaussian_filter(tone, 1.0, order=(1, 0))
    E, F, G = gx * gx, gx * gy, gy * gy
    for _ in range(passes):
        E = gaussian_filter(E, sigma)
        F = gaussian_filter(F, sigma)
        G = gaussian_filter(G, sigma)
    # principal eigenvector of [[E,F],[F,G]] = gradient dir; we want the other
    theta = 0.5 * np.arctan2(2 * F, E - G)        # major axis angle
    tx, ty = -np.sin(theta), np.cos(theta)        # rotate 90deg -> tangent
    # anisotropy: 0 where tone is locally flat (no meaningful direction)
    lam = np.sqrt(np.maximum((E - G) ** 2 + 4 * F * F, 0))
    tr = E + G + 1e-12
    coh = lam / tr
    return tx.astype(np.float32), ty.astype(np.float32), coh.astype(np.float32)


# ----------------------------------------------------------------- seeding
def coverage_radius(tone, L_px, pen_px, max_cov, r_min, r_max, packing=0.75):
    """Choose local seed spacing so that ink coverage matches tone.

    A stroke lays down L*pen of ink. To cover a fraction A of the area we need
    A/(L*pen) strokes per unit area, and a Poisson-disk field of radius r has
    about packing/r^2 points per unit area. Hence r = sqrt(packing*L*pen/A).
    Scaling density AND length with darkness instead makes coverage go as
    tone^2, which floods the shadows to solid black.
    """
    A = np.clip(tone, 1e-3, max_cov)
    r = np.sqrt(packing * np.maximum(L_px, pen_px) * pen_px / A)
    return np.clip(r, r_min, r_max).astype(np.float32)


def poisson_seeds(tone, rad, r_light, rng, oversample=12):
    """Variable-radius dart throwing against a precomputed radius field."""
    H, W = tone.shape
    r_dark = float(rad.min())
    cell = float(r_light)
    gh, gw = int(H / cell) + 2, int(W / cell) + 2
    grid = -np.ones((gh, gw, 4), dtype=np.int32)   # up to 4 points per cell
    gcount = np.zeros((gh, gw), dtype=np.int8)

    n_cand = int(oversample * H * W / (math.pi * max(r_dark, 0.5) ** 2))
    n_cand = min(n_cand, 3_000_000)
    cy = rng.uniform(0, H - 1, n_cand).astype(np.float32)
    cx = rng.uniform(0, W - 1, n_cand).astype(np.float32)
    # reject early by tone: skip candidates in near-white areas probabilistically.
    # tone == 0 is paper white and gets nothing at all - without that hard floor
    # the +0.02 would keep sprinkling dots across an empty sky.
    tv = tone[cy.astype(int), cx.astype(int)]
    keep = (tv > 0) & (rng.random(n_cand) < np.clip(tv * 1.6 + 0.02, 0, 1))
    cy, cx = cy[keep], cx[keep]

    px = np.empty(len(cy), np.float32)
    py = np.empty(len(cy), np.float32)
    n = 0
    reach = 2
    for i in range(len(cy)):
        y, x = cy[i], cx[i]
        r = rad[int(y), int(x)]
        gy_, gx_ = int(y / cell), int(x / cell)
        ok = True
        for ddy in range(-reach, reach + 1):
            yy = gy_ + ddy
            if yy < 0 or yy >= gh:
                continue
            for ddx in range(-reach, reach + 1):
                xx = gx_ + ddx
                if xx < 0 or xx >= gw:
                    continue
                for k in range(gcount[yy, xx]):
                    j = grid[yy, xx, k]
                    dx = px[j] - x
                    dy = py[j] - y
                    if dx * dx + dy * dy < r * r:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            px[n], py[n] = x, y
            if gcount[gy_, gx_] < 4:
                grid[gy_, gx_, gcount[gy_, gx_]] = n
                gcount[gy_, gx_] += 1
            n += 1
    return px[:n], py[:n]


# ----------------------------------------------------------------- tracing
def trace(seeds_x, seeds_y, tx, ty, tone, coh, steps, step_len, min_tone):
    """Vectorised RK2 streamline tracing, all seeds advanced in lockstep.

    The flow is a *line* field, so at each step the sampled direction is
    sign-aligned with the previous one; without that, strokes reverse on
    themselves at random."""
    H, W = tone.shape
    N = len(seeds_x)

    def samp(arr, x, y):
        return map_coordinates(arr, [y, x], order=1, mode="nearest")

    paths_f, paths_b = [], []
    for sign in (1.0, -1.0):
        x = seeds_x.copy()
        y = seeds_y.copy()
        alive = np.ones(N, bool)
        prev_dx = np.zeros(N, np.float32)
        prev_dy = np.zeros(N, np.float32)
        out = [np.stack([x, y], 1).copy()]
        for s in range(steps):
            dx = samp(tx, x, y)
            dy = samp(ty, x, y)
            if s == 0:
                dx, dy = dx * sign, dy * sign
            else:
                flip = (dx * prev_dx + dy * prev_dy) < 0
                dx = np.where(flip, -dx, dx)
                dy = np.where(flip, -dy, dy)
            # RK2 midpoint
            mx = x + dx * step_len * 0.5
            my = y + dy * step_len * 0.5
            dx2 = samp(tx, mx, my)
            dy2 = samp(ty, mx, my)
            flip = (dx2 * dx + dy2 * dy) < 0
            dx2 = np.where(flip, -dx2, dx2)
            dy2 = np.where(flip, -dy2, dy2)
            nx = x + dx2 * step_len
            ny = y + dy2 * step_len
            prev_dx, prev_dy = dx2, dy2
            inside = (nx >= 0) & (nx < W - 1) & (ny >= 0) & (ny < H - 1)
            dark = samp(tone, np.clip(nx, 0, W - 1), np.clip(ny, 0, H - 1)) > min_tone
            alive = alive & inside & dark
            x = np.where(alive, nx, x)
            y = np.where(alive, ny, y)
            out.append(np.stack([x, y], 1).copy())
        (paths_f if sign > 0 else paths_b).append(np.stack(out, 0))  # (steps+1, N, 2)
    return paths_f[0], paths_b[0]


# ----------------------------------------------------------------- layers
def layer(tone, tx, ty, coh, rng, args, pen_px, r_min, r_max, dots=True):
    """one seeding + tracing pass over a tone field -> (strokes, ndots).

    Called twice: once for the flow-following base layer, once more at an angle
    for cross-hatching, with a tone field that is zero outside the dark band.
    """
    tl = np.clip((tone - args.dot_below) / max(1e-6, 1 - args.dot_below), 0, 1)
    L_px = (tl ** args.len_gamma) * args.max_steps * args.step * 2.0
    rad = coverage_radius(tone, L_px, pen_px, args.max_coverage, r_min, r_max)
    sx, sy = poisson_seeds(tone, rad, r_max, rng)
    if len(sx) == 0:
        return [], 0

    Ls = L_px[sy.astype(int), sx.astype(int)]
    half = np.clip(Ls / (2.0 * args.step), 0, args.max_steps)
    F, B = trace(sx, sy, tx, ty, tone, coh, int(args.max_steps), args.step, args.min_tone)

    strokes, ndots, dotr = [], 0, args.dot_size
    for i in range(len(sx)):
        k = int(round(half[i]))
        if k < 1:
            # stipple: a dot, drawn as a minimal mark the pen can make
            if dots:
                strokes.append(np.array([[sx[i] - dotr, sy[i]], [sx[i] + dotr, sy[i]]]))
                ndots += 1
            continue
        b = B[1:k + 1, i][::-1]
        f = F[:k + 1, i]
        p = np.vstack([b, f])
        d = np.hypot(*np.diff(p, axis=0).T).sum()
        if d < args.dot_size:
            if dots:
                strokes.append(np.array([[sx[i] - dotr, sy[i]], [sx[i] + dotr, sy[i]]]))
                ndots += 1
        else:
            strokes.append(p)
    return strokes, ndots


# ----------------------------------------------------------------- main
def run(args):
    rng = np.random.default_rng(args.seed)
    tone = load_tone(args.image, args.max_px, args.contrast, args.gamma, args.invert,
                     args.white_point)
    H, W = tone.shape
    blank = float((tone <= 0).mean())
    print(f"  tone map {W}x{H}, mean darkness {tone.mean():.3f}, {blank*100:.1f}% blank")

    tx, ty, coh = edge_tangent_flow(tone, args.flow_blur)

    # in low-coherence (flat) regions, fall back to a fixed angle so hatching
    # doesn't wander aimlessly in skies and walls
    a = math.radians(args.flat_angle)
    w = np.clip(coh / (args.coh_floor + 1e-9), 0, 1)[..., None]
    fx, fy = math.cos(a), math.sin(a)
    tx = (w[..., 0] * tx + (1 - w[..., 0]) * fx).astype(np.float32)
    ty = (w[..., 0] * ty + (1 - w[..., 0]) * fy).astype(np.float32)
    nrm = np.hypot(tx, ty) + 1e-9
    tx, ty = tx / nrm, ty / nrm

    scale = args.width / W                      # mm per px
    # two widths: --stroke is what gets drawn, --pack-width is what the spacing
    # maths assumes each stroke inks over. they are the same unless you say
    # otherwise, which lets you draw a hairline at full plotted density, or the
    # reverse - a fat pen laid out sparsely.
    pen_px = (args.pack_width or args.stroke) / scale
    r_max = args.spacing * args.detail
    r_min = max(args.min_spacing, pen_px * 0.9)
    print(f"  draw {args.stroke / scale:.2f}px  packing {pen_px:.2f}px")

    strokes, ndots = layer(tone, tx, ty, coh, rng, args, pen_px, r_min, r_max)
    if not strokes:
        sys.exit("no seeds; try --contrast up, --spacing down or --white-point down")
    nbase = len(strokes)
    print(f"  base {nbase}  ({ndots} dots, {nbase-ndots} hatch)")

    # cross-hatch: a second pass at an angle to the flow, over the dark band only.
    # its tone field is zero outside the band, so seeding stops at the edge; no
    # dots, because stipple inside a solid shadow just reads as noise.
    if args.cross_above < 1.0:
        ca = args.cross_above
        t2 = np.clip((tone - ca) / max(1e-6, 1.0 - ca), 0, 1).astype(np.float32)
        a2 = math.radians(args.cross_angle)
        c2, s2 = math.cos(a2), math.sin(a2)
        cx = (tx * c2 - ty * s2).astype(np.float32)
        cy = (tx * s2 + ty * c2).astype(np.float32)
        xs, _ = layer(t2, cx, cy, coh, rng, args, pen_px, r_min, r_max, dots=False)
        strokes += xs
        print(f"  cross {len(xs)} at {args.cross_angle:g}deg above tone {ca:g}")

    print(f"  strokes {len(strokes)}  ({ndots} dots, {len(strokes)-ndots} hatch)")

    hmm = H * scale
    parts = []
    for p in strokes:
        q = p * scale
        d = "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in q)
        parts.append(f'<path d="{d}"/>')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{args.width}mm" '
           f'height="{hmm:.2f}mm" viewBox="0 0 {args.width} {hmm:.2f}">'
           f'<g fill="none" stroke="#000" stroke-width="{args.stroke}" '
           f'stroke-linecap="round">' + "".join(parts) + "</g></svg>")
    out = args.output or os.path.splitext(args.image)[0] + "-engraved.svg"
    open(out, "w").write(svg)
    print(f"  -> {out}  ({args.width} x {hmm:.1f} mm)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("-o", "--output")
    ap.add_argument("--width", type=float, default=200.0, help="page width mm")
    ap.add_argument("--max-px", type=int, default=900, help="working resolution")
    ap.add_argument("--spacing", type=float, default=3.2,
                    help="base seed spacing in px (bigger = sparser)")
    ap.add_argument("--detail", type=float, default=1.0,
                    help="scales spacing; <1 = finer, denser art")
    ap.add_argument("--max-coverage", type=float, default=0.82,
                    help="ink coverage at pure black (1.0 = solid fill)")
    ap.add_argument("--min-spacing", type=float, default=0.8,
                    help="hard floor on seed spacing, px")
    ap.add_argument("--contrast", type=float, default=1.15)
    ap.add_argument("--gamma", type=float, default=1.0,
                    help=">1 lightens midtones, <1 darkens")
    ap.add_argument("--white-point", type=float, default=0.0,
                    help="tone below this gets no marks at all, giving true paper white")
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--flow-blur", type=float, default=4.0,
                    help="structure-tensor smoothing; larger = smoother, calmer flow")
    ap.add_argument("--flat-angle", type=float, default=25.0,
                    help="hatch angle in featureless regions, degrees")
    ap.add_argument("--coh-floor", type=float, default=0.35,
                    help="anisotropy below which the flat angle takes over")
    ap.add_argument("--max-steps", type=int, default=14, help="max stroke half-length")
    ap.add_argument("--step", type=float, default=1.1, help="trace step, px")
    ap.add_argument("--min-tone", type=float, default=0.06,
                    help="strokes stop when they wander into lighter area")
    ap.add_argument("--dot-below", type=float, default=0.22,
                    help="tone below this becomes stipple instead of hatch")
    ap.add_argument("--len-gamma", type=float, default=1.3)
    ap.add_argument("--cross-above", type=float, default=1.0,
                    help="tone above this also gets a cross-hatch pass; 1.0 = off")
    ap.add_argument("--cross-angle", type=float, default=55.0,
                    help="cross-hatch angle relative to the flow, degrees")
    ap.add_argument("--dot-size", type=float, default=0.35, help="px")
    ap.add_argument("--stroke", type=float, default=0.3,
                    help="drawn stroke width, mm")
    ap.add_argument("--pack-width", type=float, default=0.0,
                    help="stroke width the spacing assumes, mm; 0 follows --stroke")
    ap.add_argument("--seed", type=int, default=0)
    run(ap.parse_args())


if __name__ == "__main__":
    main()
