"""Independent re-derivation of the producer's calibrated lens mapping.

This module is written from the mapping specification, not from the
producer's code: given the frozen lens and the exposure request it
recomputes the valid imaging radius, the distortion center and the frame a
calibrated plate must declare. Floats are compared with a 1e-9 absolute
tolerance; integers and enumerations must match exactly.

Specification: distortion acts on isotropic normalized image coordinates
(half image height = 1 unit) through L(r) = 1 + k1 r^2 + k2 r^4 + k3 r^6.
The map stays injective while rd(r) = r * L(r) keeps growing and L stays
positive; the valid imaging radius is rd at the first radius where either
fails. The imaging disk is centered at the principal point; framing "valid"
crops the canvas to the disk's bounding box, framing "full" keeps it.
"""
import math

from .errors import require

MIN_FRAME_SIDE = 16


def _poly(coeffs, s):
    c0, c1, c2, c3 = coeffs
    return c0 + s * (c1 + s * (c2 + s * c3))


def _bisect_root(coeffs, low, high):
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


def _number(value, field, low, high):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high,
            "mapping", field + " is not a bounded number")
    return float(value)


def expected_mapping(lens, request):
    """The mapping report a calibrated plate must declare, recomputed here."""
    principal = lens.get("principal_point", [0, 0])
    require(isinstance(principal, list) and len(principal) == 2,
            "mapping", "principal_point must be a pair")
    px = _number(principal[0], "principal_point", -4, 4)
    py = _number(principal[1], "principal_point", -4, 4)
    distortion = lens.get("distortion", {})
    require(isinstance(distortion, dict), "mapping", "distortion must be an object")
    k1 = _number(distortion.get("k1", 0), "distortion.k1", -4, 4)
    k2 = _number(distortion.get("k2", 0), "distortion.k2", -8, 8)
    k3 = _number(distortion.get("k3", 0), "distortion.k3", -8, 8)
    framing = lens.get("framing", "full")
    require(framing in {"full", "valid"}, "mapping", "framing must be full or valid")
    tolerance = _number(request.get("mapping_tolerance", .5), "mapping_tolerance", .1, 8)
    req_width, req_height = request["width"], request["height"]
    radius = valid_radius(k1, k2, k3)
    center = [req_width / 2 + px * req_height / 2, req_height / 2 - py * req_height / 2]
    radius_px = None if radius is None else radius * req_height / 2
    if framing == "valid" and radius_px is not None:
        x0 = max(0, math.floor(center[0] - radius_px))
        y0 = max(0, math.floor(center[1] - radius_px))
        x1 = min(req_width, math.ceil(center[0] + radius_px))
        y1 = min(req_height, math.ceil(center[1] + radius_px))
        require(x1 - x0 >= MIN_FRAME_SIDE and y1 - y0 >= MIN_FRAME_SIDE,
                "mapping", "Declared frame cannot satisfy the minimum plate extent")
        frame = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
    else:
        frame = {"x": 0, "y": 0, "width": req_width, "height": req_height}
    return {"active": True, "framing": framing, "principal_point": [px, py],
            "distortion": {"k1": k1, "k2": k2, "k3": k3},
            "valid_radius": radius, "center": center, "radius_pixels": radius_px,
            "frame": frame, "requested": {"width": req_width, "height": req_height},
            "tolerance": tolerance, "folded": radius is not None}
