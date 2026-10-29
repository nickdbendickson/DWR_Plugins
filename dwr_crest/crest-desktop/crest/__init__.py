"""Crest desktop: a native window around the built web desk (crest/web/).

There is no Python engine. The breach, flood, screen and export logic is the web app's (src/lib/crest), bundled by
`npm run build:desktop` into crest/web/. This package only serves those files locally and saves exports to disk.
"""

WEB_DIR_NAME = "web"
