"""Pre-download the .cls/.sty files that PreTeXt's journal texstyles require.

Some journal styles (AMS, E-JC, Springer, ...) list <required-files> that
PreTeXt downloads during a PDF build. Build containers have no network, so we
fetch them here, at image-build time, into ~/.ptx/latex-packages/<code>/.

We call PreTeXt's own place_latex_package_files() rather than reading the
texstyle URLs ourselves, so this stays in sync with whatever texstyles ship
with the installed PreTeXt version.

A failed download (e.g. a publisher moves its template) only warns: one broken
upstream URL shouldn't block image updates for every other journal. Builds for
that journal will fail at runtime until the URL is fixed upstream.
"""
import os
import sys
import tempfile

from lxml import etree as ET
from pretext import core

CACHE_DIR = os.path.expanduser("~/.ptx/latex-packages")

journals_xml = os.path.join(core.get_ptx_path(), "journals", "journals.xml")
tree = ET.parse(journals_xml)
tree.xinclude()
codes = [c.text for c in tree.xpath("//journal/code")]

failed = []
for code in codes:
    # place_latex_package_files also copies each file into a destination
    # directory; we only want the cache, so give it a throwaway one.
    with tempfile.TemporaryDirectory() as dest:
        try:
            core.pretext.place_latex_package_files(dest, code, CACHE_DIR)
        except Exception as e:
            failed.append(code)
            print(f"WARNING: could not fetch latex packages for {code}: {e}", file=sys.stderr)

print(f"Cached latex packages for {len(codes) - len(failed)}/{len(codes)} journals in {CACHE_DIR}")
if failed:
    print(f"WARNING: missing packages for: {', '.join(failed)}", file=sys.stderr)
