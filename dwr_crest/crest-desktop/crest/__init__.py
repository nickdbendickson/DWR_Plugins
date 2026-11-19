"""Crest desktop: a native window around the built web desk (crest/web/).

There is no Python copy of the breach engine. The desk is the web app's (src/lib/crest), bundled by
`npm run build:desktop` into crest/web/. This package serves those files, saves exports, and — from the
Report step — writes the dam folder and calls the QGIS plugins already installed on this computer.
"""

WEB_DIR_NAME = "web"
