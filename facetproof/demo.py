import argparse
import base64
import json
from pathlib import Path
import tempfile
import shutil
from .application import Application
from .errors import Fault, require
from .routing import route_request, PREFIX

FLOWS = ["mesh-intake", "mesh-retirement", "assembly-edit", "lens-edit", "seal-closure", "exposure-submit", "render-plates", "cancel-exposure", "retry-exposure", "verify-plate", "pixel-pick", "compare-plates", "proof-decision", "quarantine-plate", "recover-lease", "export-bundle", "reserve-capacity", "settle-capacity", "resize-capacity", "tile-progress", "offline-inspect", "backup-closure", "content-inspection", "lens-calibration", "valid-framing", "mapping-rejection", "mapping-preview"]

def _calibrated_scene(call, lens):
    mesh = call("POST", "meshes", {"name":"标定翼", "vertices":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,1.2]],
                                   "faces":[[0,1,4],[1,2,4],[2,3,4],[3,0,4],[0,3,2],[0,2,1]]})
    pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"标定说明图", "lens":lens})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[{"key":"wing", "mesh_id":mesh["id"], "pigment_id":pigment["id"], "transform":{"euler":[0,20,0]}}]})
    return assembly


def _grid_mesh(call, n=40):
    vertices = [[-1.5 + 3*i/n, -1.5 + 3*j/n, 0] for j in range(n+1) for i in range(n+1)]
    faces = []
    for j in range(n):
        for i in range(n):
            a = j*(n+1)+i
            faces.append([a, a+1, a+(n+2)])
            faces.append([a, a+(n+2), a+(n+1)])
    return call("POST", "meshes", {"name":"标定网格", "vertices":vertices, "faces":faces})


