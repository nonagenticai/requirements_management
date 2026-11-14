# Open Group ArchiMate Model Exchange File Format 3.1 — pinned XSDs

Retrieved 2026-10-27 with `curl` (data files only; nothing downloaded was executed).
Namespace: `http://www.opengroup.org/xsd/archimate/3.0/` (schema `version="3.1"`).

| file | source URL | sha256 as published | sha256 as vendored |
|---|---|---|---|
| `archimate3_Model.xsd` | https://www.opengroup.org/xsd/archimate/3.1/archimate3_Model.xsd | `dd451abe3e3193f91dd9544b279af9bbbf17e75ff1ef86f65ad52b3f8cd29794` | `01686f6905f5f6b8bb1cff0373949c3be96a250c4e60314f6acb7f32db3d446f` |
| `archimate3_View.xsd` | https://www.opengroup.org/xsd/archimate/3.1/archimate3_View.xsd | `d708ce176403034b1229b892712cfd69660aefe17da4cc54acea1ac35e4a9854` | unchanged |
| `archimate3_Diagram.xsd` | https://www.opengroup.org/xsd/archimate/3.1/archimate3_Diagram.xsd | `6419080f4c4bc43b4a7b8acf870146a7bae6c3487a3ce08d3c521c028ea6056e` | unchanged |
| `dc.xsd` (Dublin Core, used by `<metadata>`) | https://www.opengroup.org/xsd/archimate/3.1/dc.xsd | `cea8c7327e80c0bc9244dc8db586a4f21b4f093a869044ebdac878d339ebd5c3` | `30e25f92f27bf8832120285db14b85f9b33fc8f8e2f6758a37a6389055fa9997` |
| `xml.xsd` (W3C XML namespace) | https://www.w3.org/2001/xml.xsd | `61960fb3131e38022caad5360e2f33a3382578ab3c80cd58bd74320ede61b20c` | unchanged |

## Local modifications

Only `schemaLocation` of the `xml` namespace import, so that schema compilation never
touches the network:

- `archimate3_Model.xsd` line 11 and `dc.xsd` line 8:
  `schemaLocation="http://www.w3.org/2001/xml.xsd"` → `schemaLocation="xml.xsd"`.

`archimate3_View.xsd` / `archimate3_Diagram.xsd` already include their siblings by
relative path. Dublin Core is imported in memory by `tools/architecture/exchange.py`
(`exchange_schema()`), not by editing the Diagram schema.

## License / notice

What The Open Group actually states, as checked on 2026-10-27:

- The three ArchiMate XSDs contain **no licence or copyright statement** — only an "XSD Data
  File Information" block (Author: The Open Group ArchiMate Exchange Team; start date
  04 Oct 2016; revision date 15 July 2019; XSD Version 3.1; Status: Preliminary).
- The page that publishes them, https://www.opengroup.org/xsd/archimate/ ("ArchiMate® Model
  Exchange File Format for the ArchiMate 3.1/3.2 Modeling Language", last updated
  15 November 2019), ends with "Copyright © 2015-2019 The Open Group, All Rights Reserved." and
  "ArchiMate® is a registered trademark of The Open Group". It grants no explicit licence.
- The standard's FAQ (https://www.opengroup.org/open-group-archimate-model-exchange-file-format,
  version 1.10, October 2022) says "Supporting files (the XSD schema and examples) are
  available from http://www.opengroup.org/xsd/archimate/", served without registration (the
  standard document itself needs a free web ID), and names Archi as the reference source-code
  implementation.
- Archi (MIT licence) redistributes the same three files in
  `org.opengroup.archimate.xmlexchange/xsd/`; the copies inside Archi 5.10.0 are byte-identical
  to the as-published files above.

So: copyright The Open Group, all rights reserved, no written redistribution licence. They are
vendored here unmodified apart from the `schemaLocation` rewrite above, solely to validate
exchange documents — the use they are published for — and are not re-published separately.
If a written licence is ever required, the contact the FAQ gives is ogspecs@opengroup.org.

`xml.xsd` is © World Wide Web Consortium, distributed under the W3C Software and Document
License (https://www.w3.org/copyright/software-license/). `dc.xsd` is the Dublin Core
Metadata Initiative DCMES 1.1 element-set schema (created 2008-02-11) as republished by The
Open Group. DCMI's schema use notice (https://www.dublincore.org/about/copyright/, checked
2026-10-27) licenses its XML schemas under CC BY 4.0 and requires this notice, reproduced here:

> Portions of this software may use XML schemas Copyright © 2011 DCMI, the Dublin Core™
> Metadata Initiative (http://dublincore.org/). These are licensed under the Creative Commons
> 4.0 Attribution license (https://creativecommons.org/licenses/by/4.0/).
