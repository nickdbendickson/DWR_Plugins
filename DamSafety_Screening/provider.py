from qgis.core import QgsProcessingProvider
from .algorithms.data_package import DataPackage
from .algorithms.download_layers import DownloadLayers
from .algorithms.prepare_dams import PrepareDams
from .algorithms.terrain_cache import TerrainCache
from .algorithms.download_dem import DownloadDem
from .algorithms.estimate_storage import EstimateStorage
from .algorithms.breach_direction import BreachDirection
from .algorithms.breach_hydrograph import BreachHydrograph
from .algorithms.triage import Triage
from .algorithms.run_breach_sim import RunBreachSim
from .algorithms.runner_handoff import RunnerHandoff
from .algorithms.hazard_classification import HazardClassification
from .algorithms.sensitivity import Sensitivity
from .algorithms.reports import Reports
from .algorithms.compare_reference import CompareReference
from .algorithms.estimate_runtime import EstimateRuntime
from .algorithms.inventory_diff import InventoryDiff
from .algorithms.selftest import SelfTest
from .algorithms.run_chain import RunChain
from .algorithms.find_ponds import FindPonds
from .algorithms.propose_dam import ProposeDam
from .algorithms.preflight import Preflight
from .algorithms.merge_ponds import MergePonds
from .algorithms import _settings as ST
from .core import workflow as _wf


# the tools as written (their defaults are the "starting values" of the settings grid)
ORIGINAL_ALGORITHMS = (DataPackage, DownloadLayers, PrepareDams, TerrainCache, DownloadDem, EstimateStorage, BreachDirection, BreachHydrograph,
                       Triage, RunBreachSim, RunnerHandoff, HazardClassification, Sensitivity, Reports,
                       CompareReference, EstimateRuntime, InventoryDiff, SelfTest, RunChain, FindPonds,
                       ProposeDam, Preflight, MergePonds)


def _step_of(cls):
    try:
        name = cls().name()
    except Exception:                                            # noqa
        return None
    for k, a in _wf.ALG.items():
        if a == name:
            return k
    return None


# 0.13: every tool takes its parameter defaults from the suite settings file and sits in the
# "Screening steps" or "Advanced" group of the toolbox (algorithms/_settings.settings_aware)
ALGORITHMS = tuple(ST.settings_aware(c, _step_of(c)) for c in ORIGINAL_ALGORITHMS)


def _with_manifest(cls):
    """0.11: after a tool has run, write a run manifest next to each file / folder it wrote
    (core/manifest.py: what it read and wrote, by content). Done here once for every tool
    rather than inside each one; a manifest problem never fails the tool."""
    if getattr(cls, "_manifest_wrapped", False):
        return cls
    inner = cls.processAlgorithm

    def processAlgorithm(self, params, context, feedback):
        result = inner(self, params, context, feedback)
        try:
            from .core import manifest as mf
            written = mf.record("nddam:" + self.name(), params, result)
            for w in written:
                feedback.pushInfo("run manifest: {}".format(w))
        except Exception as e:                                   # noqa
            try:
                feedback.pushInfo("run manifest not written ({})".format(e))
            except Exception:                                    # noqa
                pass
        try:                                                     # the suite's layer styles
            from . import suite_styles
            suite_styles.attach_results(context, result)
        except Exception:                                        # noqa
            pass
        return result

    processAlgorithm.__doc__ = inner.__doc__
    cls.processAlgorithm = processAlgorithm
    cls._manifest_wrapped = True
    return cls


for _cls in ALGORITHMS:
    _with_manifest(_cls)


class NDDamProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        for cls in ALGORITHMS:
            self.addAlgorithm(cls())

    def id(self):
        return "nddam"

    def name(self):
        return "DamSafety Screening"
