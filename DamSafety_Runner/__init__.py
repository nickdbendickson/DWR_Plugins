def classFactory(iface):
    from .plugin import NDBreachRunnerPlugin
    return NDBreachRunnerPlugin(iface)
