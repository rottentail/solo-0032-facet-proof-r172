"""Version-one and version-two plate semantics, independent of the producer's Python code."""
import hashlib
import json
import math
import re
import struct
from .archive import CHANNELS, MAX_SIDE, read_members
from .errors import InvalidBundle, require
from .mapping import expected_mapping
from .png import decode_rgb

TRUST = {
    "verified": "Internal byte integrity and metadata/channel consistency only",
    "external_authenticity": "Not established: an attacker can replace content and all bundle-local hashes",
    "review_validity": "Unknown: the ZIP contains no proof records or live quarantine state",
    "render_correctness": "Not established: channels are decoded and cross-checked, not independently re-rendered",
}

INVALID_ID = 0xFFFFFFFF
INVALID_FILL_SRGB = (255, 0, 255)
INVALID_PREVIEW_SRGB = (255, 255, 255)


def _object(value, field):
    require(isinstance(value, dict), "metadata_shape", field + " must be an object")
    return value


def _integer(value, low, high, field):
    require(type(value) is int and low <= value <= high, "metadata_value", field + " is out of range")
    return value


def _text(value, field):
    require(isinstance(value, str) and bool(value), "metadata_value", field + " must be text")
    return value


def _checksum(value, field):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "metadata_hash", field + " must be a lowercase SHA-256")
    return value


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise InvalidBundle("metadata_json", "Metadata is not finite canonical JSON") from exc


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _json(raw, field):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "metadata_json", "Duplicate JSON object key")
            value[key] = item
        return value
    def constant(_):
        raise InvalidBundle("metadata_json", "Non-finite JSON constant")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        canonical(value)  # Also catches overflowing exponents and bad surrogates.
    except InvalidBundle:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidBundle("metadata_json", "Invalid " + field) from exc
    return _object(value, field)


def _objects(seal, objects):
    closure = seal.get("closure")
    require(isinstance(closure, list) and 1 <= len(closure) <= 80,
            "closure", "Expected a nonempty frozen placement closure")
    by_key = {}
    for entry in closure:
        entry = _object(entry, "closure entry")
        placement = _object(entry.get("placement"), "placement")
        key = _text(placement.get("key"), "placement.key")
        require(key not in by_key, "closure", "Frozen placement keys must be unique")
        require(placement.get("visible") is True, "closure", "Frozen closure must contain visible placements")
        _object(entry.get("mesh"), "frozen mesh")
        _object(entry.get("pigment"), "frozen pigment")
        for field in ("mesh_revision", "pigment_revision"):
            _integer(entry.get(field), 1, 2**63-2, field)
        by_key[key] = {"key": key,
                       "mesh_id": _text(placement.get("mesh_id"), "placement.mesh_id"),
                       "pigment_id": _text(placement.get("pigment_id"), "placement.pigment_id")}
    expected = {str(i): by_key[key] for i, key in enumerate(sorted(by_key), 1)}
    require(_object(objects, "objects") == expected, "object_mapping", "Object mapping disagrees with the frozen closure")
    return expected


def _close(actual, expected, field):
    require(isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and math.isfinite(actual) and abs(actual - expected) <= 1e-9,
            "mapping", field + " does not reproduce the frozen lens mapping")


def _check_mapping(mapping, expected):
    require(mapping.get("active") is True, "mapping", "Calibrated mapping must be active")
    for field in ("framing", "folded", "principal_point", "distortion", "requested", "frame"):
        require(mapping.get(field) == expected[field],
                "mapping", field + " disagrees with the frozen lens and request")
    _close(mapping.get("tolerance"), expected["tolerance"], "tolerance")
    center = mapping.get("center")
    require(isinstance(center, list) and len(center) == 2, "mapping", "center must be a pair")
    _close(center[0], expected["center"][0], "center")
    _close(center[1], expected["center"][1], "center")
    if expected["valid_radius"] is None:
        require(mapping.get("valid_radius") is None and mapping.get("radius_pixels") is None,
                "mapping", "Unfolded mapping must declare an unbounded valid radius")
    else:
        _close(mapping.get("valid_radius"), expected["valid_radius"], "valid_radius")
        _close(mapping.get("radius_pixels"), expected["radius_pixels"], "radius_pixels")


