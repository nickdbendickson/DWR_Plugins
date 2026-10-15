# -*- coding: utf-8 -*-
def _install_transform_guard():
    """Re-create intermittently invalid coordinate transformations (see damsuite/transformguard.py).
    Loaded by file so it runs before any plugin module imports QgsCoordinateTransform."""
    try:
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "_nd_transformguard", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "damsuite", "transformguard.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.install()
    except Exception as e:
        # Lumo v1.6: say so (QGIS log) instead of failing silently; the plugin still loads
        try:
            from qgis.core import Qgis, QgsMessageLog
            QgsMessageLog.logMessage("Coordinate-transform guard not installed: {}".format(e),
                                     "ND Dam Hazard Helper", Qgis.Warning)
        except Exception:
            pass


_install_transform_guard()

def classFactory(iface):
    """QGIS plugin entry point."""
    from .plugin import NDdamHazardHelperPlugin
    return NDdamHazardHelperPlugin(iface)
