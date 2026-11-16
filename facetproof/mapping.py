"""Calibrated lens mapping: validity analysis, framing and bounded subdivision.

A calibrated lens adds a decentered principal point and a radial distortion
polynomial to the symmetric perspective/orthographic models. Distortion acts
on isotropic normalized image coordinates (half image height = 1 unit):

    L(r)  = 1 + k1 r^2 + k2 r^4 + k3 r^6          (radial scale)
    rd(r) = r * L(r)                              (distorted image radius)

The forward map is injective only while rd keeps growing and L stays
positive. The first radius where either fails is the fold radius; beyond it
one imaging position names several source directions, or none at all. The
valid imaging range is the disk rd([0, r_fold)) around the principal point.
Pixels outside it are never filled from geometry: they carry the reserved
INVALID_ID identity, positive-infinite depth and a reserved fill color, so a
filled edge can never masquerade as a real surface.

Curved triangle images are approximated by adaptive subdivision whose error
is bounded by the caller-declared tolerance and whose count is bounded by
MAX_PLANNED_TRIANGLES. Exceeding the budget rejects the exposure with an
explainable reason instead of letting the workload grow without bound.
"""
import math

from .camera import distort, to_pixel
from .errors import require

INVALID_ID = 0xFFFFFFFF
INVALID_FILL_LINEAR = (1., 0., 1.)
INVALID_FILL_SRGB = (255, 0, 255)
INVALID_PREVIEW_SRGB = (255, 255, 255)
MAX_PLANNED_TRIANGLES = 240000
MAX_SUBDIVISION_DEPTH = 8
MIN_FRAME_SIDE = 16


def _poly(coeffs, s):
    c0, c1, c2, c3 = coeffs
    return c0 + s * (c1 + s * (c2 + s * c3))


def _bisect_root(coeffs, low, high):
    # _poly(low) > 0 and _poly(high) <= 0; isolate the first crossing.
    for _ in range(200):
        mid = (low + high) / 2
        if mid == low or mid == high:
            break
        if _poly(coeffs, mid) > 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _first_positive_root(coeffs):
    """Smallest s > 0 where the cubic p(s) with p(0) = 1 first reaches zero."""
    _, c1, c2, c3 = coeffs
    critical = []
    a, b, c = 3 * c3, 2 * c2, c1
    if a == 0:
        if b != 0:
            root = -c / b
            if root > 0:
                critical.append(root)
    else:
        discriminant = b * b - 4 * a * c
        if discriminant >= 0:
            square = math.sqrt(discriminant)
            for candidate in ((-b + square) / (2 * a), (-b - square) / (2 * a)):
                if candidate > 0:
                    critical.append(candidate)
    critical.sort()
    points = [0.] + critical
    for index in range(1, len(points)):
        if _poly(coeffs, points[index]) <= 0:
            return _bisect_root(coeffs, points[index - 1], points[index])
    leading = c3 if c3 else (c2 if c2 else c1)
    if leading < 0:
        low = points[-1]
        high = 1. if low <= 0 else low * 2
        while _poly(coeffs, high) > 0:
            high *= 2
            if high > 1e15:
                return None
        return _bisect_root(coeffs, low, high)
    return None


def valid_radius(k1, k2, k3):
    """Distorted-image radius inside which every position has exactly one
    source direction, or None when the map never folds within sane bounds."""
    if not (k1 or k2 or k3):
        return None
    fold = None
    for coeffs in ((1., 3 * k1, 5 * k2, 7 * k3), (1., k1, k2, k3)):
        root = _first_positive_root(coeffs)
        if root is not None and (fold is None or root < fold):
            fold = root
    if fold is None:
        return None
    factor = 1 + (k1 + (k2 + k3 * fold) * fold) * fold
    return math.sqrt(fold) * max(0., factor)


