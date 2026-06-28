# Qt / PySide6 LGPL Notice and Relink Offer

This notice applies to xPST desktop bundles that ship the native QML desktop
application. Those bundles dynamically link against the Qt libraries through the
PySide6 Python bindings. This document satisfies the desktop-packaging
obligations described in [LICENSING_REPORT.md](LICENSING_REPORT.md) and
complements [LICENSE](LICENSE) and [NOTICES.md](NOTICES.md).

xPST itself is dual licensed under `MIT OR Apache-2.0`. This notice covers only
the bundled Qt and PySide6 components and does not change the license of xPST.

## Component and License

| Component | Upstream | License used by xPST |
|---|---|---|
| PySide6 (Qt for Python bindings) | The Qt Company / Qt Project | GNU Lesser General Public License version 3 (LGPL-3.0) |
| Qt libraries (Qt 6) | The Qt Company / Qt Project | GNU Lesser General Public License version 3 (LGPL-3.0) |

PySide6 and Qt are made available under multiple terms
(`LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`, plus a separate commercial
license from The Qt Company). xPST distributes the bundled Qt and PySide6
components under the terms of the **GNU Lesser General Public License, version 3
(LGPL-3.0)**.

A copy of the LGPL v3 is available at
<https://www.gnu.org/licenses/lgpl-3.0.txt>, and it incorporates by reference
the GNU General Public License v3 at <https://www.gnu.org/licenses/gpl-3.0.txt>.
The Qt source code and corresponding license texts are published by the Qt
Project at <https://download.qt.io/> and the PySide6 source is published at
<https://code.qt.io/cgit/pyside/pyside-setup.git/>.

## LGPL Compliance Posture

xPST follows the LGPL-3.0 redistribution model for desktop bundles:

- Qt is used through PySide6 as **dynamically linked** shared libraries. xPST
  does not statically link Qt.
- The bundled Qt/PySide6 shared libraries are unmodified upstream releases.
- This notice and the upstream license texts above are included so recipients
  receive the required copyright and license information.

## Written Offer to Relink

In accordance with the LGPL v3 (in particular section 4 on "Combined Works"),
xPST provides the means for a recipient to replace the bundled Qt/PySide6
libraries with a modified, compatible version and to relink the application
against that version.

The xPST desktop application is distributed in a form that supports relinking
against a user-supplied, LGPL-compatible build of Qt/PySide6:

- The desktop application loads Qt through the dynamically linked PySide6
  shared libraries shipped in the bundle. A recipient may substitute a modified
  but interface-compatible build of those Qt/PySide6 shared libraries in place
  of the shipped ones and run the application against the substituted
  libraries.
- The complete source code for the exact PySide6 and Qt versions used in a
  given xPST release, together with the build configuration needed to produce
  compatible shared libraries, is available from the upstream Qt Project URLs
  listed above. The shipped versions are recorded in each release's SBOM
  (`xpst-sbom.cdx.json`) and dependency notices.
- For at least three (3) years from the date of a given xPST desktop release,
  the xPST maintainers will, on request, provide the information required to
  relink that release against a modified version of Qt/PySide6, including the
  build configuration and any application object files or installation
  information necessary to do so under the LGPL.

To request relinking information for a specific xPST desktop release, open an
issue at <https://github.com/TysAIs/xPST/issues> identifying the release version
and platform, or contact the maintainers through the project's security/support
channels documented in [SECURITY.md](SECURITY.md).

Last updated: June 2026.
