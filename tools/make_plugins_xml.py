#!/usr/bin/env python3
"""Write plugins.xml, the QGIS plugin repository index, for the DamSafety zips of one release.

    python3 tools/make_plugins_xml.py --tag v2026.11.07 --zips dist --out dist/plugins.xml
    python3 tools/make_plugins_xml.py --tag v2026.11.07 --zips dist --out dist/plugins.xml \\
        --merge gh-pages/plugins.xml      (keep other plugins / older entries already published)

One <pyqgis_plugin> per zip in --zips (DamSafety_<Name>_<version>.zip, as tools/build_release.py
names them), filled from the plugin's metadata.txt inside the zip, with download_url pointing at
the GitHub Release asset:
  https://github.com/nickdbendickson/dAmIt_Plugins/releases/download/<tag>/<zip>
QGIS reads the file from the URL the user adds under Plugins > Manage and Install Plugins >
Settings > Add repository (https://nickdbendickson.github.io/dAmIt_Plugins/plugins.xml, published
to the gh-pages branch by .github/workflows/release.yml).

--merge takes a previously published plugins.xml and keeps every entry whose (name, version)
is not replaced by a new one, so a release of one plugin does not drop the others. The index is
written with the fields QGIS's plugin installer reads (name, version, description, about,
qgis_minimum_version, qgis_maximum_version, homepage, file_name, icon, author_name, download_url,
update_date, experimental, deprecated, tracker, repository, tags, supports_qt6). Pure stdlib."""
import argparse
import configparser
import datetime
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

RELEASE_URL = "https://github.com/nickdbendickson/dAmIt_Plugins/releases/download/{tag}/{zip}"
ZIP_RE = re.compile(r"^(DamSafety_[A-Za-z]+)_(\d+\.\d+\.\d+)\.zip$")
# metadata key -> plugins.xml element (in the order the official repository writes them)
FIELDS = (("description", "description"), ("about", "about"), ("version", "version"),
          ("qgisMinimumVersion", "qgis_minimum_version"), ("qgisMaximumVersion", "qgis_maximum_version"),
          ("homepage", "homepage"), ("author", "author_name"), ("tracker", "tracker"),
          ("repository", "repository"), ("tags", "tags"), ("experimental", "experimental"),
          ("deprecated", "deprecated"), ("supportsQt6", "supports_qt6"))


def read_metadata(zip_path):
    """(plugin folder, {key: value}) from the metadata.txt inside a plugin zip."""
    with zipfile.ZipFile(zip_path) as z:
        metas = [n for n in z.namelist() if n.count("/") == 1 and n.endswith("/metadata.txt")]
        if len(metas) != 1:
            raise SystemExit("{}: expected one <plugin>/metadata.txt, found {}".format(
                zip_path, metas))
        folder = metas[0].split("/")[0]
        # QGIS reads metadata.txt with configparser and % interpolation (tools/ci_checks.py
        # guards the files for that), so read it the same way
        cp = configparser.ConfigParser()
        cp.read_string(z.read(metas[0]).decode("utf-8-sig"))
        return folder, {k: cp.get("general", k) for k in cp.options("general")}


def plugin_element(zip_name, meta, folder, tag, date):
    name = meta.get("name") or folder
    el = ET.Element("pyqgis_plugin", name=name, version=meta.get("version", ""))
    lower = {k.lower(): v for k, v in meta.items()}
    for key, tagname in FIELDS:
        val = lower.get(key.lower(), "")
        if key in ("experimental", "deprecated", "supportsQt6"):
            val = "True" if str(val).strip().lower() in ("true", "1", "yes") else "False"
        ET.SubElement(el, tagname).text = val.strip()
    ET.SubElement(el, "file_name").text = zip_name
    ET.SubElement(el, "icon").text = "{}/{}".format(folder, lower.get("icon", "icon.svg")).strip()
    ET.SubElement(el, "download_url").text = RELEASE_URL.format(tag=tag, zip=zip_name)
    ET.SubElement(el, "update_date").text = date
    ET.SubElement(el, "create_date").text = date
    return el


def merge_existing(path, new_keys):
    """The <pyqgis_plugin> elements of an existing plugins.xml not replaced by a new entry."""
    if not path or not os.path.isfile(path):
        return []
    try:
        old = ET.parse(path).getroot()
    except ET.ParseError as e:
        print("NOTE: {} not merged (not well-formed: {})".format(path, e), file=sys.stderr)
        return []
    kept = []
    for el in old.findall("pyqgis_plugin"):
        if (el.get("name"), el.get("version")) not in new_keys and el.get("name") not in {
                n for n, _v in new_keys}:
            kept.append(el)
    return kept


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tag", required=True, help="the release tag the zips are uploaded under")
    ap.add_argument("--zips", required=True, help="folder with the DamSafety_<Name>_<ver>.zip files")
    ap.add_argument("--out", required=True, help="plugins.xml to write")
    ap.add_argument("--merge", default="", help="an existing plugins.xml whose other entries are kept")
    ap.add_argument("--date", default="", help="update_date (YYYY-MM-DD) [today, UTC]")
    a = ap.parse_args(argv)
    date = a.date or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    root = ET.Element("plugins")
    new_keys = set()
    zips = sorted(n for n in os.listdir(a.zips) if ZIP_RE.match(n))
    if not zips:
        raise SystemExit("no DamSafety_<Name>_<version>.zip in " + a.zips)
    for zip_name in zips:
        folder, meta = read_metadata(os.path.join(a.zips, zip_name))
        if ZIP_RE.match(zip_name).group(2) != meta.get("version", "").strip():
            raise SystemExit("{}: zip name version != metadata.txt version {}".format(
                zip_name, meta.get("version")))
        el = plugin_element(zip_name, meta, folder, a.tag, date)
        new_keys.add((el.get("name"), el.get("version")))
        root.append(el)
        print("{} {} -> {}".format(el.get("name"), el.get("version"), el.find("download_url").text))
    for el in merge_existing(a.merge, new_keys):
        root.append(el)
        print("kept {} {} from {}".format(el.get("name"), el.get("version"), a.merge))
    try:
        ET.indent(root)                                  # Python 3.9+
    except AttributeError:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    ET.ElementTree(root).write(a.out, encoding="utf-8", xml_declaration=True)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