def analyze(lens, req_width, req_height, tolerance):
    """Explain one calibrated mapping: valid range, center and chosen frame.

    The result is a pure function of the frozen lens and the exposure
    request, so the plate, the tile fingerprint and the offline reader all
    reproduce the same numbers. Rejections name the reason explicitly.
    """
    distortion = lens["distortion"]
    k1, k2, k3 = distortion["k1"], distortion["k2"], distortion["k3"]
    px, py = lens["principal_point"]
    framing = lens["framing"]
    radius = valid_radius(k1, k2, k3)
    center = [req_width / 2 + px * req_height / 2, req_height / 2 - py * req_height / 2]
    radius_px = None if radius is None else radius * req_height / 2
    if framing == "valid" and radius_px is not None:
        x0 = max(0, math.floor(center[0] - radius_px))
        y0 = max(0, math.floor(center[1] - radius_px))
        x1 = min(req_width, math.ceil(center[0] + radius_px))
        y1 = min(req_height, math.ceil(center[1] + radius_px))
        require(x1 - x0 >= MIN_FRAME_SIDE and y1 - y0 >= MIN_FRAME_SIDE,
                "valid_range",
                "Valid imaging area is smaller than the 16 pixel minimum plate extent", 422)
        frame = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    else:
        frame = {"x": 0, "y": 0, "width": req_width, "height": req_height}
        if radius_px is not None:
            near_x = min(max(center[0], 0), req_width)
            near_y = min(max(center[1], 0), req_height)
            require((near_x - center[0]) ** 2 + (near_y - center[1]) ** 2 < radius_px ** 2,
                    "valid_range",
                    "Valid imaging area lies entirely outside the requested frame", 422)
    return {"active": True, "framing": framing, "principal_point": [px, py],
            "distortion": {"k1": k1, "k2": k2, "k3": k3},
            "valid_radius": radius, "center": center, "radius_pixels": radius_px,
            "frame": frame, "requested": {"width": req_width, "height": req_height},
            "tolerance": tolerance, "folded": radius is not None}


def subdivide(uvz, lens, projection, width, height, origin, tolerance, budget):
    """Approximate the curved image of one camera-space triangle.

    Subdivision refines the undistorted image plane (where perspective and
    orthographic projections are exactly linear), so only true distortion
    curvature triggers splits. Returns projected [x, y, depth] vertex
    triples. `budget` is a shared one-element counter bounding the total
    planned triangles of the whole exposure.
    """
    pieces = []
    _split(list(uvz), lens, projection, width, height, origin, tolerance, 0, pieces, budget)
    return pieces


def _vertex(lens, u, v, z, width, height, origin):
    du, dv = distort(lens, u, v)
    px, py = to_pixel(du, dv, width, height, origin)
    return [px, py, z]


def _split(uvz, lens, projection, width, height, origin, tolerance, depth, pieces, budget):
    projected = [_vertex(lens, u, v, z, width, height, origin) for u, v, z in uvz]
    if depth < MAX_SUBDIVISION_DEPTH:
        mids = []
        for i, j in ((0, 1), (1, 2), (2, 0)):
            a, b = uvz[i], uvz[j]
            if projection == "perspective":
                z = 2 / (1 / a[2] + 1 / b[2])
            else:
                z = (a[2] + b[2]) / 2
            mids.append([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, z])
        split = False
        for (i, j), mid in zip(((0, 1), (1, 2), (2, 0)), mids):
            true = _vertex(lens, mid[0], mid[1], mid[2], width, height, origin)
            linear_x = (projected[i][0] + projected[j][0]) / 2
            linear_y = (projected[i][1] + projected[j][1]) / 2
            if (true[0] - linear_x) ** 2 + (true[1] - linear_y) ** 2 > tolerance ** 2:
                split = True
                break
        if split:
            m01, m12, m20 = mids
            a, b, c = uvz
            _split([a, m01, m20], lens, projection, width, height, origin, tolerance, depth + 1, pieces, budget)
            _split([m01, b, m12], lens, projection, width, height, origin, tolerance, depth + 1, pieces, budget)
            _split([m20, m12, c], lens, projection, width, height, origin, tolerance, depth + 1, pieces, budget)
            _split([m01, m12, m20], lens, projection, width, height, origin, tolerance, depth + 1, pieces, budget)
            return
    pieces.append(projected)
    budget[0] += 1
    require(budget[0] <= MAX_PLANNED_TRIANGLES, "mapping_budget",
            "Lens mapping exceeds the 240000 triangle planning budget; "
            "increase mapping_tolerance or reduce the canvas or face count", 422)
