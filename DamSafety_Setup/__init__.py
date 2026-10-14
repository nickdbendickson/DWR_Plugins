def classFactory(iface):
    from .plugin import DamSafetySetupPlugin
    return DamSafetySetupPlugin(iface)
