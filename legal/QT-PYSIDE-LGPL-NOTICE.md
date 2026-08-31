# Qt and PySide LGPL Notice

MatteLoop's original source code is licensed under 0BSD. The unsigned native
application also contains dynamically linked Qt and Qt for Python components
from the PySide6, PySide6_Essentials, PySide6_Addons, and shiboken6 6.10.3
distributions. Those third-party components are available under their
applicable commercial or open-source terms, including the GNU Lesser General
Public License version 3. This notice does not relicense MatteLoop's original
code.

The complete GPL version 3 and LGPL version 3 texts are installed beside this
notice as `GPL-3.0.txt` and `LGPL-3.0.txt`. Practical installation information
for rebuilding and replacing the dynamically linked components is installed as
`RELINK.md`.

Binary distribution must keep the application together with all four adjacent
source deliverables:

- its `MatteLoop-media-sources-<target>-<identity>.tar.gz` archive and
  `.sha256` file; and
- its `MatteLoop-qt-sources-6.10.3-<identity>.tar.gz` companion and `.sha256`
  file.

The Qt companion contains the original, unmodified official source archives
for Qt Base 6.10.3, Qt Image Formats 6.10.3, and PySide Setup 6.10.3, plus
checksums, the installed package/file inventory, component provenance, these
license texts, replacement instructions, and the project-side build evidence.
MatteLoop applies no patch to those Qt or PySide sources.

Qt and PySide names and trademarks belong to their respective owners. Patent
rights, if any, are separate from copyright license permissions.