def evaluate(raw):
    members = read_members(raw)
    plate = _json(members["plate.json"], "plate.json")
    seal = _json(members["seal.json"], "seal.json")
    payload = _object(plate.get("payload"), "plate.payload")
    frozen = _object(seal.get("payload"), "seal.payload")
    plate_format = payload.get("format")
    require(plate_format in {"facet-plate-1", "facet-plate-2"} and frozen.get("format") == "facet-seal-1",
            "format", "Unsupported plate or seal format")
    calibrated = plate_format == "facet-plate-2"
    require(plate.get("state") == "ready" and seal.get("kind") == "seal"
            and seal.get("state") == "frozen", "metadata_state", "Unexpected embedded plate or seal state")
    plate_id = _text(plate.get("id"), "plate.id")
    exposure_id = _text(plate.get("exposure_id"), "plate.exposure_id")
    seal_id = _text(seal.get("id"), "seal.id")
    require(payload.get("seal_id") == seal_id, "seal_id", "Plate refers to another seal")
    content_hash = _checksum(frozen.get("content_hash"), "content_hash")
    require(_digest({key: value for key, value in frozen.items() if key != "content_hash"}) == content_hash,
            "seal_hash", "Frozen canonical content checksum mismatch")
    require(_checksum(payload.get("seal_hash"), "seal_hash") == content_hash,
            "seal_hash", "Plate checksum does not name its frozen content")
    lens = _object(frozen.get("lens"), "lens")
    require(_checksum(payload.get("lens_hash"), "lens_hash") == _digest(lens),
            "lens_hash", "Plate lens checksum mismatch")
    width = _integer(payload.get("width"), 16, MAX_SIDE, "width")
    height = _integer(payload.get("height"), 16, MAX_SIDE, "height")
    request = _object(payload.get("request"), "request")
    require(type(request.get("width")) is int and 16 <= request["width"] <= MAX_SIDE
            and type(request.get("height")) is int and 16 <= request["height"] <= MAX_SIDE,
            "extent", "Request dimensions are out of range")
    if calibrated:
        mapping = _object(payload.get("mapping"), "mapping")
        _check_mapping(mapping, expected_mapping(lens, request))
        require(mapping["frame"]["width"] == width and mapping["frame"]["height"] == height,
                "extent", "Plate extent disagrees with its mapping frame")
    else:
        mapping = None
        require(payload.get("mapping") is None, "mapping",
                "Version-one plates carry no mapping report")
        require(request["width"] == width and request["height"] == height,
                "extent", "Request dimensions disagree with the plate")
    samples = _integer(request.get("samples"), 1, 2, "samples")
    _text(request.get("renderer"), "request.renderer")
    objects = _objects(frozen, payload.get("objects"))
    descriptors = _object(payload.get("passes"), "passes")
    require(set(descriptors) == CHANNELS, "channels", "Expected exactly the six version-one channels")
    for name in sorted(CHANNELS):
        descriptor = _object(descriptors[name], "descriptor")
        require(set(descriptor) == {"sha256", "size"}, "descriptor", "Unexpected descriptor fields")
        data = members["passes/" + name]
        size = _integer(descriptor.get("size"), 0, 64*1024*1024, "descriptor.size")
        require(size == len(data), "channel_size", "Channel size mismatch: " + name)
        require(_checksum(descriptor.get("sha256"), "descriptor.sha256") == hashlib.sha256(data).hexdigest(),
                "channel_hash", "Channel checksum mismatch: " + name)
        if not name.endswith(".png"):
            stride = 3 if name == "color.rgb" else 4
            require(size == width * height * stride, "extent", "Raw channel extent mismatch: " + name)
    require(_checksum(payload.get("visual_hash"), "visual_hash") == _digest(descriptors),
            "visual_hash", "Channel manifest checksum mismatch")
    decoded = {name: decode_rgb(members["passes/" + name], width, height)
               for name in ("color.png", "depth.png", "object.png")}
    color_raw = members["passes/color.rgb"]
    require(decoded["color.png"] == color_raw,
            "color_pixels", "PNG pixels disagree with the raw color channel")
    depths = struct.iter_unpack("<f", members["passes/depth.f32"])
    indices = struct.iter_unpack("<I", members["passes/object.u32"])
    covered = 0
    background = 0
    invalid = 0
    minimum = math.inf
    maximum = 0.
    picks = []
    sample_offsets = {0, width-1, (height//2)*width+width//2, width*height-1}
    first_foreground = False
    first_background = False
    first_invalid = False
    object_preview = decoded["object.png"]
    for offset, ((depth,), (index,)) in enumerate(zip(depths, indices)):
        if index == 0:
            require(depth == math.inf, "depth_background", "Background must have positive infinite depth")
            background += 1
            take = not first_background
            first_background = True
        elif calibrated and index == INVALID_ID:
            require(depth == math.inf, "depth_invalid",
                    "Invalid imaging pixels must have positive infinite depth")
            require(color_raw[offset*3:offset*3+3] == bytes(INVALID_FILL_SRGB),
                    "color_invalid", "Invalid imaging pixels must use the reserved fill color")
            invalid += 1
            take = not first_invalid
            first_invalid = True
        else:
            require(str(index) in objects, "object_index", "Pixel refers to an unknown object")
            require(math.isfinite(depth) and depth > 0,
                    "depth_value", "Foreground depth must be finite and positive")
            covered += 1
            minimum = min(minimum, depth)
            maximum = max(maximum, depth)
            take = not first_foreground
            first_foreground = True
        if index == INVALID_ID:
            expected_color = bytes(INVALID_PREVIEW_SRGB)
        else:
            expected_color = bytes(((index * factor) % 255) if index else 0 for factor in (73, 151, 199))
        require(object_preview[offset*3:offset*3+3] == expected_color,
                "object_preview", "Object preview disagrees with the identifier channel")
        if take or offset in sample_offsets:
            picks.append({"x": offset % width, "y": offset // width, "object_index": index,
                          "object": objects.get(str(index)), "depth": depth if index and index != INVALID_ID else None,
                          "valid_source": index != INVALID_ID, "seal_id": seal_id})
    statistics = _object(payload.get("statistics"), "statistics")
    require(statistics.get("byte_order") == "little"
            and statistics.get("background_depth") == "positive_infinity"
            and statistics.get("depth_units") == "camera_forward_distance",
            "channel_semantics", "Unexpected raw-channel interpretation")
    require(type(statistics.get("covered_pixels")) is int and statistics["covered_pixels"] == covered,
            "coverage", "Coverage count disagrees with the identifier channel")
    if calibrated:
        require(type(statistics.get("invalid_pixels")) is int and statistics["invalid_pixels"] == invalid,
                "coverage", "Invalid pixel count disagrees with the identifier channel")
    report = {
        "valid": True, "format": plate_format,
        "summary": {"width": width, "height": height, "samples": samples,
                    "covered_pixels": covered, "background_pixels": background,
                    "invalid_pixels": invalid,
                    "framing": mapping["framing"] if calibrated else "full",
                    "objects": len(objects), "finite_depth_range": [minimum, maximum] if covered else None,
                    "depth_units": "camera_forward_distance", "object_id_scope": "local_to_this_plate"},
        "provenance": {"plate_id": plate_id, "exposure_id": exposure_id, "seal_id": seal_id,
                       "seal_hash": content_hash, "lens_hash": payload["lens_hash"],
                       "visual_hash": payload["visual_hash"], "renderer": request["renderer"],
                       "mapping": mapping,
                       "assembly_id": _text(frozen.get("assembly_id"), "assembly_id"),
                       "assembly_revision": _integer(frozen.get("assembly_revision"), 1, 2**63-2, "assembly_revision"),
                       "objects": objects,
                       "frozen_resources": [
                           {"key": item["placement"]["key"],
                            "mesh_id": item["placement"]["mesh_id"], "mesh_revision": item["mesh_revision"],
                            "pigment_id": item["placement"]["pigment_id"], "pigment_revision": item["pigment_revision"]}
                           for item in sorted(frozen["closure"], key=lambda item: item["placement"]["key"])]},
        "channels": descriptors, "picks": picks, "trust": dict(TRUST),
    }
    return report, members
