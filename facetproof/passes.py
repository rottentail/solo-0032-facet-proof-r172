import math
import struct
from .mapping import INVALID_FILL_SRGB, INVALID_ID, INVALID_PREVIEW_SRGB
from .pigments import linear_to_srgb, srgb_to_linear
from .png import encode_rgb


def serialize_passes(plan, rendered, request):
    width, height, samples = plan["frame"]["width"], plan["frame"]["height"], request["samples"]
    rgb = bytearray(width * height * 3)
    depth = bytearray(width * height * 4)
    ids = bytearray(width * height * 4)
    depth_rgb = bytearray(width * height * 3)
    object_rgb = bytearray(width * height * 3)
    coverage = 0
    invalid = 0
    for y in range(height):
        for x in range(width):
            offsets = [(y*samples+sy)*plan["width"] + x*samples+sx
                       for sy in range(samples) for sx in range(samples)]
            # A pixel is invalid only when no high-sample source exists at
            # all; mixed pixels keep their valid sources and stay honest.
            valid = [p for p in offsets if rendered["ids"][p] != INVALID_ID]
            output = y * width + x
            if valid:
                nearest = min(valid, key=lambda p: (rendered["depth"][p], p))
                obj_id = rendered["ids"][nearest]
                distance = rendered["depth"][nearest]
                for channel in range(3):
                    value = sum(rendered["colors"][p*3+channel] for p in valid) / len(valid)
                    rgb[output*3+channel] = round(linear_to_srgb(value) * 255)
            else:
                obj_id, distance = INVALID_ID, math.inf
                invalid += 1
                for channel in range(3):
                    rgb[output*3+channel] = INVALID_FILL_SRGB[channel]
            struct.pack_into("<f", depth, output*4, distance)
            struct.pack_into("<I", ids, output*4, obj_id)
            if obj_id == INVALID_ID:
                object_rgb[output*3:output*3+3] = bytes(INVALID_PREVIEW_SRGB)
            else:
                for channel in range(3):
                    object_rgb[output*3+channel] = ((obj_id * (73, 151, 199)[channel]) % 255) if obj_id else 0
            shade = round(255 / (1 + distance)) if math.isfinite(distance) else 0
            for channel in range(3):
                depth_rgb[output*3+channel] = shade
            coverage += obj_id not in (0, INVALID_ID)
    return {"color.rgb": bytes(rgb), "depth.f32": bytes(depth), "object.u32": bytes(ids),
            "color.png": encode_rgb(width, height, bytes(rgb)),
            "depth.png": encode_rgb(width, height, bytes(depth_rgb)),
            "object.png": encode_rgb(width, height, bytes(object_rgb))}, {
                "covered_pixels": coverage, "invalid_pixels": invalid,
                "triangles": rendered["triangles"],
                "fragments": rendered["fragments"], "background_depth": "positive_infinity",
                "depth_units": "camera_forward_distance", "byte_order": "little"}
