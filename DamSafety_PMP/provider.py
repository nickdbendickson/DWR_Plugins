from qgis.core import QgsProcessingProvider
from .algorithm import NDPMPAlgorithm
from .algorithm_hydro import InflowHydrographAlgorithm
from .algorithm_pothole import PotholeStorageAlgorithm
from .algorithm_spatial_snow import SpatialDistributionAlgorithm, SnowmeltAlgorithm
from .algorithm_hms import ExportHMSAlgorithm

SETTING_DATA_HOME = "nd_pmp/data_home"   # QgsSettings key: path to the 'PMP_Evaluation_Tool' folder


class NDPMPProvider(QgsProcessingProvider):
    def id(self):
        return "ndpmp"

    def name(self):
        return "ND PMP (prototype)"

    def longName(self):
        return "North Dakota PMP Evaluation Tool - QGIS prototype"

    def loadAlgorithms(self):
        self.addAlgorithm(NDPMPAlgorithm())
        self.addAlgorithm(InflowHydrographAlgorithm())
        self.addAlgorithm(PotholeStorageAlgorithm())
        self.addAlgorithm(SpatialDistributionAlgorithm())
        self.addAlgorithm(SnowmeltAlgorithm())
        self.addAlgorithm(ExportHMSAlgorithm())
