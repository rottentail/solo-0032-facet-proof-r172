from .database import Database
from .blobs import Blobs
from .meshes import Meshes
from .pigments import Pigments
from .assemblies import Assemblies
from .seals import Seals
from .exposures import Exposures
from .planner import RENDERER_REVISION
from .plates import Plates
from .preview import Previews
from .worker import Worker
from .comparison import Comparisons
from .proofs import Proofs
from .bundles import Bundles
from .resources import ResourceBudget
from .maintenance import Maintenance

class Application:
    def __init__(self, root):
        self.db = Database(root)
        self.blobs = Blobs(root)
        self.meshes = Meshes(self.db)
        self.pigments = Pigments(self.db)
        self.assemblies = Assemblies(self.db)
        self.seals = Seals(self.db)
        self.resources = ResourceBudget(self.db)
        self.exposures = Exposures(self.db, self.resources)
        self.plates = Plates(self.db, self.blobs, self.exposures)
        self.previews = Previews(self.db)
        self.worker = Worker(self.db, self.exposures, self.plates, self.blobs)
        self.tiles = self.worker.tiles
        self.comparisons = Comparisons(self.db, self.plates, self.blobs)
        self.proofs = Proofs(self.db, self.plates)
        self.bundles = Bundles(self.db, self.plates, self.blobs)
        self.maintenance = Maintenance(self.db, self.blobs, self.plates)

    def health(self):
        with self.db.unit_of_work() as conn:
            return {"integrity": conn.execute("PRAGMA quick_check").fetchone()[0], "renderer": RENDERER_REVISION}
