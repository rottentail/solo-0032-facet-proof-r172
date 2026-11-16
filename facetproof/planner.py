from .algebra import point, face_normal, unit, sub, dot
from .camera import frustum_planes, mapping_active, normalized, project, view_point
from .clipping import clip_triangle
from .errors import require
from .mapping import analyze, subdivide
from .shading import illuminate
from .values import integer, digest, scalar
from .pigments import srgb_to_linear

RENDERER_REVISION = "facet-raster-2"


def validate_request(data):
    width = integer(data.get("width", 256), "width", 16, 512)
    height = integer(data.get("height", 256), "height", 16, 512)
    samples = integer(data.get("samples", 1), "samples", 1, 2)
    tolerance = scalar(data.get("mapping_tolerance", .5), "mapping_tolerance", .1, 8)
    return {"width": width, "height": height, "samples": samples,
            "mapping_tolerance": tolerance, "renderer": RENDERER_REVISION}


def build_plan(seal, request):
    lens = seal["lens"]
    samples = request["samples"]
    active = mapping_active(lens)
    mapping = analyze(lens, request["width"], request["height"],
                      request["mapping_tolerance"]) if active else {"active": False}
    frame = mapping["frame"] if active else {
        "x": 0, "y": 0, "width": request["width"], "height": request["height"]}
    full_width = request["width"] * samples
    full_height = request["height"] * samples
    width, height = frame["width"] * samples, frame["height"] * samples
    origin = (frame["x"] * samples, frame["y"] * samples)
    planes = frustum_planes(lens, full_width, full_height)
    triangles = []
    objects = {}
    total_faces = sum(len(item["mesh"]["faces"]) for item in seal["closure"])
    require(total_faces <= 40000, "capacity", "Exposure exceeds 40000 source faces", 422)
    distort = active and any(lens["distortion"].values())
    budget = [0]
    tolerance = mapping["tolerance"] * samples if distort else None
    sorted_items = sorted(seal["closure"], key=lambda item: item["placement"]["key"])
    for object_id, item in enumerate(sorted_items, 1):
        placement, mesh, pigment = item["placement"], item["mesh"], item["pigment"]
        objects[str(object_id)] = {"key": placement["key"], "mesh_id": placement["mesh_id"],
                                   "pigment_id": placement["pigment_id"]}
        vertices = [point(placement["matrix"], v) for v in mesh["vertices"]]
        for face_number, face in enumerate(mesh["faces"]):
            world = [vertices[i] for i in face]
            normal = face_normal(world)
            center = [sum(v[a] for v in world) / 3 for a in range(3)]
            direction = sub(lens["eye"], center)
            if dot(direction, direction) < 1e-12:
                continue
            view_direction = unit(direction) if lens["projection"] == "perspective" else [-v for v in lens["forward"]]
            if not pigment["two_sided"] and dot(normal, view_direction) <= 0:
                continue
            color = illuminate(normal, pigment, seal["illumination"], view_direction)
            camera_vertices = [view_point(lens, v) for v in world]
            for clipped in clip_triangle(camera_vertices, planes):
                if distort:
                    uvz = [normalized(lens, v) for v in clipped]
                    for piece in subdivide(uvz, lens, lens["projection"], full_width,
                                           full_height, origin, tolerance, budget):
                        triangles.append({"vertices": piece, "color": color,
                                          "object_id": object_id, "face": face_number})
                else:
                    triangles.append({"vertices": [project(lens, v, full_width, full_height, origin) for v in clipped],
                                      "color": color, "object_id": object_id, "face": face_number})
    valid_mask = None
    if active and mapping["radius_pixels"] is not None:
        valid_mask = {"center": [(mapping["center"][0] - frame["x"]) * samples,
                                 (mapping["center"][1] - frame["y"]) * samples],
                      "radius": mapping["radius_pixels"] * samples}
    return {"width": width, "height": height, "triangles": triangles, "objects": objects,
            "projection": lens["projection"], "background": [srgb_to_linear(c) for c in seal["illumination"]["background"]],
            "lens_hash": digest(lens), "source_faces": total_faces,
            "frame": frame, "mapping": mapping, "valid_mask": valid_mask}
