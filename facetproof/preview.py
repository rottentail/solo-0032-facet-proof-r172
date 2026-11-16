"""Synchronous bounded previews while a calibration is being edited.

A preview renders the assembly's current draft lens and placements through
the same plan builder and rasterizer a formal exposure would use, but it
persists nothing: no exposure, no tiles, no plate, no capacity reservation.
The response always declares the actual mapping quality used, so a preview
approximation can never be confused with a formal plate, which records its
own frozen request quality and is checked independently offline.
"""
import base64
import time

from .errors import require
from .passes import serialize_passes
from .planner import RENDERER_REVISION, build_plan
from .raster import rasterize
from .values import integer, scalar

PREVIEW_SIDE = 192
PREVIEW_BUDGET_SECONDS = 15


class Previews:
    def __init__(self, db):
        self.db = db

    def create(self, assembly_id, data):
        width = integer(data.get("width", 96), "width", 16, PREVIEW_SIDE)
        height = integer(data.get("height", 96), "height", 16, PREVIEW_SIDE)
        tolerance = scalar(data.get("mapping_tolerance", 1.), "mapping_tolerance", .1, 8)
        started = time.monotonic()

        def checkpoint():
            require(time.monotonic() - started <= PREVIEW_BUDGET_SECONDS,
                    "preview_timeout", "Preview exceeded its 15 second budget", 422)

        with self.db.unit_of_work() as conn:
            obj = self.db.get(conn, assembly_id, "assembly")
            payload = obj["payload"]
            visible = [p for p in payload["placements"] if p["visible"]]
            require(bool(visible), "empty", "Preview requires visible geometry")
            closure = []
            for placement in visible:
                mesh = self.db.get(conn, placement["mesh_id"], "mesh")
                pigment = self.db.get(conn, placement["pigment_id"], "pigment")
                require(mesh["state"] == "usable", "retired", "Geometry retired before preview", 409)
                closure.append({"placement": placement, "mesh": mesh["payload"],
                                "pigment": pigment["payload"]})
        seal_like = {"lens": payload["lens"], "illumination": payload["illumination"],
                     "closure": closure}
        request = {"width": width, "height": height, "samples": 1,
                   "mapping_tolerance": tolerance, "renderer": RENDERER_REVISION}
        plan = build_plan(seal_like, request)
        checkpoint()
        rendered = rasterize(plan, checkpoint)
        passes, statistics = serialize_passes(plan, rendered, request)
        checkpoint()
        return {"format": "facet-preview-1", "approximate": True,
                "assembly_id": assembly_id, "assembly_revision": obj["revision"],
                "width": plan["frame"]["width"], "height": plan["frame"]["height"],
                "request": request,
                "mapping": plan["mapping"] if plan["mapping"]["active"] else None,
                "statistics": statistics,
                "note": "Preview only: not a plate. A formal exposure records and freezes its own mapping quality.",
                "png_base64": base64.b64encode(passes["color.png"]).decode("ascii")}