def _commission_mapping(app, call, rejected, flow, output):
    result = {"workflow": flow}
    if flow == "mapping-preview":
        assembly = _calibrated_scene(call, {"eye":[3,2,4], "principal_point":[0.35,-0.15],
                                            "distortion":{"k1":-0.9,"k2":0.15}})
        coarse = call("POST", "assemblies/"+assembly["id"]+"/preview",
                      {"width":96, "height":96, "mapping_tolerance":2})
        require(coarse["format"] == "facet-preview-1" and coarse["approximate"]
                and coarse["mapping"]["tolerance"] == 2 and coarse["statistics"]["invalid_pixels"] > 0,
                "commission", "Preview does not declare its approximate mapping", 500)
        fine = call("POST", "assemblies/"+assembly["id"]+"/preview",
                    {"width":96, "height":96, "mapping_tolerance":0.5})
        require(fine["statistics"]["triangles"] > coarse["statistics"]["triangles"],
                "commission", "Declared preview quality does not change the mapping", 500)
        from .png import inspect_png
        png = inspect_png(base64.b64decode(coarse["png_base64"]))
        require(png["width"] == 96 and png["height"] == 96, "commission", "Preview image extent mismatch", 500)
        require(call("GET", "resources")["reserved"] == 0 and not app.exposures.list(),
                "commission", "Preview reserved capacity or created exposures", 500)
        impossible = _calibrated_scene(call, {"eye":[3,2,4], "principal_point":[3.5,0], "distortion":{"k1":-4}})
        result["rejected"] = rejected("POST", "assemblies/"+impossible["id"]+"/preview", {}, "valid_range")
        result["preview"] = {"tolerance": coarse["mapping"]["tolerance"],
                             "triangles": coarse["statistics"]["triangles"],
                             "invalid_pixels": coarse["statistics"]["invalid_pixels"],
                             "approximate": coarse["approximate"]}
        return result
    if flow == "mapping-rejection":
        assembly = _calibrated_scene(call, {"eye":[3,2,4], "principal_point":[3.5,0], "distortion":{"k1":-4}})
        seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures",
                                      {"key":"impossible", "width":96, "height":96}, "valid_range")
        mesh = _grid_mesh(call)
        pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
        dense = call("POST", "assemblies", {"name":"预算说明图", "lens":{"eye":[0,0,4], "distortion":{"k1":-4,"k2":8,"k3":8}}})
        dense = call("PUT", "assemblies/"+dense["id"], {"revision":1, "placements":[{"key":"grid", "mesh_id":mesh["id"], "pigment_id":pigment["id"]}]})
        dense_seal = call("POST", "assemblies/"+dense["id"]+"/seal", {"revision":2})
        exposure = call("POST", "seals/"+dense_seal["id"]+"/exposures",
                        {"key":"budget", "width":256, "height":256, "samples":2, "mapping_tolerance":0.1})
        rendered = call("POST", "worker/step")
        require(rendered["state"] == "failed" and rendered["error"]["error"] == "mapping_budget",
                "commission", "Runaway mapping was not bounded", 500)
        require(call("GET", "exposures/"+exposure["id"])["state"] == "failed",
                "commission", "Bounded failure did not settle", 500)
        result["budget"] = rendered["error"]
        return result
    framing = "valid" if flow == "valid-framing" else "full"
    assembly = _calibrated_scene(call, {"eye":[3,2,4], "principal_point":[0.35,-0.15],
                                        "distortion":{"k1":-0.9,"k2":0.15}, "framing":framing})
    seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
    request = {"key":"calibrated", "width":96, "height":96, "samples":2}
    call("POST", "seals/"+seal["id"]+"/exposures", request)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    plate = rendered["plate"]
    payload = plate["payload"]
    mapping = payload["mapping"]
    statistics = payload["statistics"]
    require(payload["format"] == "facet-plate-2" and mapping["framing"] == framing and mapping["folded"],
            "commission", "Plate does not record its calibrated mapping", 500)
    require(statistics["invalid_pixels"] > 0 and statistics["covered_pixels"] > 0,
            "commission", "Calibrated plate lacks both valid and invalid content", 500)
    require((payload["width"], payload["height"]) == (mapping["frame"]["width"], mapping["frame"]["height"]),
            "commission", "Plate extent differs from its mapping frame", 500)
    if flow == "valid-framing":
        require(mapping["frame"]["width"] < request["width"], "commission", "Valid framing did not crop", 500)
    result["mapping"] = mapping
    result["statistics"] = statistics
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name in ("color.png", "depth.png", "object.png"):
            (Path(output)/name).write_bytes(call("GET", "plates/"+plate["id"]+"/passes/"+name)[0])
    corner = call("GET", "plates/"+plate["id"]+"/pick?x=0&y=0")
    require(corner["valid_source"] is False and corner["object"] is None
            and corner["reason"] == "outside_valid_imaging_range",
            "commission", "Invalid pixel names a part by accident", 500)
    result["invalid_pick"] = corner
    if flow == "lens-calibration":
        center_x = int(mapping["center"][0])
        center_y = int(mapping["center"][1])
        center = call("GET", "plates/"+plate["id"]+"/pick?x=%d&y=%d" % (center_x, center_y))
        require(center["valid_source"] and center["object"] is not None,
                "commission", "Valid pixel lost its source", 500)
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"again"})
        second = call("POST", "worker/step")["plate"]
        comparison = call("POST", "comparisons", {"left":plate["id"], "right":second["id"]})["payload"]
        require(comparison["color_changed"] == 0 and comparison["invalid_changed"] == 0,
                "commission", "Calibrated rendering is not deterministic", 500)
        result["comparison"] = {"invalid_changed": comparison["invalid_changed"],
                                "color_changed": comparison["color_changed"]}
    else:
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"coarse", "mapping_tolerance":2})
        second = call("POST", "worker/step")["plate"]
        result["rejected"] = rejected("POST", "comparisons", {"left":plate["id"], "right":second["id"]}, "alignment")
    from facetreader import validate_bundle
    report = validate_bundle(call("GET", "plates/"+plate["id"]+"/bundle")[0])
    require(report["valid"] and report["summary"]["invalid_pixels"] == statistics["invalid_pixels"],
            "commission", "Offline reader cannot reproduce the mapping", 500)
    result["offline"] = report["summary"]
    return result


