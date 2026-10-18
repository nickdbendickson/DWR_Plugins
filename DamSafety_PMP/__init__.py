def classFactory(iface):
    from .plugin import NDPMPPlugin
    return NDPMPPlugin(iface)
