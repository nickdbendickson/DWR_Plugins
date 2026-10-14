def classFactory(iface):
    from .plugin import NDDamScreeningPlugin
    return NDDamScreeningPlugin(iface)
