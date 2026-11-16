import math
from .algebra import cross, dot, sub, unit
from .errors import require
from .values import choice, scalar, vector, object_value

DISTORTION_LIMITS = {"k1": 4, "k2": 8, "k3": 8}
FRAMINGS = {"full", "valid"}


def validate_lens(data):
    eye = vector(data.get("eye", [4, 3, 5]), "eye", low=-10000, high=10000)
    target = vector(data.get("target", [0, 0, 0]), "target", low=-10000, high=10000)
    up = unit(vector(data.get("up", [0, 1, 0]), "up"))
    forward = unit(sub(target, eye))
    right = unit(cross(forward, up))
    true_up = unit(cross(right, forward))
    near = scalar(data.get("near", .1), "near", .001, 10000)
    far = scalar(data.get("far", 100), "far", .01, 100000)
    require(far > near, "lens", "Far plane must exceed near plane")
    principal = vector(data.get("principal_point", [0, 0]), "principal_point",
                       length=2, low=-4, high=4)
    distortion = object_value(data.get("distortion", {}), "distortion")
    radial = {name: scalar(distortion.get(name, 0), "distortion." + name, -limit, limit)
              for name, limit in DISTORTION_LIMITS.items()}
    return {"eye": eye, "target": target, "up": up, "right": right,
            "true_up": true_up, "forward": forward,
            "projection": choice(data.get("projection", "perspective"), "projection", {"perspective", "orthographic"}),
            "fov": scalar(data.get("fov", 45), "fov", 5, 150),
            "span": scalar(data.get("span", 5), "span", .01, 20000),
            "near": near, "far": far,
            "principal_point": principal,
            "distortion": radial,
            "framing": choice(data.get("framing", "full"), "framing", FRAMINGS)}


def mapping_active(lens):
    """True when the lens leaves the legacy symmetric pinhole/ortho model."""
    return (lens.get("framing", "full") != "full"
            or lens.get("principal_point", [0, 0]) != [0, 0]
            or any(lens.get("distortion", {}).values()))


def view_point(lens, world):
    v = sub(world, lens["eye"])
    return [dot(v, lens["right"]), dot(v, lens["true_up"]), dot(v, lens["forward"])]


def normalized(lens, vertex):
    """Isotropic pre-distortion image coordinates and camera-forward depth."""
    x, y, z = vertex
    if lens["projection"] == "perspective":
        half = math.tan(math.radians(lens["fov"]) / 2)
        return [x / (z * half), y / (z * half), z]
    half = lens["span"] / 2
    return [x / half, y / half, z]


def distort(lens, u, v):
    """Radial distortion around the principal point, then decentering."""
    radial = lens["distortion"]
    r2 = u * u + v * v
    factor = 1 + (radial["k1"] + (radial["k2"] + radial["k3"] * r2) * r2) * r2
    px, py = lens["principal_point"]
    return [u * factor + px, v * factor + py]


def to_pixel(du, dv, width, height, origin=(0, 0)):
    return [width / 2 + du * height / 2 - origin[0],
            height / 2 - dv * height / 2 - origin[1]]


def project(lens, vertex, width, height, origin=(0, 0)):
    x, y, z = vertex
    if not mapping_active(lens):
        if lens["projection"] == "perspective":
            half = math.tan(math.radians(lens["fov"]) / 2)
            nx, ny = x / (z * half * width / height), y / (z * half)
        else:
            nx, ny = x / (lens["span"] * width / height / 2), y / (lens["span"] / 2)
        return [(nx + 1) * width / 2 - origin[0], (1 - ny) * height / 2 - origin[1], z]
    u, v, _ = normalized(lens, vertex)
    du, dv = distort(lens, u, v)
    px, py = to_pixel(du, dv, width, height, origin)
    return [px, py, z]


def frustum_planes(lens, width, height):
    planes = [(0, 0, 1, -lens["near"]), (0, 0, -1, lens["far"])]
    if mapping_active(lens):
        # Lateral bounds of a calibrated mapping are enforced by the canvas
        # and the valid imaging disk, not by symmetric frustum planes.
        return planes
    if lens["projection"] == "perspective":
        half_y = math.tan(math.radians(lens["fov"]) / 2)
        half_x = half_y * width / height
        planes += [(1, 0, half_x, 0), (-1, 0, half_x, 0),
                   (0, 1, half_y, 0), (0, -1, half_y, 0)]
    else:
        half_y = lens["span"] / 2
        half_x = half_y * width / height
        planes += [(1, 0, 0, half_x), (-1, 0, 0, half_x),
                   (0, 1, 0, half_y), (0, -1, 0, half_y)]
    return planes
