# Exchange-format test fixtures

Third-party, published Open Group exchange files, pinned byte-for-byte. Retrieved
2026-10-27 with `curl`; none of them was opened in, or produced by running, any tool here.

| file | source | sha256 | license |
|---|---|---|---|
| `archisurance.xml` | https://raw.githubusercontent.com/ThomasRohde/archimate-renderer/1fb9fefaa2455e14ece51e28032c59e83ccfd208/examples/archisurance.xml (an Archi export of the Archisurance case study) | `7ffab113951fa8c08200d898b228dff5e4dcdf7e3b82b9e6e50ba8a4d12b9fc1` | MIT — Copyright (c) 2025 Thomas Jensen (repository LICENSE) |
| `archi_bendpoint.test.xml` | https://raw.githubusercontent.com/archimatetool/archi/9e6a014f9ef6a782a7dda604ef7b4d03d2c7819a/tests/org.opengroup.archimate.xmlexchange.tests/testdata/bendpoint.test.xml | `c2b1e74b44089c84ca054fc2c3dd9a9789e2e7fbc33990aae9654632c08bc95f` | MIT — Archi, Copyright (c) Phillip Beauvoir, Jean-Baptiste Sarrodie, The Open Group |
| `opengroup_Model_View.xml` | https://www.opengroup.org/xsd/archimate/3.1/examples/Model_View.xml | `cf4af23751519efd8994449d8d8a470bfdaa75cddefd5e969e1caf94e677cdf7` | The Open Group published example (no license statement in file) |
| `opengroup_Basic_Model_Organization.xml` | https://www.opengroup.org/xsd/archimate/3.1/examples/Basic_Model_Organization.xml | `3823cbeb9d76c1b3d4d8830870b0cfd1891f5d407c0adb567405e86ed7f05045` | as above |
| `opengroup_Basic_Model_Properties.xml` | https://www.opengroup.org/xsd/archimate/3.1/examples/Basic_Model_Properties.xml | `fa3540878b8798f0dfe604a9ab19200007ea2ba5eef47f9c5c3d73d95669ef21` | as above |
| `opengroup_Basic_Model_Metadata.xml` | https://www.opengroup.org/xsd/archimate/3.1/examples/Basic_Model_Metadata.xml | `75c9dd683c2ce3852dae7f774853d9b0a54258d7179b7a9fbfdca126f59c2358` | as above |

`tests/test_archimate_exchange.py` re-hashes every file and fails if a byte changes.

## Archi 5.10.0 oracle outputs (`archi510_*`)

Produced 2026-10-27 on macOS 27.0 / arm64 (the two `*_reexport` files regenerated the same day after notes, groups, labels, lines, border types and folders were mapped) by the official Archi 5.10.0 release, run headless
from a scratch copy (never installed, never committed):

- Release: https://github.com/archimatetool/archi.io/releases/download/5.10/Archi-Mac-Silicon-5.10.0.dmg
  (linked from https://www.archimatetool.com/download/), sha256
  `be4ccc46f952e8ba856d1414080d3eff6bfc52bfb087df6c18ed6bb0a94a5b85` — equal to the value in the
  release's own `Archi-5.10.0-SHA256.txt`. Code signature: `Developer ID Application: Phillip
  Beauvoir (375WT5T296)`.
- Archi bundles the same 3.1 schemas: its `archimate3_Model/View/Diagram.xsd` are byte-identical
  to the as-published files (`src/data/archimate/exchange/3.1/SOURCE.md`).

Command (`$A` = `Archi.app/Contents/MacOS/Archi -application com.archimatetool.commandline.app
-consoleLog -nosplash`):

| fixture | input | command | sha256 |
|---|---|---|---|
| `archi510_full_reexport.xml` | `export_exchange(full_snapshot(), model_id="m-1", name="Full", documentation="all of it")` (every concept/relationship type, notes, groups with documentation, labels, a line, border types), sha256 `fc6794b11c5220cda7059a314589c9fa6f763d62e5cc773326c2c88d4887443e` | `$A --xmlexchange.import in.xml --xmlexchange.export out.xml --xmlexchange.exportFolders` (Archi: "Validated!" on both) | `3c6816972519fdcd9111710b27e0cca54571c03789215c2ae8530f26f1399051` |
| `archi510_probe_reexport.xml` | `export_exchange(probe_snapshot(), model_id="probe", name="Probe", language="nl")` (edge cases, nested custom folders, lines of every endpoint kind), sha256 `c38b7f7b9865091d57d35d5a59489cad63d123f84b51f08e3dc468c0db81cea2` | same; Archi validated the input, then reported its own output XSD-invalid (`cvc-identity-constraint.4.3: Key 'ConnectionTargetRef' with value 'c-agg' not found`) and wrote it anyway; a second run produced identical bytes | `2d3e280a887a59590b8ed35285f108c05dbf2e010757405f47a93ee6dbd19d75` |
| `archi510_native.archimate` | hand-written Archi model (input, not Archi output) using Archi-only features | — | `27931f719894800a293c1d199f8ef773b8b802465ee8cacc8d2785abe4588c28` |
| `archi510_native_export.xml` | `archi510_native.archimate` | `$A --loadModel in.archimate --xmlexchange.export out.xml --xmlexchange.exportFolders --xmlexchange.exportLang en` | `a28a4980bdf8e8bc6fe1319a06077d99a1825d0389896f6dc3c798ba58098b7d` |

`full_snapshot()` is in `tests/test_archimate_exchange.py`, `probe_snapshot()` in
`tests/test_archimate_exchange_archi.py`. The Archi outputs keep Archi's CRLF line endings and
are pinned byte-for-byte. `test_archi_fixtures_are_not_stale` fails when today's export of
either builder no longer matches the recorded input hash: regenerate with the commands above.

Also run on the same day and found equivalent here (not pinned): Archi re-export of our re-export
of `archisurance.xml` (only difference: names with whitespace runs collapsed), and Archi's own
import/export of `archisurance.xml` with and without `--xmlexchange.exportFolders` (same).

Every difference Archi introduces is listed, with its cause, in
`tests/test_archimate_exchange_archi.py` (`FULL_EXPECTED`, `PROBE_EXPECTED`) and in the
"Interoperability with Archi 5.10.0" section of the `tools/architecture/exchange.py` docstring.
Archi is MIT-licensed; these files are its output on inputs written for this repository.
