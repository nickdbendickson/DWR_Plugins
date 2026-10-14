# -*- coding: utf-8 -*-

def classFactory(iface):
    """QGIS plugin entry point."""
    from .plugin import NDdamHazardHelperPlugin
    return NDdamHazardHelperPlugin(iface)
