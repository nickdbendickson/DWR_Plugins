"""Mock of qgis.utils for damsuite.versions: PLUGIN_VERSIONS {folder: version} set by a test."""
PLUGIN_VERSIONS = {}
plugins = {}


def pluginMetadata(folder, key):
    if key == "version" and folder in PLUGIN_VERSIONS:
        return PLUGIN_VERSIONS[folder]
    return "__error__"


# real qgis.utils has these (suite_home loads a plugin that is installed but not started);
# here nothing is installed, so both fail like QGIS does for an unknown plugin
def loadPlugin(name):
    return False


def startPlugin(name):
    return False
