import math
import struct
from .errors import require
from .mapping import INVALID_ID
from .values import identity, scalar


class Comparisons:
    def __init__(self, db, plates, blobs):
        self.db, self.plates, self.blobs = db, plates, blobs

    def create(self, data):
        tolerance = scalar(data.get("tolerance", 2), "tolerance", 0, 255)
        with self.db.unit_of_work() as conn:
            left = self.plates._get(conn, data.get("left"))
            right = self.plates._get(conn, data.get("right"))
            a, b = left["payload"], right["payload"]
            require((a["width"], a["height"], a["lens_hash"]) == (b["width"], b["height"], b["lens_hash"]),
                    "alignment", "Plate extent and lens must match", 409)
            require(a.get("mapping") == b.get("mapping"),
                    "alignment", "Plate lens mapping must match", 409)
            read = lambda p, n: self.blobs.read(p["passes"][n]["sha256"])
            ca, cb = read(a, "color.rgb"), read(b, "color.rgb")
            da, db = read(a, "depth.f32"), read(b, "depth.f32")
            ia, ib = read(a, "object.u32"), read(b, "object.u32")
            deltas = [abs(x-y) for x, y in zip(ca, cb)]
            changed = sum(max(deltas[i:i+3]) > tolerance for i in range(0, len(deltas), 3))
            silhouette = depth_changed = invalid_changed = 0
            maximum_depth = 0.
            for offset in range(0, len(da), 4):
                va, vb = struct.unpack_from("<f", da, offset)[0], struct.unpack_from("<f", db, offset)[0]
                silhouette += math.isfinite(va) != math.isfinite(vb)
                if math.isfinite(va) and math.isfinite(vb):
                    delta = abs(va-vb)
                    maximum_depth = max(maximum_depth, delta)
                    depth_changed += delta > 1e-5
                invalid_changed += ((struct.unpack_from("<I", ia, offset)[0] == INVALID_ID) !=
                                    (struct.unpack_from("<I", ib, offset)[0] == INVALID_ID))
            result = {"left": left["id"], "right": right["id"], "tolerance": tolerance,
                      "pixels": a["width"]*a["height"], "color_changed": changed,
                      "mean_channel_delta": sum(deltas)/len(deltas),
                      "silhouette_changed": silhouette, "depth_changed": depth_changed,
                      "invalid_changed": invalid_changed,
                      "maximum_depth_delta": maximum_depth,
                      "pass_hashes": {"left": a["visual_hash"], "right": b["visual_hash"]}}
            return self.db.insert(conn, identity("comparison"), "comparison", "computed", result)