def commission(root, flow, output=None):
    app = Application(root)
    def call(method, route, data=None): return route_request(app, method, PREFIX + route, data)
    def rejected(method, route, data, code):
        try: call(method, route, data)
        except Fault as exc:
            require(exc.code == code, "commission", f"Expected {code}, got {exc.code}", 500)
            return exc.code
        raise Fault("commission", "Expected rejection", 500)
    if flow in {"lens-calibration", "valid-framing", "mapping-rejection", "mapping-preview"}:
        return _commission_mapping(app, call, rejected, flow, output)
    mesh_data = {"name":"斜翼", "vertices":[[-1,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,1.2]], "faces":[[0,1,4],[1,2,4],[2,3,4],[3,0,4],[0,3,2],[0,2,1]]}
    mesh = call("POST", "meshes", mesh_data)
    pigment = call("POST", "pigments", {"name":"釉青", "color":[.12,.62,.61], "two_sided":True})
    assembly = call("POST", "assemblies", {"name":"斜翼结构说明图", "lens":{"eye":[3,2,4]}})
    assembly = call("PUT", "assemblies/"+assembly["id"], {"revision":1, "placements":[{"key":"wing", "mesh_id":mesh["id"], "pigment_id":pigment["id"], "transform":{"euler":[0,20,0]}}]})
    result = {"workflow":flow}
    if flow == "mesh-intake":
        result["rejected"] = rejected("POST", "meshes", {**mesh_data, "faces":[[0,0,1]]}, "face")
        return result
    if flow == "mesh-retirement":
        call("POST", "meshes/"+mesh["id"]+"/retire", {"revision":1})
        result["rejected"] = rejected("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2}, "retired")
        return result
    if flow in ("assembly-edit", "lens-edit"):
        changes = {"name":"更新说明图"} if flow == "assembly-edit" else {"lens":{"projection":"orthographic"}}
        edited = call("PUT", "assemblies/"+assembly["id"], {"revision":2, **changes})
        result.update(revision=edited["revision"], rejected=rejected("PUT", "assemblies/"+assembly["id"], {"revision":2, "name":"过期"}, "stale_revision"))
        return result
    seal = call("POST", "assemblies/"+assembly["id"]+"/seal", {"revision":2})
    if flow == "seal-closure":
        call("PUT", "pigments/"+pigment["id"], {"revision":1, "name":"改色", "color":[1,0,0]})
        require(call("GET", "seals/"+seal["id"]) == seal, "commission", "Seal changed", 500)
        result["rejected"] = rejected("PUT", "assemblies/"+assembly["id"], {"revision":3, "name":"改稿"}, "sealed")
        return result
    request = {"key":"commission", "width":96, "height":96, "samples":2}
    if flow in {'reserve-capacity', 'settle-capacity', 'resize-capacity'}:
        request.update(width=16, height=16, samples=1)
        call('PUT', 'resources', {'capacity':256, 'revision':1})
        exposure = call('POST', 'seals/'+seal['id']+'/exposures', request)
        result['reserved'] = call('GET', 'resources')['reserved']
        require(result['reserved'] == 256, 'commission', 'Reservation missing', 500)
        result['rejected'] = rejected('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'too-many'}, 'capacity_reserved')
        if flow == 'reserve-capacity':
            call('POST', 'exposures/'+exposure['id']+'/cancel')
        elif flow == 'settle-capacity':
            claim = app.exposures.claim()
            call('POST', 'exposures/'+exposure['id']+'/cancel')
            require(call('GET', 'resources')['reserved'] == 256, 'commission', 'Accepted cancellation released running work', 500)
            app.exposures.fail(exposure['id'], claim['token'], 'cancel_checkpoint')
        else:
            call('PUT', 'resources', {'capacity':512, 'revision':2})
            second = call('POST', 'seals/'+seal['id']+'/exposures', {**request,'key':'second'})
            policy = call('PUT', 'resources', {'capacity':256, 'revision':3})
            require(policy['overcommitted'] == 256, 'commission', 'Prior promises were erased', 500)
            rejected('PUT', 'resources', {'capacity':512,'revision':3}, 'stale_revision')
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Promised work lost',500)
            require(call('POST', 'worker/step')['state']=='succeeded','commission','Second promise lost',500)
            result['settled'] = second['id']
        require(call('GET', 'resources')['reserved'] == 0, 'commission', 'Settled work leaked capacity', 500)
        result['capacity'] = call('GET', 'resources')
        return result
    exposure = call("POST", "seals/"+seal["id"]+"/exposures", request)
    eroute = "exposures/"+exposure["id"]
    if flow == 'tile-progress':
        require(not call('GET', eroute+'/progress')['planned'], 'commission', 'Queued work claims completed pixels', 500)
    if flow == "exposure-submit":
        require(call("POST", "seals/"+seal["id"]+"/exposures", request)["id"] == exposure["id"], "commission", "Idempotency mismatch", 500)
        result["rejected"] = rejected("POST", "seals/"+seal["id"]+"/exposures", {**request, "width":128}, "key_conflict")
        return result
    if flow == "cancel-exposure":
        result["state"] = call("POST", eroute+"/cancel")["state"]
        require(call("POST", "worker/step")["state"] == "idle", "commission", "Cancelled work claimed", 500)
        result["rejected"] = rejected("POST", eroute+"/retry", {}, "state")
        return result
    if flow in ("retry-exposure", "recover-lease"):
        claimed = app.exposures.claim(lease_seconds=-1 if flow == "recover-lease" else 120)
        if flow == "retry-exposure":
            app.exposures.fail(exposure["id"], claimed["token"], "interrupted")
            call("POST", eroute+"/retry")
        else:
            require(exposure["id"] in call("POST", "worker/recover")["recovered"], "commission", "Recovery failed", 500)
    rendered = call("POST", "worker/step")
    require(rendered["state"] == "succeeded", "commission", str(rendered), 500)
    plate = rendered["plate"]
    route = "plates/"+plate["id"]
    require(plate["payload"]["statistics"]["covered_pixels"] > 0, "commission", "Empty image", 500)
    result["statistics"] = plate["payload"]["statistics"]
    if output:
        Path(output).mkdir(parents=True, exist_ok=True)
        for name in ("color.png", "depth.png", "object.png"): (Path(output)/name).write_bytes(call("GET", route+"/passes/"+name)[0])
    if flow == 'offline-inspect':
        from facetreader import validate_bundle
        bundle = call('GET', route+'/bundle')[0]
        report = validate_bundle(bundle)
        require(report['valid'], 'commission', str(report), 500)
        damaged = validate_bundle(bundle[:-20])
        require(not damaged['valid'], 'commission', 'Truncated package was accepted', 500)
        result['offline'] = report['summary']
        result['rejected'] = damaged['error']
    elif flow == 'backup-closure':
        proof = call('POST', route+'/proofs', {'note':'备份前核验'})
        call('POST', 'proofs/'+proof['id']+'/decide', {'state':'accepted','note':'通道完整'})
        backup = call('POST', 'maintenance/backups')
        op = backup['operation_id']
        require(call('GET', 'maintenance/backups/'+op)['state']=='running','commission','Missing running backup',500)
        rejected('GET', 'maintenance/backups/'+op+'/verify', {}, 'backup_incomplete')
        completed = call('POST', 'maintenance/backups/'+op+'/resume')
        require(completed['state']=='succeeded','commission','Backup was not published',500)
        call('POST', route+'/quarantine', {'reason':'备份时点后的撤回'})
        require(call('GET', 'maintenance/backups/'+op+'/verify')['valid'],'commission','Immutable backup verification failed',500)
        restore_root = Path(root)/'restore-check'
        shutil.copytree(completed['data_directory'], restore_root)
        restored = Application(restore_root)
        require(restored.plates.verify(plate['id'])['valid'] and restored.proofs.get(proof['id'])['state']=='accepted',
                'commission','Restored snapshot mixed later review facts',500)
        require(restored.bundles.export(plate['id']), 'commission', 'Restored export failed', 500)
        result['backup'] = {'state':completed['state'],'references':len(completed['manifest']['closure']['references'])}
        result['rejected'] = rejected('GET', route+'/bundle', {}, 'plate_state')
    elif flow == 'content-inspection':
        result['inspection'] = call('GET', 'maintenance/inspect')
        result['rejected'] = rejected('GET', 'maintenance/backups/unknown', {}, 'backup_id')
    elif flow == 'tile-progress':
        progress = call('GET', eroute+'/progress')
        require(progress['complete'] and progress['committed_tiles'] == 9 and progress['state'] == 'succeeded',
                'commission', 'Persisted progress does not match the completed exposure', 500)
        result['progress'] = progress
        result['verification'] = call('POST', route+'/verify')
    elif flow == "verify-plate": result["verification"] = call("POST", route+"/verify")
    elif flow == "pixel-pick":
        result["pick"] = call("GET", route+"/pick?x=48&y=48")
        require(result["pick"]["object"] is not None, "commission", "Center object missing", 500)
    elif flow == "compare-plates":
        call("POST", "seals/"+seal["id"]+"/exposures", {**request, "key":"second"})
        second = call("POST", "worker/step")["plate"]
        result["comparison"] = call("POST", "comparisons", {"left":plate["id"], "right":second["id"]})["payload"]
        require(result["comparison"]["color_changed"] == 0, "commission", "Determinism failed", 500)
    elif flow in ("proof-decision", "quarantine-plate"):
        proof = call("POST", route+"/proofs", {"note":"检查结构轮廓"})
        result["proof"] = call("POST", "proofs/"+proof["id"]+"/decide", {"state":"accepted", "note":"通过"})["state"]
        if flow == "quarantine-plate":
            call("POST", route+"/quarantine", {"reason":"撤回"})
            require(call("GET", "proofs/"+proof["id"])["state"] == "invalidated", "commission", "Proof remains accepted", 500)
            call("POST", route+"/purge")
            result["rejected"] = rejected("GET", route+"/bundle", {}, "plate_state")
    elif flow == "export-bundle":
        bundle = call("GET", route+"/bundle")[0]
        require(bundle == call("GET", route+"/bundle")[0], "commission", "Bundle changed", 500)
        result["bundle_bytes"] = len(bundle)
    if "rejected" not in result:
        result["rejected"] = rejected("POST", eroute+"/cancel", {}, "state")
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--flow", choices=FLOWS, default="render-plates")
    parser.add_argument("--output")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="facetproof-") as root:
        print(json.dumps(commission(root, args.flow, args.output), ensure_ascii=False, indent=2))
if __name__ == "__main__": main()
