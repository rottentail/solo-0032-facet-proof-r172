from urllib.parse import parse_qs, unquote, urlsplit
from .errors import Fault, require
from .values import object_value

PREFIX = "/facet/v1/"

def route_request(app, method, target, data=None):
    url = urlsplit(target)
    require(url.path.startswith(PREFIX), "not_found", "Unknown route", 404)
    p = [unquote(s) for s in url.path[len(PREFIX):].split("/")]
    data = object_value({} if data is None else data)
    if method == "GET" and p == ["health"]: return app.health()
    if method == 'GET' and p == ['resources']: return app.resources.snapshot()
    if method == 'PUT' and p == ['resources']:
        return app.resources.revise(data.get('capacity'), data.get('revision'))
    if method == 'POST' and p == ['resources', 'reconcile']: return app.resources.reconcile()
    if method == 'POST' and p == ['maintenance', 'backups']: return app.maintenance.begin_backup()
    if method == 'GET' and p == ['maintenance', 'inspect']: return app.maintenance.inspect()
    if len(p) == 3 and p[:2] == ['maintenance', 'backups'] and method == 'GET':
        return app.maintenance.get(p[2])
    if len(p) == 4 and p[:2] == ['maintenance', 'backups']:
        if method == 'POST' and p[3] == 'resume': return app.maintenance.resume_backup(p[2])
        if method == 'GET' and p[3] == 'verify': return app.maintenance.verify_backup(p[2])
    if method == "POST" and p == ["meshes"]: return app.meshes.create(data)
    if method == "POST" and p == ["pigments"]: return app.pigments.create(data)
    if method == "POST" and p == ["assemblies"]: return app.assemblies.create(data)
    if method == "POST" and p == ["comparisons"]: return app.comparisons.create(data)
    if method == "POST" and p == ["worker", "step"]: return app.worker.run_one()
    if method == "POST" and p == ["worker", "recover"]: return app.exposures.recover()
    if len(p) == 2:
        group, ident = p
        if method == "GET" and group == "exposures": return app.exposures.get(ident)
        if method == "GET" and group == "plates": return app.plates.get(ident)
        if method == "GET" and group == "proofs": return app.proofs.get(ident)
        if method == "PUT" and group == "assemblies": return app.assemblies.revise(ident, data)
        if method == "PUT" and group == "pigments":
            require("revision" in data, "revision", "Expected revision is required")
            return app.pigments.revise(ident, data)
        if method == "GET" and group in {"meshes", "pigments", "assemblies", "seals", "comparisons"}:
            kind = {"meshes":"mesh", "pigments":"pigment", "assemblies":"assembly", "seals":"seal", "comparisons":"comparison"}[group]
            with app.db.unit_of_work() as conn: return app.db.get(conn, ident, kind)
    if len(p) == 3:
        group, ident, action = p
        if method == "POST" and group == "meshes" and action == "retire": return app.meshes.retire(ident, data.get("revision"))
        if method == "POST" and group == "assemblies" and action == "seal": return app.seals.create(ident, data.get("revision"))
        if method == "POST" and group == "assemblies" and action == "fork": return app.assemblies.fork(ident, data.get("name"))
        if method == "POST" and group == "assemblies" and action == "preview": return app.previews.create(ident, data)
        if method == "POST" and group == "seals" and action == "exposures": return app.exposures.submit(ident, data)
        if method == "POST" and group == "exposures" and action == "cancel": return app.exposures.cancel(ident)
        if method == "POST" and group == "exposures" and action == "retry": return app.exposures.retry(ident)
        if method == 'GET' and group == 'exposures' and action == 'progress': return app.tiles.progress(ident)
        if method == "POST" and group == "plates" and action == "verify": return app.plates.verify(ident)
        if method == "POST" and group == "plates" and action == "quarantine": return app.plates.quarantine(ident, data.get("reason"))
        if method == "POST" and group == "plates" and action == "purge": return app.plates.purge(ident)
        if method == "POST" and group == "plates" and action == "proofs": return app.proofs.open(ident, data)
        if method == "POST" and group == "proofs" and action == "decide": return app.proofs.decide(ident, data)
        if method == "GET" and group == "plates" and action == "bundle": return app.bundles.export(ident), "application/zip"
        if method == "GET" and group == "plates" and action == "pick":
            query = parse_qs(url.query)
            try: x, y = int(query["x"][0]), int(query["y"][0])
            except (KeyError, ValueError, IndexError) as exc: raise Fault("pixel", "Pick requires integer x and y") from exc
            return app.plates.pick(ident, x, y)
        if method == "GET" and group == "events" and action == "history": return {"items": app.db.events(ident)}
    if method == "GET" and len(p) == 4 and p[0] == "plates" and p[2] == "passes":
        return app.plates.read_pass(p[1], p[3]), "image/png" if p[3].endswith(".png") else "application/octet-stream"
    raise Fault("not_found", "Unknown route or method", 404)
