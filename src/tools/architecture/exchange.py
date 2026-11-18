"""Open Group ArchiMate Model Exchange File Format (3.1) import and export.

Pure transport adapter between the exchange XML and :class:`ArchimateSnapshot`.
No database, no routes, no network. The exchange format is a transport format,
not persistence (The Open Group says so itself); the snapshot is the contract.

Guarantees
----------
- **Export is XSD-valid and deterministic.** Output is validated against the
  locally pinned 3.1 model/view/diagram schemas (``src/data/archimate/exchange/
  3.1``) before it is returned; the same input always yields the same bytes.
- **Nothing is dropped silently.** Every construct either direction cannot carry
  is reported as an :class:`ExchangeLoss`. On import, every element and
  attribute of the document that the mapper did not consume is reported
  mechanically (``UNHANDLED`` / ``EXTENSION``), so a construct this module does
  not know about can never vanish without a trace.
- **Identifiers are preserved.** Imported ids are kept verbatim; an id that
  cannot be a snapshot id (longer than 100 characters, or a duplicate) is
  mapped to a deterministic replacement and the mapping is reported. Export
  rewrites ids that are not XML ``NCName`` values (``xs:ID``) with an ``id-``
  prefix, as Archi does, and reports each rewrite.
- **Import is idempotent**: same bytes, same result.
- **Import never repairs the model.** Invalid relationships are retained and
  surface as findings from :func:`tools.architecture.archimate_rules.validate`.
- **Hardened parsing**: no DTD loading, no entity resolution, no network, no
  huge trees; any DOCTYPE is refused; oversize input is refused before parsing.

Coordinate conventions
----------------------
Snapshot ``Bounds`` are relative to the parent occurrence (Archi). The exchange
format uses absolute diagram coordinates for nested nodes, so export adds the
parent offsets and import subtracts them. Bendpoints are absolute diagram
coordinates on both sides. A ``w``/``h`` of -1 ("default size") is written as
Archi's default figure size (120x55) and reported.

View objects, connections and folders
-------------------------------------
==================================  =============================================
snapshot                            exchange
==================================  =============================================
object ``kind="element"``           ``node xsi:type="Element" elementRef``;
                                    ``label`` (label expression) -> ``<label>``,
                                    which the standard defines as overriding the
                                    element's name on that occurrence
object ``kind="note"``              ``node xsi:type="Label"``; ``text`` -> ``<label>``
object ``kind="group"``             ``node xsi:type="Container"``; ``name`` ->
                                    ``<label>``
note/group ``documentation``        ``<documentation>`` on the node
object ``border_type``              no slot in the format (and Archi rejects
                                    foreign-namespace attributes): one view
                                    property ``archi:borderType`` per object,
                                    value ``<node identifier>=<border type>``
connection ``kind="relationship"``  ``connection xsi:type="Relationship"``
connection ``kind="line"``          ``connection xsi:type="Line"`` (no
                                    ``relationshipRef``)
connection ``label``                ``<label>`` on the connection
``organizations``                   ``<organizations>`` folder tree: ``id`` ->
                                    ``identifier``, ``name`` -> ``<label>``,
                                    ``documentation``, ``children`` -> nested
                                    folder items, ``items`` -> ``identifierRef``
==================================  =============================================

``organizations=None`` exports Archi's default folders (Strategy ... Other,
Relations, Views); an imported tree identical to those is read back as ``None``.
Within a folder, export writes sub-folders before members (as Archi does); the
interleaving of sub-folders and members in an imported file is not kept. A note
cannot contain nodes in the format: an object nested in a note is exported at
the note's parent (``OBJECT-REPARENTED``).

Style vocabulary
----------------
The snapshot ``style`` dict of a view object or connection maps to the exchange
``<style>`` element. These ten keys are the whole vocabulary; any other key is
reported (``STYLE-KEY-UNSUPPORTED``) and an unrepresentable value is reported
(``STYLE-VALUE-INVALID``), never guessed:

=================  ==============================  ===============================
key                value                           exchange representation
=================  ==============================  ===============================
``fill_color``     ``"#rrggbb"``                   ``fillColor`` r/g/b
``fill_alpha``     int 0-100 (needs fill_color)    ``fillColor/@a``
``line_color``     ``"#rrggbb"``                   ``lineColor`` r/g/b
``line_alpha``     int 0-100 (needs line_color)    ``lineColor/@a``
``line_width``     int > 0                         ``style/@lineWidth``
``font_name``      str                             ``font/@name``
``font_size``      number, granularity 0.5         ``font/@size``
``font_style``     list of ``plain``, ``bold``,    ``font/@style``
                   ``italic``, ``underline``       (space separated)
``font_color``     ``"#rrggbb"``                   ``font/color`` r/g/b
``font_alpha``     int 0-100 (needs font_color)    ``font/color/@a``
=================  ==============================  ===============================

Loss codes (stable)
-------------------
Every :class:`ExchangeLoss` carries one of these codes; :data:`LOSS_CODES` is the
same list. Codes are part of the contract: they are never renamed or reused.

Export (snapshot -> exchange):

- ``ID-REWRITTEN`` -- an id is not a unique XML NCName; written with a new id.
- ``TEXT-INVALID-CHARS`` -- characters XML 1.0 cannot carry were removed.
- ``EXPORT-TYPE-UNKNOWN`` -- element type is not an ArchiMate 3.2 concept; not written.
- ``EXPORT-REL-TYPE-UNKNOWN`` -- relationship type unknown; not written.
- ``EXPORT-DANGLING`` -- relationship, view object, connection or folder member
  whose end/target was not written; not written.
- ``JUNCTION-TYPE-INVALID`` -- junction_type is not and/or; written as And.
- ``ATTR-NOT-APPLICABLE`` -- junction_type/access_type/strength/directed on a
  type that cannot carry it, or label/text/name on an object kind that does not
  carry that field; omitted.
- ``ACCESS-TYPE-INVALID`` -- access_type outside write/read/access/readwrite; omitted.
- ``CONCEPT-PARENT`` -- ``parent_id`` has no exchange representation.
- ``VIEWPOINT-UNKNOWN`` -- viewpoint has no standard exchange name (also on import).
- ``STYLE-KEY-UNSUPPORTED`` / ``STYLE-VALUE-INVALID`` -- see the style vocabulary.
- ``BOUNDS-MISSING`` / ``BOUNDS-DEFAULT-SIZE`` / ``BOUNDS-CLAMPED`` /
  ``BENDPOINT-CLAMPED`` -- geometry the format cannot carry as given.
- ``OBJECT-REPARENTED`` -- nesting parent unresolved, not written, a note, or cyclic.
- ``PROPERTY-KEY-RESERVED`` -- a view property named ``archi:borderType``.
- ``EXCHANGE-CONTENT-INVALID`` -- stored metadata or a viewpoint definition that is not
  exchange XML; not written.

Import (exchange -> snapshot):

- ``XSD-INVALID`` -- one schema error of a document imported non-strictly.
- ``ID-REMAPPED`` -- identifier missing, longer than 100 characters or duplicate.
- ``LANG-EXTRA`` -- text in a second language (one language per text is kept).
- ``TEXT-TRUNCATED`` -- name or influence modifier longer than the snapshot allows.
- ``VALUE-MISSING`` / ``VALUE-INVALID`` -- required number missing or not
  parseable, a colour component outside 0-255 or an alpha outside 0-100 (that
  colour, resp. alpha, is dropped), or an ``archi:borderType`` view property that
  names no node.
- ``TYPE-UNKNOWN`` -- unknown element/relationship/node/connection/view type.
- ``MODEL-VERSION`` -- the model's ``version`` attribute.
- ``PROPERTY-TYPE`` / ``PROPERTY-DOCUMENTATION`` / ``PROPERTY-DEF-UNRESOLVED`` /
  ``PROPERTY-DEF-UNUSED`` -- property definitions the snapshot cannot carry.
- ``ORGANIZATION`` -- a folder reference outside any folder, or text or
  content on a folder reference.
- ``VIEWPOINT-REF`` -- a ``viewpointRef`` naming no viewpoint of the document.
- ``VIEW-REF`` -- diagram-model reference on a connection, beyond the first on a node,
  on a node that is not kept, or naming no view.

Retired (no longer reported; kept in :data:`LOSS_CODES`, never reused):
``MODEL-PROPERTIES``, ``MODEL-METADATA``, ``VIEWPOINT-DEFINITION`` -- model properties,
metadata and custom viewpoint definitions are kept (snapshot ``exchange``), views keep
their ``viewpointRef`` (``viewpoint_ref``) and nodes their first ``viewRef``
(``view_ref``).
- ``NODE-DOCUMENTATION`` -- documentation on an element occurrence (notes and
  groups keep theirs; an occurrence shows its concept's).
- ``LABEL-CONCEPT-REF`` -- a Label node's ``conceptRef``/``xpathPart`` binding.
- ``CONNECTION-DANGLING`` -- connection attached to something not kept.
- ``CONNECTION-ATTACHMENT`` -- source/target attachment points.
- ``CONNECTION-DOCUMENTATION`` -- documentation on a connection.
- ``NESTING-FLAG`` -- NestingRelationship marker (the connection is kept).
- ``UNHANDLED`` / ``EXTENSION`` -- any element or attribute, standard or vendor,
  the mapper did not consume.

An import loss names the construct by ``xpath`` and by ``id``: the construct's
own identifier or, for one without (a ``viewRef``, a folder, a label), that of its
nearest identified ancestor.

Interoperability with Archi 5.10.0
----------------------------------
Measured by round-tripping through the official Archi 5.10.0 command line
(``tests/fixtures/exchange/SOURCE.md``). Archi validated and imported every export
tried, and its re-exports import here with no loss beyond the ones listed below.
What Archi itself changes, so that a snapshot that went through Archi differs from
the one that went in:

- Names and property values are made single-line with whitespace runs collapsed
  and trimmed; documentation loses trailing whitespace.
- Every node and connection gets an explicit style (Archi's effective defaults:
  fill/line colour, alpha 100, font name/size/colour).
- ``font_size`` is truncated to an integer; ``underline`` and ``font_alpha`` are
  dropped; ``line_width`` on an element node is dropped (kept on connections).
- ``isDirected="false"`` (the XSD default) is omitted, so ``directed=False``
  comes back as ``None``.
- Connections are re-ordered (grouped by source occurrence); ids are unchanged.
- A property with an empty key is dropped (its definition is still written).
- A connection between a nested occurrence and its parent is dropped on export
  (Archi hides such connections); a connection attached to it then dangles and
  Archi's file is XSD-invalid (``XSD-INVALID`` + ``CONNECTION-DANGLING`` here).
- Archi writes its "specialization" (profile) as an ordinary property named
  ``Specialization``; it is imported as that property.
- Archi neither reads nor writes ``<label>`` on an element occurrence or a
  connection: ``label`` does not survive Archi (Archi's own label expressions are
  not exported either).
- Archi's import drops a line between two element occurrences and a line attached
  to a connection; lines from or to a note or group survive.
- Notes, groups, their text and group documentation survive; border types
  survive as the ``archi:borderType`` view properties (Archi keeps them as plain
  properties). Archi does not export a note's documentation.
- Folders (with ``--xmlexchange.exportFolders``): Archi keeps every concept under
  its type's top-level folder, so a custom tree is split across Business /
  Relations / Views etc. (a top-level folder named like Archi's merges into it),
  folder identifiers are dropped, unfiled concepts land in the default folders,
  and element/relationship order follows the folders. Archi's default folders
  read back as ``organizations=None``.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from schemas.modeling import (
    ArchimateConceptIn,
    ArchimateExchangeContent,
    ArchimateOrganization,
    ArchimateRelationshipIn,
    ArchimateSnapshot,
    ArchimateViewConnectionIn,
    ArchimateViewIn,
    ArchimateViewObjectIn,
    ArchimateViewpointDefinition,
    Bounds,
    Finding,
    Point,
    ValidationReport,
)
from tools.architecture.archimate_rules import validate
from tools.architecture.registry import ACCESS_TYPES, JUNCTION_TYPES, get_registry
from tools.rendering.svg import clean_text

NS = "http://www.opengroup.org/xsd/archimate/3.0/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DC_NS = "http://purl.org/dc/elements/1.1/"
SCHEMA_LOCATION = f"{NS} http://www.opengroup.org/xsd/archimate/3.1/archimate3_Diagram.xsd"
XSD_DIR = Path(__file__).resolve().parents[2] / "data" / "archimate" / "exchange" / "3.1"

DEFAULT_MAX_BYTES = 25 * 1024 * 1024
ARCHI_DEFAULT_W = 120
ARCHI_DEFAULT_H = 55
_MAX_ID = 100
_MAX_NAME = 300
_MAX_STRENGTH = 50

_LANG = f"{{{XML_NS}}}lang"
_XSI_TYPE = f"{{{XSI}}}type"
_XSI_SCHEMA_LOCATION = f"{{{XSI}}}schemaLocation"

_ACCESS_TO_XML = {"write": "Write", "read": "Read", "access": "Access", "readwrite": "ReadWrite"}
_ACCESS_FROM_XML = {v: k for k, v in _ACCESS_TO_XML.items()}

# ViewpointsEnum of archimate3_View.xsd (3.1). Only these names are standard.
EXCHANGE_VIEWPOINTS = frozenset(
    {
        "Organization",
        "Application Platform",
        "Application Structure",
        "Information Structure",
        "Technology",
        "Layered",
        "Physical",
        "Product",
        "Application Usage",
        "Technology Usage",
        "Business Process Cooperation",
        "Application Cooperation",
        "Service Realization",
        "Implementation and Deployment",
        "Goal Realization",
        "Goal Contribution",
        "Principles",
        "Requirements Realization",
        "Motivation",
        "Strategy",
        "Capability Map",
        "Outcome Realization",
        "Resource Map",
        "Value Stream",
        "Project",
        "Migration",
        "Implementation and Migration",
        "Stakeholder",
    }
)

# Archi's default top-level folders, in Archi's order.
_FOLDERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Strategy", ("strategy",)),
    ("Business", ("business",)),
    ("Application", ("application",)),
    ("Technology & Physical", ("technology", "physical")),
    ("Motivation", ("motivation",)),
    ("Implementation & Migration", ("implementation_migration",)),
    ("Other", ("other",)),
)

# View property that carries view objects' border types (the exchange format has
# no slot for them, and Archi rejects foreign-namespace attributes). One property
# per object: value "<exchange node identifier>=<border type>".
BORDER_TYPE_PROPERTY = "archi:borderType"

_STYLE_KEYS = (
    "fill_color",
    "fill_alpha",
    "line_color",
    "line_alpha",
    "line_width",
    "font_name",
    "font_size",
    "font_style",
    "font_color",
    "font_alpha",
)
_FONT_STYLES = ("plain", "bold", "italic", "underline")
_HEX = re.compile(r"^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$")
_NCNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9._\-]*$")
_FONT_SIZE = re.compile(r"^[1-9][0-9]*(\.0|\.5)?$")

# The stable loss-code list documented in the module docstring.
LOSS_CODES = frozenset(
    {
        # export
        "ID-REWRITTEN",
        "TEXT-INVALID-CHARS",
        "EXPORT-TYPE-UNKNOWN",
        "EXPORT-REL-TYPE-UNKNOWN",
        "EXPORT-DANGLING",
        "JUNCTION-TYPE-INVALID",
        "ATTR-NOT-APPLICABLE",
        "ACCESS-TYPE-INVALID",
        "CONCEPT-PARENT",
        "VIEWPOINT-UNKNOWN",
        "STYLE-KEY-UNSUPPORTED",
        "STYLE-VALUE-INVALID",
        "BOUNDS-MISSING",
        "BOUNDS-DEFAULT-SIZE",
        "BOUNDS-CLAMPED",
        "BENDPOINT-CLAMPED",
        "OBJECT-REPARENTED",
        "PROPERTY-KEY-RESERVED",
        "EXCHANGE-CONTENT-INVALID",
        # import
        "XSD-INVALID",
        "ID-REMAPPED",
        "LANG-EXTRA",
        "TEXT-TRUNCATED",
        "VALUE-MISSING",
        "VALUE-INVALID",
        "TYPE-UNKNOWN",
        "MODEL-VERSION",
        "PROPERTY-TYPE",
        "PROPERTY-DOCUMENTATION",
        "PROPERTY-DEF-UNRESOLVED",
        "PROPERTY-DEF-UNUSED",
        "ORGANIZATION",
        "VIEWPOINT-REF",
        "VIEW-REF",
        "NODE-DOCUMENTATION",
        "LABEL-CONCEPT-REF",
        "CONNECTION-DOCUMENTATION",
        "CONNECTION-DANGLING",
        "CONNECTION-ATTACHMENT",
        "NESTING-FLAG",
        "UNHANDLED",
        "EXTENSION",
    }
)


class ExchangeError(ValueError):
    """The input cannot be imported at all (malformed, hostile, oversize)."""


class ExchangeLoss(BaseModel):
    """One construct that did not survive the transform, and why."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="Stable loss code, e.g. ORGANIZATION, TYPE-UNKNOWN")
    id: str | None = Field(None, description="Identifier of the affected construct, if any")
    xpath: str | None = Field(None, description="Location in the exchange document, if any")
    message: str


class ExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: bytes
    losses: list[ExchangeLoss] = Field(default_factory=list)
    id_map: dict[str, str] = Field(
        default_factory=dict, description="Snapshot id -> exchange id, rewritten ids only"
    )


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: ArchimateSnapshot
    model_id: str
    model_name: str
    model_documentation: str | None = None
    language: str | None = Field(None, description="Language chosen for multilingual text")
    id_map: dict[str, str] = Field(
        default_factory=dict, description="Exchange id -> snapshot id for every imported id"
    )
    losses: list[ExchangeLoss] = Field(default_factory=list)
    schema_valid: bool = Field(..., description="Document validated against the pinned XSDs")
    findings: list[Finding] = Field(default_factory=list)
    validation: ValidationReport


# ---------------------------------------------------------------------------
# Schema and parser
# ---------------------------------------------------------------------------


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        remove_blank_text=False,
        collect_ids=False,
    )


_SCHEMA_LOCAL = threading.local()


def exchange_schema() -> etree.XMLSchema:
    """The pinned 3.1 Diagram schema (which includes View and Model) plus Dublin Core.

    One compiled schema PER THREAD: an ``XMLSchema`` keeps its ``error_log`` on the
    instance and lxml releases the GIL while validating, so a schema shared by the
    threadpool lets one request read another request's errors (a cross-tenant leak
    into stored findings). Use :func:`_xsd_validate`, which reads the log at once.
    """
    schema = getattr(_SCHEMA_LOCAL, "schema", None)
    if schema is None:
        doc = etree.parse(str(XSD_DIR / "archimate3_Diagram.xsd"), _parser())
        imp = etree.Element(
            "{http://www.w3.org/2001/XMLSchema}import", namespace=DC_NS, schemaLocation="dc.xsd"
        )
        doc.getroot().insert(0, imp)
        schema = etree.XMLSchema(doc)
        _SCHEMA_LOCAL.schema = schema
    return schema


@dataclass(frozen=True)
class _XsdError:
    line: int
    path: str | None
    message: str


def _xsd_validate(tree: etree._ElementTree) -> list[_XsdError]:
    """Validate against this thread's schema; errors copied before anything else runs."""
    schema = exchange_schema()
    if schema.validate(tree):
        return []
    return [_XsdError(line=e.line, path=e.path, message=e.message) for e in schema.error_log]


def schema_errors(data: bytes) -> list[str]:
    """XSD errors for an exchange document (empty list = valid). Hardened parse."""
    tree = _parse(data, DEFAULT_MAX_BYTES * 8)
    return [f"line {e.line}: {e.message}" for e in _xsd_validate(tree)]


def _parse(data: bytes, max_bytes: int) -> etree._ElementTree:
    if not isinstance(data, bytes | bytearray):
        raise ExchangeError("exchange input must be bytes")
    if len(data) > max_bytes:
        raise ExchangeError(f"exchange input is {len(data)} bytes; the limit is {max_bytes}")
    try:
        tree = etree.ElementTree(etree.fromstring(bytes(data), _parser()))
    except etree.XMLSyntaxError as exc:
        raise ExchangeError(f"not well-formed XML: {exc}") from None
    info = tree.docinfo
    if info.doctype or info.internalDTD is not None or info.externalDTD is not None:
        raise ExchangeError("DOCTYPE declarations are not accepted in exchange files")
    return tree


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


@dataclass
class _ExportCtx:
    language: str
    losses: list[ExchangeLoss] = field(default_factory=list)
    xml_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    used: set[str] = field(default_factory=set)
    id_map: dict[str, str] = field(default_factory=dict)
    view_ids: set[str] = field(default_factory=set)
    viewpoint_ids: set[str] = field(default_factory=set)

    def loss(self, code: str, ident: str | None, message: str) -> None:
        self.losses.append(ExchangeLoss(code=code, id=ident, xpath=None, message=message))

    def fragment(self, xml: str, tag: str, ident: str) -> etree._Element | None:
        """A stored exchange-XML fragment, or None (reported) if it is not a ``tag``."""
        try:
            el = etree.fromstring(xml.encode(), _parser())
        except etree.XMLSyntaxError:
            el = None
        if el is None or el.tag != _q(tag):
            self.loss("EXCHANGE-CONTENT-INVALID", ident, f"stored {tag} is not exchange XML")
            return None
        return el

    def xml_id(self, kind: str, ident: str) -> str:
        """Allocate the document-wide unique ``xs:ID`` for a snapshot id."""
        key = (kind, ident)
        if key in self.xml_ids:
            return self.xml_ids[key]
        cand = ident if _NCNAME.match(ident) else f"id-{ident}"
        if not _NCNAME.match(cand):
            cand = "id-" + re.sub(r"[^A-Za-z0-9._\-]", "_", ident)
        base, n = cand, 1
        while cand in self.used:
            n += 1
            cand = f"{base}-{n}"
        self.used.add(cand)
        self.xml_ids[key] = cand
        if cand != ident:
            self.id_map[ident] = cand
            self.loss(
                "ID-REWRITTEN",
                ident,
                f"{kind} id {ident!r} is not a unique XML NCName; exported as {cand!r}",
            )
        return cand

    def text(self, value: str, ident: str | None) -> str:
        cleaned = clean_text(value)
        if cleaned != value:
            self.loss("TEXT-INVALID-CHARS", ident, "characters not allowed in XML were removed")
        return cleaned

    def lang_el(self, parent: etree._Element, tag: str, value: str, ident: str | None) -> None:
        el = etree.SubElement(parent, _q(tag))
        el.set(_LANG, self.language)
        el.text = self.text(value, ident)


def _add_doc(ctx: _ExportCtx, parent: etree._Element, doc: str | None, ident: str) -> None:
    if doc:
        ctx.lang_el(parent, "documentation", doc, ident)


def _add_props(
    ctx: _ExportCtx,
    parent: etree._Element,
    props: Iterable[tuple[str, str]] | None,
    defs: dict[str, str],
    ident: str,
) -> None:
    if not props:
        return
    wrap = etree.SubElement(parent, _q("properties"))
    for key, value in props:
        p = etree.SubElement(wrap, _q("property"), propertyDefinitionRef=defs[key])
        ctx.lang_el(p, "value", value, ident)


def _rgb(ctx: _ExportCtx, parent: etree._Element, tag: str, style: dict, prefix: str, ident: str):
    color = style.get(f"{prefix}_color")
    if color is None:
        return
    m = _HEX.match(str(color))
    if not m:
        ctx.loss("STYLE-VALUE-INVALID", ident, f"{prefix}_color {color!r} is not #rrggbb")
        return
    el = etree.SubElement(parent, _q(tag))
    el.set("r", str(int(m.group(1), 16)))
    el.set("g", str(int(m.group(2), 16)))
    el.set("b", str(int(m.group(3), 16)))
    alpha = style.get(f"{prefix}_alpha")
    if alpha is not None:
        if isinstance(alpha, int) and not isinstance(alpha, bool) and 0 <= alpha <= 100:
            el.set("a", str(alpha))
        else:
            ctx.loss("STYLE-VALUE-INVALID", ident, f"{prefix}_alpha {alpha!r} is not 0-100")


def _add_style(ctx: _ExportCtx, parent: etree._Element, style: dict | None, ident: str) -> None:
    if not style:
        return
    for key in sorted(set(style) - set(_STYLE_KEYS)):
        ctx.loss(
            "STYLE-KEY-UNSUPPORTED",
            ident,
            f"style key {key!r} has no exchange-format representation",
        )
    for prefix in ("fill", "line", "font"):
        if f"{prefix}_alpha" in style and f"{prefix}_color" not in style:
            ctx.loss("STYLE-VALUE-INVALID", ident, f"{prefix}_alpha without {prefix}_color")
    el = etree.Element(_q("style"))
    width = style.get("line_width")
    if width is not None:
        if isinstance(width, int) and not isinstance(width, bool) and width > 0:
            el.set("lineWidth", str(width))
        else:
            ctx.loss("STYLE-VALUE-INVALID", ident, f"line_width {width!r} is not a positive int")
    _rgb(ctx, el, "lineColor", style, "line", ident)
    _rgb(ctx, el, "fillColor", style, "fill", ident)
    font_keys = ("font_name", "font_size", "font_style", "font_color")
    if any(style.get(k) is not None for k in font_keys):
        font = etree.SubElement(el, _q("font"))
        if style.get("font_name") is not None:
            font.set("name", ctx.text(str(style["font_name"]), ident))
        size = style.get("font_size")
        if size is not None:
            text = _fmt_size(size)
            if text is None:
                ctx.loss("STYLE-VALUE-INVALID", ident, f"font_size {size!r} is not representable")
            else:
                font.set("size", text)
        fs = style.get("font_style")
        if fs is not None:
            items = [fs] if isinstance(fs, str) else list(fs)
            ok = [s for s in items if s in _FONT_STYLES]
            if len(ok) != len(items):
                ctx.loss("STYLE-VALUE-INVALID", ident, f"font_style {fs!r} has unknown values")
            if ok:
                font.set("style", " ".join(ok))
        _rgb(ctx, font, "color", style, "font", ident)
    if len(el) or el.attrib:
        parent.append(el)


def _fmt_size(size: Any) -> str | None:
    if isinstance(size, bool) or not isinstance(size, int | float):
        return None
    if float(size) == int(size):
        text = str(int(size))
    else:
        text = f"{float(size):.1f}"
    return text if _FONT_SIZE.match(text) else None


def default_organizations(snapshot: ArchimateSnapshot) -> list[ArchimateOrganization]:
    """Archi's default folder tree for a snapshot: one folder per Archi top-level
    folder that has members, then Relations and Views. Unknown types are left out."""
    reg = get_registry()
    label_of_layer = {layer: label for label, layers in _FOLDERS for layer in layers}
    members: dict[str, list[str]] = {}
    for c in snapshot.concepts:
        concept = reg.concepts.get(c.type)
        if concept is not None:
            members.setdefault(label_of_layer.get(concept.layer, "Other"), []).append(c.id)
    rels = [r.id for r in snapshot.relationships if r.type in reg.relationship_types]
    out = [
        ArchimateOrganization(name=label, items=members[label])
        for label, _ in _FOLDERS
        if members.get(label)
    ]
    if rels:
        out.append(ArchimateOrganization(name="Relations", items=rels))
    if snapshot.views:
        out.append(ArchimateOrganization(name="Views", items=[v.id for v in snapshot.views]))
    return out


def _walk_folders(folders: list[ArchimateOrganization]) -> Iterable[ArchimateOrganization]:
    for f in folders:
        yield f
        yield from _walk_folders(f.children)


def _write_folder(
    ctx: _ExportCtx,
    parent: etree._Element,
    folder: ArchimateOrganization,
    written: dict[str, str],
    *,
    explicit: bool,
) -> None:
    """Write one folder: label, documentation, sub-folders, then member references.

    A reference to something that was not written is left out; for an explicit
    folder tree that is reported, for the default tree it is already reported
    on the construct itself."""
    ident = folder.id or folder.name or ""
    item = etree.SubElement(parent, _q("item"))
    if folder.id is not None:
        item.set("identifier", ctx.xml_id("folder", folder.id))
    if folder.name is not None:
        ctx.lang_el(item, "label", folder.name, ident)
    _add_doc(ctx, item, folder.documentation, ident)
    for child in folder.children:
        _write_folder(ctx, item, child, written, explicit=explicit)
    for ref in folder.items:
        kind = written.get(ref)
        if kind is None:
            if explicit:
                ctx.loss(
                    "EXPORT-DANGLING",
                    ref,
                    f"folder {folder.name or folder.id!r} member {ref!r} was not exported",
                )
            continue
        etree.SubElement(item, _q("item"), identifierRef=ctx.xml_id(kind, ref))


def export_exchange_report(
    snapshot: ArchimateSnapshot,
    *,
    model_id: str,
    name: str,
    documentation: str | None = None,
    language: str = "en",
) -> ExportResult:
    """Export a snapshot and report every construct the format cannot carry."""
    reg = get_registry()
    ctx = _ExportCtx(language=language)
    # lxml-stubs types nsmap as Mapping[str, str]; lxml's documented default-namespace
    # key is None, which is what the XMI round-trip needs.
    root = etree.Element(_q("model"), nsmap={None: NS, "xsi": XSI})  # ty: ignore[invalid-argument-type]
    root.set(_XSI_SCHEMA_LOCATION, SCHEMA_LOCATION)
    root.set("identifier", ctx.xml_id("model", model_id))
    ctx.lang_el(root, "name", name, model_id)
    _add_doc(ctx, root, documentation, model_id)
    extra = snapshot.exchange or ArchimateExchangeContent()

    # Property definitions: one per distinct key, first-appearance order.
    keys: list[str] = [k for k, _ in extra.properties or []]
    owners: list[Any] = list(snapshot.concepts) + list(snapshot.relationships)
    owners += list(snapshot.views or []) + list(extra.viewpoints)
    for owner in owners:
        reserved_here = isinstance(owner, ArchimateViewIn)
        for k, _ in owner.properties or []:
            if k not in keys and not (reserved_here and k == BORDER_TYPE_PROPERTY):
                keys.append(k)
    has_borders = any(o.border_type is not None for v in snapshot.views or [] for o in v.objects)
    if has_borders and BORDER_TYPE_PROPERTY not in keys:
        keys.append(BORDER_TYPE_PROPERTY)
    keys = list(dict.fromkeys(keys))
    reserved = {c.id for c in owners}
    reserved |= {f.id for f in _walk_folders(snapshot.organizations or []) if f.id is not None}
    for v in snapshot.views or []:
        reserved |= {o.id for o in v.objects} | {c.id for c in v.connections}
    defs: dict[str, str] = {}
    n = 0
    for k in keys:
        n += 1
        cand = f"propid-{n}"
        while cand in reserved:
            cand = f"propid-{n}-x"
            n += 1
        defs[k] = cand

    # Pre-allocate ids in document order so rewrites are stable.
    concept_ok: dict[str, str] = {}  # snapshot id -> exchange xsi:type
    for c in snapshot.concepts:
        ctx.xml_id("concept", c.id)
    for r in snapshot.relationships:
        ctx.xml_id("concept", r.id)
    for k in keys:
        ctx.used.add(defs[k])
    for vp in extra.viewpoints:
        ctx.xml_id("viewpoint", vp.id)
    _add_props(ctx, root, extra.properties, defs, model_id)
    if (
        extra.metadata is not None
        and (md := ctx.fragment(extra.metadata, "metadata", model_id)) is not None
    ):
        root.append(md)

    elements = etree.Element(_q("elements"))
    for c in snapshot.concepts:
        concept = reg.concepts.get(c.type)
        if concept is None:
            ctx.loss(
                "EXPORT-TYPE-UNKNOWN",
                c.id,
                f"element type {c.type!r} is not an ArchiMate 3.2 concept; element not exported",
            )
            continue
        if c.type == "junction":
            jt = c.junction_type or "and"
            if jt not in JUNCTION_TYPES:
                ctx.loss("JUNCTION-TYPE-INVALID", c.id, f"junction_type {jt!r} exported as And")
                jt = "and"
            xtype = "AndJunction" if jt == "and" else "OrJunction"
        else:
            xtype = concept.archi_class
            if c.junction_type is not None:
                ctx.loss("ATTR-NOT-APPLICABLE", c.id, "junction_type on a non-junction")
        if c.parent_id is not None:
            ctx.loss(
                "CONCEPT-PARENT",
                c.id,
                f"organisational parent {c.parent_id!r} has no exchange-format representation",
            )
        el = etree.SubElement(elements, _q("element"))
        el.set("identifier", ctx.xml_id("concept", c.id))
        el.set(_XSI_TYPE, xtype)
        if c.name or c.type != "junction":
            ctx.lang_el(el, "name", c.name, c.id)
        _add_doc(ctx, el, c.documentation, c.id)
        _add_props(ctx, el, c.properties, defs, c.id)
        concept_ok[c.id] = xtype

    # Relationships may reference relationships; resolve to a fixpoint so a
    # relationship whose end was not exported is itself not exported.
    rel_by_id = {r.id: r for r in snapshot.relationships}
    exported_rels: set[str] = set()
    pending = [r for r in snapshot.relationships]
    for r in pending:
        if r.type not in reg.relationship_types:
            ctx.loss(
                "EXPORT-REL-TYPE-UNKNOWN",
                r.id,
                f"relationship type {r.type!r} is not an ArchiMate relationship; not exported",
            )
    candidates = {r.id for r in pending if r.type in reg.relationship_types}
    changed = True
    while changed:
        changed = False
        for rid in sorted(candidates):
            r = rel_by_id[rid]
            ends_ok = all(e in concept_ok or e in candidates for e in (r.source, r.target))
            if not ends_ok:
                candidates.discard(rid)
                changed = True
    for r in snapshot.relationships:
        if r.type in reg.relationship_types and r.id not in candidates:
            ctx.loss(
                "EXPORT-DANGLING",
                r.id,
                f"relationship end {r.source!r} or {r.target!r} is unresolved or not exported",
            )
    relationships = etree.Element(_q("relationships"))
    for r in snapshot.relationships:
        if r.id not in candidates:
            continue
        el = etree.SubElement(relationships, _q("relationship"))
        el.set("identifier", ctx.xml_id("concept", r.id))
        el.set("source", ctx.xml_id("concept", r.source))
        el.set("target", ctx.xml_id("concept", r.target))
        el.set(_XSI_TYPE, r.type.capitalize())
        if r.access_type is not None:
            if r.type != "access":
                ctx.loss("ATTR-NOT-APPLICABLE", r.id, "access_type on a non-access relationship")
            elif r.access_type not in ACCESS_TYPES:
                ctx.loss("ACCESS-TYPE-INVALID", r.id, f"access_type {r.access_type!r} omitted")
            else:
                el.set("accessType", _ACCESS_TO_XML[r.access_type])
        if r.strength is not None:
            if r.type != "influence":
                ctx.loss("ATTR-NOT-APPLICABLE", r.id, "strength on a non-influence relationship")
            else:
                el.set("modifier", ctx.text(r.strength, r.id))
        if r.directed is not None:
            if r.type != "association":
                ctx.loss("ATTR-NOT-APPLICABLE", r.id, "directed on a non-association")
            else:
                el.set("isDirected", "true" if r.directed else "false")
        if r.name:
            ctx.lang_el(el, "name", r.name, r.id)
        _add_doc(ctx, el, r.documentation, r.id)
        _add_props(ctx, el, r.properties, defs, r.id)
        exported_rels.add(r.id)

    if len(elements):
        root.append(elements)
    if len(relationships):
        root.append(relationships)

    views = list(snapshot.views or [])
    for v in views:
        ctx.xml_id("view", v.id)
    for v in views:
        for o in v.objects:
            ctx.xml_id("node", o.id)
        for c in v.connections:
            ctx.xml_id("node", c.id)

    # Organizations: the snapshot's folder tree, or Archi's default folders.
    written = {i: "concept" for i in concept_ok} | {i: "concept" for i in candidates}
    written |= {v.id: "view" for v in views}
    orgs = etree.Element(_q("organizations"))
    if snapshot.organizations is None:
        for folder in default_organizations(snapshot):
            _write_folder(ctx, orgs, folder, written, explicit=False)
    else:
        for folder in _walk_folders(snapshot.organizations):
            if folder.id is not None:
                ctx.xml_id("folder", folder.id)
        for folder in snapshot.organizations:
            _write_folder(ctx, orgs, folder, written, explicit=True)
    if len(orgs):
        root.append(orgs)

    if defs:
        pd = etree.SubElement(root, _q("propertyDefinitions"))
        for k in keys:
            d = etree.SubElement(pd, _q("propertyDefinition"), identifier=defs[k], type="string")
            ctx.lang_el(d, "name", k, defs[k])

    viewpoints = etree.Element(_q("viewpoints"))
    for vp in extra.viewpoints:
        el = ctx.fragment(vp.xml, "viewpoint", vp.id)
        if el is None:
            continue
        el.set("identifier", ctx.xml_id("viewpoint", vp.id))
        # Properties follow name and documentation (NamedReferenceableType).
        head = sum(1 for c in el if c.tag in (_q("name"), _q("documentation")))
        _add_props(ctx, el, vp.properties, defs, vp.id)
        if vp.properties:
            el.insert(head, el[-1])
        viewpoints.append(el)
        ctx.viewpoint_ids.add(vp.id)
    if views or len(viewpoints):
        vs = etree.SubElement(root, _q("views"))
        if len(viewpoints):
            vs.append(viewpoints)
        ctx.view_ids = {v.id for v in views}
        if views:
            diagrams = etree.SubElement(vs, _q("diagrams"))
            vp_names = {vp.id: vp.name for vp in reg.viewpoints.values()}
            for v in views:
                _export_view(ctx, diagrams, v, concept_ok, exported_rels, defs, vp_names)

    xsd_errors = _xsd_validate(etree.ElementTree(root))
    if xsd_errors:
        errors = "; ".join(f"line {e.line}: {e.message}" for e in xsd_errors)
        raise AssertionError(f"exchange export is not XSD-valid (bug): {errors}")
    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
    return ExportResult(data=data, losses=ctx.losses, id_map=ctx.id_map)


def _export_view(
    ctx: _ExportCtx,
    diagrams: etree._Element,
    v: ArchimateViewIn,
    concept_ok: dict[str, str],
    exported_rels: set[str],
    defs: dict[str, str],
    vp_names: dict[str, str],
) -> None:
    el = etree.SubElement(diagrams, _q("view"))
    el.set("identifier", ctx.xml_id("view", v.id))
    el.set(_XSI_TYPE, "Diagram")
    if v.viewpoint is not None:
        vp = vp_names.get(v.viewpoint)
        if vp in EXCHANGE_VIEWPOINTS:
            el.set("viewpoint", vp)
        else:
            ctx.loss(
                "VIEWPOINT-UNKNOWN",
                v.id,
                f"viewpoint {v.viewpoint!r} has no standard exchange name; view exported unfiltered",
            )
    if v.viewpoint_ref is not None:
        if v.viewpoint_ref in ctx.viewpoint_ids:
            el.set("viewpointRef", ctx.xml_id("viewpoint", v.viewpoint_ref))
        else:
            ctx.loss("EXPORT-DANGLING", v.id, f"viewpointRef {v.viewpoint_ref!r} not exported")
    ctx.lang_el(el, "name", v.name, v.id)
    _add_doc(ctx, el, v.documentation, v.id)

    objs = {o.id: o for o in v.objects}
    # Element occurrences of exported elements, and every note and group.
    keep = {o.id for o in v.objects if o.kind != "element" or o.concept_id in concept_ok}
    for o in v.objects:
        if o.id not in keep:
            ctx.loss(
                "EXPORT-DANGLING",
                o.id,
                f"view object for {o.concept_id!r} not exported: not an exported element",
            )
        _check_object_attrs(ctx, o)

    # View properties, then one border-type property per object that has one.
    props: list[tuple[str, str]] = []
    for key, value in v.properties or []:
        if key == BORDER_TYPE_PROPERTY:
            ctx.loss(
                "PROPERTY-KEY-RESERVED",
                v.id,
                f"view property {key!r} is reserved for border types; not exported",
            )
        else:
            props.append((key, value))
    for o in v.objects:
        if o.id in keep and o.border_type is not None:
            props.append((BORDER_TYPE_PROPERTY, f"{ctx.xml_id('node', o.id)}={o.border_type}"))
    _add_props(ctx, el, props, defs, v.id)

    def can_contain(pid: str) -> bool:
        # The exchange Label type (a note) has no child nodes.
        return pid in keep and objs[pid].kind != "note"

    def kept_parent(o: ArchimateViewObjectIn) -> str | None:
        seen: set[str] = set()
        p = o.parent_object_id
        while p is not None and p in objs and not can_contain(p) and p not in seen:
            seen.add(p)
            p = objs[p].parent_object_id
        return p if p is not None and can_contain(p) else None

    parent_of: dict[str, str | None] = {}
    for o in v.objects:
        if o.id in keep:
            parent_of[o.id] = kept_parent(o)
            if o.parent_object_id is not None and parent_of[o.id] != o.parent_object_id:
                why = (
                    "a note cannot contain nodes in the exchange format"
                    if o.parent_object_id in objs and objs[o.parent_object_id].kind == "note"
                    else "unresolved or not exported"
                )
                ctx.loss("OBJECT-REPARENTED", o.id, f"parent {o.parent_object_id!r}: {why}")
    # Break cycles: an object is a root unless its ancestor chain reaches a root.
    reachable: set[str] = set()
    for oid in parent_of:
        chain: list[str] = []
        cur: str | None = oid
        while cur is not None and cur not in reachable and cur not in chain:
            chain.append(cur)
            cur = parent_of[cur]
        if cur is not None and cur in chain:
            ctx.loss("OBJECT-REPARENTED", cur, "nesting cycle broken; object placed at top level")
            parent_of[cur] = None
        reachable.update(chain)
    children: dict[str | None, list[ArchimateViewObjectIn]] = {}
    for o in v.objects:
        if o.id in keep:
            children.setdefault(parent_of[o.id], []).append(o)

    def absolute(oid: str) -> tuple[int, int]:
        """Diagram position through the ORIGINAL parent chain, so an object that is
        re-parented on export stays where it was drawn."""
        x = y = 0
        seen: set[str] = set()
        cur: str | None = oid
        while cur is not None and cur in objs and cur not in seen:
            seen.add(cur)
            b = objs[cur].bounds or Bounds()
            x, y = x + b.x, y + b.y
            cur = objs[cur].parent_object_id
        return x, y

    def emit(parent_el: etree._Element, o: ArchimateViewObjectIn) -> None:
        b = o.bounds
        if b is None:
            ctx.loss("BOUNDS-MISSING", o.id, "no bounds; exported at the parent origin")
            b = Bounds()
        ax, ay = absolute(o.id)
        w, h = b.w, b.h
        if w <= 0 or h <= 0:
            ctx.loss("BOUNDS-DEFAULT-SIZE", o.id, "default/unsized figure exported as 120x55")
            w = ARCHI_DEFAULT_W if w <= 0 else w
            h = ARCHI_DEFAULT_H if h <= 0 else h
        if ax < 0 or ay < 0:
            ctx.loss("BOUNDS-CLAMPED", o.id, f"absolute position ({ax},{ay}) clamped to >= 0")
        node = etree.SubElement(parent_el, _q("node"))
        node.set("identifier", ctx.xml_id("node", o.id))
        if o.kind == "element" and o.concept_id is not None:
            node.set("elementRef", ctx.xml_id("concept", o.concept_id))
        node.set(_XSI_TYPE, _NODE_TYPE[o.kind])
        node.set("x", str(max(ax, 0)))
        node.set("y", str(max(ay, 0)))
        node.set("w", str(w))
        node.set("h", str(h))
        text = getattr(o, _OBJECT_TEXT[o.kind])
        if text is not None:
            ctx.lang_el(node, "label", text, o.id)
        _add_doc(ctx, node, o.documentation, o.id)
        _add_style(ctx, node, o.style, o.id)
        if o.view_ref is not None:
            if o.view_ref in ctx.view_ids:
                etree.SubElement(node, _q("viewRef"), ref=ctx.xml_id("view", o.view_ref))
            else:
                ctx.loss("EXPORT-DANGLING", o.id, f"viewRef {o.view_ref!r} names no view")
        for child in children.get(o.id, []):
            emit(node, child)

    for o in children.get(None, []):
        emit(el, o)

    # Connections: endpoints are kept nodes or other kept connections (fixpoint).
    conns = {c.id: c for c in v.connections}
    ok = {c.id for c in v.connections if c.kind == "line" or c.relationship_id in exported_rels}
    changed = True
    while changed:
        changed = False
        for cid in sorted(ok):
            c = conns[cid]
            if not all(e in keep or e in ok for e in (c.source_object_id, c.target_object_id)):
                ok.discard(cid)
                changed = True
    for c in v.connections:
        if c.id not in ok:
            ctx.loss(
                "EXPORT-DANGLING",
                c.id,
                "connection not exported: relationship or an endpoint is unresolved/not exported",
            )
            continue
        ce = etree.SubElement(el, _q("connection"))
        ce.set("identifier", ctx.xml_id("node", c.id))
        if c.kind == "relationship" and c.relationship_id is not None:
            ce.set("relationshipRef", ctx.xml_id("concept", c.relationship_id))
            ce.set(_XSI_TYPE, "Relationship")
        else:
            ce.set(_XSI_TYPE, "Line")
        ce.set("source", ctx.xml_id("node", c.source_object_id))
        ce.set("target", ctx.xml_id("node", c.target_object_id))
        if c.label is not None:
            ctx.lang_el(ce, "label", c.label, c.id)
        _add_style(ctx, ce, c.style, c.id)
        for p in c.bendpoints or []:
            if p.x < 0 or p.y < 0:
                ctx.loss("BENDPOINT-CLAMPED", c.id, f"bendpoint ({p.x},{p.y}) clamped to >= 0")
            etree.SubElement(ce, _q("bendpoint"), x=str(max(p.x, 0)), y=str(max(p.y, 0)))


_NODE_TYPE = {"element": "Element", "note": "Label", "group": "Container"}
# Which text field each object kind carries, as the node's <label>.
_OBJECT_TEXT = {"element": "label", "note": "text", "group": "name"}


def _check_object_attrs(ctx: _ExportCtx, o: ArchimateViewObjectIn) -> None:
    """Text fields a kind does not carry are reported, never written elsewhere."""
    for attr in ("label", "text", "name"):
        if getattr(o, attr) is not None and _OBJECT_TEXT[o.kind] != attr:
            ctx.loss("ATTR-NOT-APPLICABLE", o.id, f"{attr} on a view object of kind {o.kind!r}")


def exchange_content_errors(content: ArchimateExchangeContent) -> list[str]:
    """Why stored exchange ``content`` would not export, if it would not (none = it does)."""
    try:
        result = export_exchange_report(ArchimateSnapshot(exchange=content), model_id="m", name="")
    except AssertionError as exc:  # XSD-invalid output
        return [str(exc)]
    return [x.message for x in result.losses if x.code == "EXCHANGE-CONTENT-INVALID"]


def export_exchange(
    snapshot: ArchimateSnapshot,
    *,
    model_id: str,
    name: str,
    documentation: str | None = None,
    language: str = "en",
) -> bytes:
    """XSD-valid, deterministic exchange XML. Use :func:`export_exchange_report` for losses."""
    return export_exchange_report(
        snapshot, model_id=model_id, name=name, documentation=documentation, language=language
    ).data


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class _ImportCtx:
    tree: etree._ElementTree
    lang: str | None
    losses: list[ExchangeLoss] = field(default_factory=list)
    consumed: dict[Any, set[str]] = field(default_factory=dict)
    id_map: dict[str, str] = field(default_factory=dict)
    snap_ids: set[str] = field(default_factory=set)

    def path(self, el: etree._Element) -> str:
        parts: list[str] = []
        cur: etree._Element | None = el
        while cur is not None:
            name = etree.QName(cur).localname
            parent = cur.getparent()
            if parent is None:
                parts.append(name)
            else:
                same = [s for s in parent if isinstance(s.tag, str) and s.tag == cur.tag]
                idx = same.index(cur) + 1
                parts.append(f"{name}[{idx}]" if len(same) > 1 else name)
            cur = parent
        return "/" + "/".join(reversed(parts))

    def loss(self, code: str, el: etree._Element | None, message: str, ident: str | None = None):
        if ident is None and el is not None:
            # The construct's own identifier, else its nearest identified ancestor's.
            owner: etree._Element | None = el
            while owner is not None and owner.get("identifier") is None:
                owner = owner.getparent()
            ident = owner.get("identifier") if owner is not None else None
        self.losses.append(
            ExchangeLoss(
                code=code,
                id=ident,
                xpath=self.path(el) if el is not None else None,
                message=message,
            )
        )

    def take(self, el: etree._Element, *attrs: str) -> etree._Element:
        self.consumed.setdefault(el, set()).update(attrs)
        return el

    def kids(self, el: etree._Element, tag: str) -> list[etree._Element]:
        return [c for c in el if isinstance(c.tag, str) and c.tag == _q(tag)]

    def pick_text(self, el: etree._Element, tag: str, what: str) -> str | None:
        """Text of the preferred-language child ``tag``; other languages are losses."""
        items = self.kids(el, tag)
        if not items:
            return None
        for it in items:
            self.take(it, _LANG)
        chosen = next((i for i in items if i.get(_LANG) == self.lang), items[0])
        for it in items:
            if it is not chosen:
                self.loss(
                    "LANG-EXTRA",
                    it,
                    f"{what} in language {it.get(_LANG)!r} not kept (one language per text)",
                    ident=el.get("identifier"),
                )
        return chosen.text or ""

    def snap_id(self, el: etree._Element, raw: str | None) -> str:
        """Map an exchange identifier to a unique, schema-valid snapshot id."""
        raw = raw or ""
        sid = raw
        if not raw or len(raw) > _MAX_ID or raw in self.snap_ids:
            digest = hashlib.sha256(f"{raw}\0{self.path(el)}".encode()).hexdigest()[:32]
            sid = f"x-{digest}"
            reason = "missing" if not raw else "too long" if len(raw) > _MAX_ID else "duplicate"
            self.loss("ID-REMAPPED", el, f"identifier {raw!r} is {reason}; mapped to {sid!r}")
        self.snap_ids.add(sid)
        if raw and raw not in self.id_map:
            self.id_map[raw] = sid
        return sid


def _xsi_type(el: etree._Element) -> str:
    raw = el.get(_XSI_TYPE) or ""
    return raw.split(":", 1)[1] if ":" in raw else raw


def _clip(ctx: _ImportCtx, el: etree._Element, text: str | None, limit: int, what: str) -> str:
    text = text or ""
    if len(text) > limit:
        ctx.loss("TEXT-TRUNCATED", el, f"{what} longer than {limit} characters was truncated")
        return text[:limit]
    return text


def _int(ctx: _ImportCtx, el: etree._Element, attr: str, default: int) -> int:
    raw = el.get(attr)
    ctx.take(el, attr)
    if raw is None:
        if default == -1 or attr in ("x", "y"):
            ctx.loss("VALUE-MISSING", el, f"attribute {attr!r} missing; using {default}")
        return default
    try:
        return int(raw)
    except ValueError:
        ctx.loss("VALUE-INVALID", el, f"attribute {attr}={raw!r} is not an integer")
        return default


def _props(
    ctx: _ImportCtx, el: etree._Element, defs: dict[str, str]
) -> list[tuple[str, str]] | None:
    wraps = ctx.kids(el, "properties")
    out: list[tuple[str, str]] = []
    for w in wraps:
        ctx.take(w)
        for p in ctx.kids(w, "property"):
            ctx.take(p, "propertyDefinitionRef")
            ref = p.get("propertyDefinitionRef") or ""
            key = defs.get(ref)
            if key is None:
                ctx.loss("PROPERTY-DEF-UNRESOLVED", p, f"property definition {ref!r} not found")
                key = ref
            out.append((key, ctx.pick_text(p, "value", "property value") or ""))
    return out or None


def _color(ctx: _ImportCtx, el: etree._Element, prefix: str, style: dict[str, Any]) -> None:
    ctx.take(el, "r", "g", "b", "a")
    try:
        r, g, b = (int(el.get(k, "0")) for k in ("r", "g", "b"))
    except ValueError:
        ctx.loss("VALUE-INVALID", el, "colour component is not an integer")
        return
    if not all(0 <= c <= 255 for c in (r, g, b)):
        # ``#{300:02x}`` is ``#12c…`` — not a colour. Report and drop it (and its alpha).
        ctx.loss("VALUE-INVALID", el, f"colour component outside 0-255 (r={r} g={g} b={b})")
        return
    style[f"{prefix}_color"] = f"#{r:02x}{g:02x}{b:02x}"
    if el.get("a") is not None:
        try:
            alpha = int(el.get("a", ""))
        except ValueError:
            ctx.loss("VALUE-INVALID", el, "alpha is not an integer")
            return
        if 0 <= alpha <= 100:
            style[f"{prefix}_alpha"] = alpha
        else:
            ctx.loss("VALUE-INVALID", el, f"alpha {alpha} is outside 0-100")


def _style(ctx: _ImportCtx, el: etree._Element) -> dict[str, Any] | None:
    found = ctx.kids(el, "style")
    if not found:
        return None
    s = ctx.take(found[0], "lineWidth")
    style: dict[str, Any] = {}
    for child in s:
        if not isinstance(child.tag, str):
            continue
        if child.tag == _q("fillColor"):
            _color(ctx, child, "fill", style)
        elif child.tag == _q("lineColor"):
            _color(ctx, child, "line", style)
        elif child.tag == _q("font"):
            ctx.take(child, "name", "size", "style")
            if child.get("name") is not None:
                style["font_name"] = child.get("name")
            if child.get("size") is not None:
                try:
                    size = float(child.get("size", ""))
                    style["font_size"] = int(size) if size.is_integer() else size
                except ValueError:
                    ctx.loss("VALUE-INVALID", child, "font size is not a number")
            if child.get("style") is not None:
                style["font_style"] = child.get("style", "").split()
            for c in ctx.kids(child, "color"):
                _color(ctx, c, "font", style)
    if s.get("lineWidth") is not None:
        try:
            style["line_width"] = int(s.get("lineWidth", ""))
        except ValueError:
            ctx.loss("VALUE-INVALID", s, "lineWidth is not an integer")
    ordered = {k: style[k] for k in _STYLE_KEYS if k in style}
    return ordered or None


def import_exchange(
    data: bytes, *, max_bytes: int = DEFAULT_MAX_BYTES, strict: bool = False
) -> ImportResult:
    """Import an Open Group exchange document into a snapshot.

    The document is validated against the pinned XSDs. ``strict=True`` refuses
    an XSD-invalid document; otherwise every schema error is reported as an
    ``XSD-INVALID`` loss and the mapper keeps everything it can, so a file with
    an unknown ``xsi:type`` is imported with that construct retained and
    reported rather than refused or dropped.
    """
    tree = _parse(data, max_bytes)
    root = tree.getroot()
    if root.tag != _q("model"):
        raise ExchangeError(f"root element is {root.tag!r}, expected {{{NS}}}model")
    xsd_errors = _xsd_validate(tree)
    schema_valid = not xsd_errors
    if not schema_valid and strict:
        errors = "; ".join(f"line {e.line}: {e.message}" for e in xsd_errors)
        raise ExchangeError(f"exchange document is not XSD-valid: {errors}")

    names = [c for c in root if isinstance(c.tag, str) and c.tag == _q("name")]
    lang = names[0].get(_LANG) if names else None
    ctx = _ImportCtx(tree=tree, lang=lang)
    if not schema_valid:
        for e in xsd_errors:
            ctx.losses.append(
                ExchangeLoss(code="XSD-INVALID", id=None, xpath=e.path, message=e.message)
            )
    reg = get_registry()
    by_class = {c.archi_class: c.type for c in reg.concepts.values()}
    rel_types = {t.capitalize(): t for t in reg.relationship_types}
    vp_by_name = {
        vp.name: vp.id for vp in reg.viewpoints.values() if vp.name in EXCHANGE_VIEWPOINTS
    }

    ctx.take(root, "identifier", _XSI_SCHEMA_LOCATION)
    model_id = root.get("identifier") or ""
    model_name = ctx.pick_text(root, "name", "model name") or ""
    model_doc = ctx.pick_text(root, "documentation", "model documentation")
    if root.get("version") is not None:
        ctx.take(root, "version")
        ctx.loss("MODEL-VERSION", root, f"model version {root.get('version')!r} not kept")

    # Property definitions first: properties anywhere refer to them.
    defs: dict[str, str] = {}
    def_used: set[str] = set()
    for pds in ctx.kids(root, "propertyDefinitions"):
        ctx.take(pds)
        for pd in ctx.kids(pds, "propertyDefinition"):
            ctx.take(pd, "identifier", "type")
            defs[pd.get("identifier") or ""] = ctx.pick_text(pd, "name", "property name") or ""
            if pd.get("type") not in (None, "string"):
                ctx.loss(
                    "PROPERTY-TYPE",
                    pd,
                    f"property {defs[pd.get('identifier') or '']!r} type {pd.get('type')!r} "
                    "kept as string",
                )
            if ctx.kids(pd, "documentation"):
                ctx.pick_text(pd, "documentation", "property documentation")
                ctx.loss("PROPERTY-DOCUMENTATION", pd, "property definition documentation dropped")

    model_props = _props(ctx, root, defs)
    _mark_props_used(root, def_used, ctx)
    metadata: str | None = None
    for md in ctx.kids(root, "metadata"):
        metadata = c14n(md)
        _mark_subtree(ctx, md)

    concepts: list[ArchimateConceptIn] = []
    concept_xml_ids: set[str] = set()
    for els in ctx.kids(root, "elements"):
        ctx.take(els)
        for el in ctx.kids(els, "element"):
            ctx.take(el, "identifier", _XSI_TYPE)
            xtype = _xsi_type(el)
            jt: str | None = None
            if xtype in ("AndJunction", "OrJunction"):
                ctype = "junction"
                jt = "and" if xtype == "AndJunction" else "or"
            elif xtype in by_class and by_class[xtype] != "junction":
                ctype = by_class[xtype]
            else:
                ctype = xtype
                ctx.loss(
                    "TYPE-UNKNOWN",
                    el,
                    f"element xsi:type {xtype!r} is not an ArchiMate 3.2 element; "
                    "retained with its raw type",
                )
            raw_id = el.get("identifier")
            concept_xml_ids.add(raw_id or "")
            concepts.append(
                ArchimateConceptIn(
                    id=ctx.snap_id(el, raw_id),
                    type=ctype,
                    name=_clip(ctx, el, ctx.pick_text(el, "name", "name"), _MAX_NAME, "name"),
                    junction_type=jt,
                    documentation=ctx.pick_text(el, "documentation", "documentation"),
                    properties=_props(ctx, el, defs),
                )
            )
            _mark_props_used(el, def_used, ctx)

    relationships: list[ArchimateRelationshipIn] = []
    rel_raw: list[tuple[etree._Element, str]] = []
    for rels in ctx.kids(root, "relationships"):
        ctx.take(rels)
        for el in ctx.kids(rels, "relationship"):
            rel_raw.append((el, ctx.snap_id(el, el.get("identifier"))))
    for el, sid in rel_raw:
        ctx.take(el, "identifier", _XSI_TYPE, "source", "target")
        xtype = _xsi_type(el)
        rtype = rel_types.get(xtype)
        if rtype is None:
            rtype = xtype
            ctx.loss(
                "TYPE-UNKNOWN",
                el,
                f"relationship xsi:type {xtype!r} is not an ArchiMate relationship; "
                "retained with its raw type",
            )
        access = strength = None
        directed: bool | None = None
        if el.get("accessType") is not None:
            ctx.take(el, "accessType")
            access = _ACCESS_FROM_XML.get(el.get("accessType", ""))
            if access is None:
                ctx.loss("VALUE-INVALID", el, f"accessType {el.get('accessType')!r} not kept")
        if el.get("modifier") is not None:
            ctx.take(el, "modifier")
            strength = _clip(ctx, el, el.get("modifier"), _MAX_STRENGTH, "influence modifier")
        if el.get("isDirected") is not None:
            ctx.take(el, "isDirected")
            directed = el.get("isDirected") in ("true", "1")
        relationships.append(
            ArchimateRelationshipIn(
                id=sid,
                type=rtype,
                source=ctx.id_map.get(el.get("source") or "", el.get("source") or ""),
                target=ctx.id_map.get(el.get("target") or "", el.get("target") or ""),
                name=_clip(ctx, el, ctx.pick_text(el, "name", "name"), _MAX_NAME, "name"),
                access_type=access,
                strength=strength,
                directed=directed,
                documentation=ctx.pick_text(el, "documentation", "documentation"),
                properties=_props(ctx, el, defs),
            )
        )
        _mark_props_used(el, def_used, ctx)

    views: list[ArchimateViewIn] = []
    viewpoints: list[ArchimateViewpointDefinition] = []
    for vs in ctx.kids(root, "views"):
        ctx.take(vs)
        for vps in ctx.kids(vs, "viewpoints"):
            ctx.take(vps)
            for vp in ctx.kids(vps, "viewpoint"):
                viewpoints.append(_import_viewpoint(ctx, vp, defs, def_used))
        for diagrams in ctx.kids(vs, "diagrams"):
            ctx.take(diagrams)
            for vel in ctx.kids(diagrams, "view"):
                views.append(_import_view(ctx, vel, defs, def_used, vp_by_name))

    organizations: list[ArchimateOrganization] | None = None
    for orgs in ctx.kids(root, "organizations"):
        ctx.take(orgs)
        organizations = (organizations or []) + _import_orgs(ctx, orgs, top=True)

    for ref, key in defs.items():
        if ref not in def_used:
            ctx.loss(
                "PROPERTY-DEF-UNUSED",
                None,
                f"property definition {key!r} is used by nothing that was kept",
                ident=ref,
            )

    _resolve_view_refs(ctx, views, {vp.id for vp in viewpoints})
    _report_unconsumed(ctx, root)

    exchange = ArchimateExchangeContent(
        properties=model_props, metadata=metadata, viewpoints=viewpoints
    )
    snapshot = ArchimateSnapshot(
        concepts=concepts,
        relationships=relationships,
        views=views,
        exchange=exchange if exchange != ArchimateExchangeContent() else None,
    )
    # Folders identical to the ones export would regenerate are not model content.
    if organizations is not None and organizations != default_organizations(snapshot):
        snapshot = snapshot.model_copy(update={"organizations": organizations})
    report = validate(snapshot)
    return ImportResult(
        snapshot=snapshot,
        model_id=model_id,
        model_name=model_name,
        model_documentation=model_doc,
        language=lang,
        id_map=ctx.id_map,
        losses=ctx.losses,
        schema_valid=schema_valid,
        findings=report.findings,
        validation=report,
    )


def c14n(el: etree._Element) -> str:
    """``el`` as exclusive C14N without whitespace-only text, so that the same content
    reads back as the same string whatever file (and indentation) it came from."""
    copy = deepcopy(el)
    copy.tail = None
    for d in copy.iter():
        if not isinstance(d.tag, str):
            continue
        if d.text is not None and not d.text.strip():
            d.text = None
        if d is not copy and d.tail is not None and not d.tail.strip():
            d.tail = None
    return etree.tostring(copy, method="c14n", exclusive=True).decode()


def _import_viewpoint(
    ctx: _ImportCtx, vp: etree._Element, defs: dict[str, str], def_used: set[str]
) -> ArchimateViewpointDefinition:
    """A custom viewpoint, kept verbatim but for its identifier and properties."""
    ctx.take(vp, "identifier")
    sid = ctx.snap_id(vp, vp.get("identifier"))
    props = _props(ctx, vp, defs)
    _mark_props_used(vp, def_used, ctx)
    _mark_subtree(ctx, vp)
    body = deepcopy(vp)
    body.attrib.pop("identifier", None)
    for wrap in body.findall(_q("properties")):
        body.remove(wrap)
    return ArchimateViewpointDefinition(id=sid, xml=c14n(body), properties=props)


def _resolve_view_refs(ctx: _ImportCtx, views: list[ArchimateViewIn], viewpoints: set[str]):
    """Map viewpoint and drill-down references to snapshot ids (any view may be named
    before it is read); one naming nothing that was kept is reported and dropped."""
    view_ids = {v.id for v in views}
    for v in views:
        if v.viewpoint_ref is not None:
            ref = ctx.id_map.get(v.viewpoint_ref, v.viewpoint_ref)
            if ref not in viewpoints:
                ctx.loss("VIEWPOINT-REF", None, f"viewpointRef {ref!r} names no viewpoint", v.id)
                ref = None
            v.viewpoint_ref = ref
        for o in v.objects:
            if o.view_ref is not None:
                ref = ctx.id_map.get(o.view_ref, o.view_ref)
                if ref not in view_ids:
                    ctx.loss("VIEW-REF", None, f"viewRef {ref!r} names no view", o.id)
                    ref = None
                o.view_ref = ref


def _mark_props_used(el: etree._Element, used: set[str], ctx: _ImportCtx) -> None:
    for w in ctx.kids(el, "properties"):
        for p in ctx.kids(w, "property"):
            used.add(p.get("propertyDefinitionRef") or "")


def _mark_subtree(ctx: _ImportCtx, el: etree._Element) -> None:
    """Mark a subtree as accounted for (its loss was already reported)."""
    for d in el.iter():
        if isinstance(d.tag, str):
            ctx.take(d, *d.attrib.keys())


def _import_orgs(
    ctx: _ImportCtx, parent: etree._Element, *, top: bool
) -> list[ArchimateOrganization]:
    """The folder tree under ``parent``. Folder members are kept in order; a
    folder's sub-folders and members are two lists, so their interleaving is not
    kept (export writes sub-folders first, as Archi does)."""
    folders: list[ArchimateOrganization] = []
    for item in ctx.kids(parent, "item"):
        ctx.take(item, "identifier", "identifierRef")
        if item.get("identifierRef") is not None:
            if top:
                ctx.loss("ORGANIZATION", item, "reference outside any folder not kept")
                _mark_subtree(ctx, item)
            continue  # members are read by the enclosing folder
        raw = item.get("identifier")
        items: list[str] = []
        for ref in ctx.kids(item, "item"):
            target = ref.get("identifierRef")
            if target is None:
                continue
            for sub in (
                ctx.kids(ref, "label") + ctx.kids(ref, "documentation") + ctx.kids(ref, "item")
            ):
                ctx.loss("ORGANIZATION", sub, "content of a folder reference not kept")
                _mark_subtree(ctx, sub)
            items.append(ctx.id_map.get(target, target))
        folders.append(
            ArchimateOrganization(
                id=ctx.snap_id(item, raw) if raw is not None else None,
                name=ctx.pick_text(item, "label", "folder label"),
                documentation=ctx.pick_text(item, "documentation", "folder documentation"),
                items=items,
                children=_import_orgs(ctx, item, top=False),
            )
        )
    return folders


def _import_view(
    ctx: _ImportCtx,
    vel: etree._Element,
    defs: dict[str, str],
    def_used: set[str],
    vp_by_name: dict[str, str],
) -> ArchimateViewIn:
    ctx.take(vel, "identifier", _XSI_TYPE)
    vid = ctx.snap_id(vel, vel.get("identifier"))
    xtype = _xsi_type(vel)
    if xtype not in ("", "Diagram"):
        ctx.loss("TYPE-UNKNOWN", vel, f"view xsi:type {xtype!r} imported as a diagram")
    viewpoint: str | None = None
    if vel.get("viewpoint") is not None:
        ctx.take(vel, "viewpoint")
        viewpoint = vp_by_name.get(vel.get("viewpoint", ""))
        if viewpoint is None:
            ctx.loss(
                "VIEWPOINT-UNKNOWN",
                vel,
                f"viewpoint {vel.get('viewpoint')!r} is not an Archi viewpoint; view unfiltered",
            )
    ctx.take(vel, "viewpointRef")
    name = _clip(ctx, vel, ctx.pick_text(vel, "name", "name"), _MAX_NAME, "name")
    doc = ctx.pick_text(vel, "documentation", "documentation")
    props = _props(ctx, vel, defs)
    _mark_props_used(vel, def_used, ctx)

    objects: list[dict[str, Any]] = []
    node_ids: dict[str, str] = {}  # exchange node id -> snapshot id (kept nodes only)

    def walk(el: etree._Element, parent_sid: str | None, px: int, py: int) -> None:
        for node in ctx.kids(el, "node"):
            ctx.take(node, "identifier", _XSI_TYPE)
            ntype = _xsi_type(node)
            x = _int(ctx, node, "x", 0)
            y = _int(ctx, node, "y", 0)
            w = _int(ctx, node, "w", -1)
            h = _int(ctx, node, "h", -1)
            view_refs = ctx.kids(node, "viewRef")
            for vr in view_refs:
                ctx.take(vr, "ref")
            for vr in view_refs[1:]:
                ctx.loss("VIEW-REF", vr, "one diagram-model reference per node is kept")
            kind = {"Label": "note", "Container": "group"}.get(ntype)
            if ntype == "Element" and node.get("elementRef") is not None:
                kind = "element"
            if kind in ("element", None):
                for d in ctx.kids(node, "documentation"):
                    ctx.loss(
                        "NODE-DOCUMENTATION",
                        d,
                        "documentation on an element occurrence not kept (the concept has its own)",
                    )
                    _mark_subtree(ctx, d)
            if kind is None:
                ctx.loss(
                    "TYPE-UNKNOWN",
                    node,
                    f"node xsi:type {ntype!r} not kept; nested nodes re-parented to the enclosing node",
                )
                # The node itself is what was reported; its children are walked.
                for vr in view_refs[:1]:
                    ctx.loss("VIEW-REF", vr, "diagram-model reference on a node not kept")
                for sub in ctx.kids(node, "label") + ctx.kids(node, "style"):
                    _mark_subtree(ctx, sub)
                ctx.take(node, *node.attrib.keys())
                walk(node, parent_sid, px, py)
                continue
            if node.get("conceptRef") is not None or node.get("xpathPart") is not None:
                ctx.take(node, "conceptRef", "xpathPart")
                ctx.loss("LABEL-CONCEPT-REF", node, "label bound to a concept part not kept")
            text = ctx.pick_text(node, "label", "label")
            sid = ctx.snap_id(node, node.get("identifier"))
            node_ids[node.get("identifier") or ""] = sid
            obj: dict[str, Any] = {
                "id": sid,
                "kind": kind,
                "parent_object_id": parent_sid,
                "bounds": Bounds(x=x - px, y=y - py, w=w, h=h),
                "style": _style(ctx, node),
                _OBJECT_TEXT[kind]: text,
                "view_ref": view_refs[0].get("ref") if view_refs else None,
            }
            if kind != "element":
                obj["documentation"] = ctx.pick_text(node, "documentation", "documentation")
            if kind == "element":
                ctx.take(node, "elementRef")
                ref = node.get("elementRef") or ""
                obj["concept_id"] = ctx.id_map.get(ref, ref)
            objects.append(obj)
            walk(node, sid, x, y)

    walk(vel, None, 0, 0)

    # Border types travel as reserved view properties: "<node identifier>=<type>".
    kept_props: list[tuple[str, str]] = []
    by_sid = {o["id"]: o for o in objects}
    for key, value in props or []:
        if key != BORDER_TYPE_PROPERTY:
            kept_props.append((key, value))
            continue
        nid, sep, border = value.partition("=")
        target = by_sid.get(node_ids.get(nid, "")) if sep and border else None
        if target is None or target.get("border_type") is not None:
            ctx.loss("VALUE-INVALID", vel, f"{BORDER_TYPE_PROPERTY} value {value!r} not kept")
            continue
        target["border_type"] = border

    conns: list[tuple[etree._Element, str]] = []
    for ce in ctx.kids(vel, "connection"):
        ctx.take(ce, "identifier", _XSI_TYPE, "source", "target")
        ctype = _xsi_type(ce)
        if ctype in ("Relationship", "NestingRelationship") and ce.get("relationshipRef"):
            ctx.take(ce, "relationshipRef")
            conns.append((ce, ctype))
        elif ctype == "Line":
            conns.append((ce, ctype))
        else:
            ctx.loss("TYPE-UNKNOWN", ce, f"connection xsi:type {ctype!r} not kept")
            _mark_subtree(ctx, ce)
    # Endpoints must be kept nodes or kept connections (fixpoint).
    kept = {ce.get("identifier") or "" for ce, _ in conns}
    changed = True
    while changed:
        changed = False
        for ce, _ in conns:
            cid = ce.get("identifier") or ""
            if cid in kept and not all(
                (ce.get(a) or "") in node_ids or (ce.get(a) or "") in kept
                for a in ("source", "target")
            ):
                kept.discard(cid)
                changed = True
    connections: list[ArchimateViewConnectionIn] = []
    conn_ids: dict[str, str] = {}
    for ce, _ in conns:
        if (ce.get("identifier") or "") in kept:
            conn_ids[ce.get("identifier") or ""] = ctx.snap_id(ce, ce.get("identifier"))
    for ce, ctype in conns:
        cid = ce.get("identifier") or ""
        if cid not in kept:
            ctx.loss(
                "CONNECTION-DANGLING",
                ce,
                "connection attached to a construct that was not kept; not kept",
            )
            _mark_subtree(ctx, ce)
            continue
        if ctype == "NestingRelationship":
            ctx.loss("NESTING-FLAG", ce, "nesting-relationship marker not kept (connection kept)")
        for tag in ("sourceAttachment", "targetAttachment"):
            for att in ctx.kids(ce, tag):
                ctx.loss("CONNECTION-ATTACHMENT", att, f"{tag} point not kept")
                _mark_subtree(ctx, att)
        for sub in ctx.kids(ce, "documentation"):
            ctx.loss("CONNECTION-DOCUMENTATION", sub, "documentation on a connection not kept")
            _mark_subtree(ctx, sub)
        for vr in ctx.kids(ce, "viewRef"):
            ctx.loss("VIEW-REF", vr, "diagram-model reference on a connection not kept")
            _mark_subtree(ctx, vr)
        bends = [
            Point(x=_int(ctx, b, "x", 0), y=_int(ctx, b, "y", 0)) for b in ctx.kids(ce, "bendpoint")
        ]

        def end(attr: str, _ce: etree._Element = ce) -> str:
            raw = _ce.get(attr) or ""
            return node_ids.get(raw) or conn_ids.get(raw) or raw

        rel = ce.get("relationshipRef")
        connections.append(
            ArchimateViewConnectionIn(
                id=conn_ids[cid],
                kind="line" if ctype == "Line" else "relationship",
                relationship_id=None if rel is None else ctx.id_map.get(rel, rel),
                source_object_id=end("source"),
                target_object_id=end("target"),
                label=ctx.pick_text(ce, "label", "label"),
                bendpoints=bends or None,
                style=_style(ctx, ce),
            )
        )
    return ArchimateViewIn(
        id=vid,
        name=name,
        viewpoint=viewpoint,
        viewpoint_ref=vel.get("viewpointRef"),
        objects=[ArchimateViewObjectIn(**o) for o in objects],
        connections=connections,
        documentation=doc,
        properties=kept_props or None,
    )


def _report_unconsumed(ctx: _ImportCtx, root: etree._Element) -> None:
    """Report every element/attribute the mapper did not account for."""
    allowed_attr_ns = {XSI, XML_NS}

    def visit(el: etree._Element) -> None:
        for child in el:
            if not isinstance(child.tag, str):
                continue  # comments and processing instructions carry no model content
            q = etree.QName(child)
            if child not in ctx.consumed:
                code = "EXTENSION" if q.namespace != NS else "UNHANDLED"
                ctx.loss(
                    code,
                    child,
                    f"element {{{q.namespace}}}{q.localname} not imported"
                    + (" (vendor extension)" if code == "EXTENSION" else ""),
                )
                continue
            check_attrs(child)
            visit(child)

    def check_attrs(el: etree._Element) -> None:
        taken = ctx.consumed.get(el, set())
        for attr in el.attrib:
            if attr in taken:
                continue
            q = etree.QName(attr)
            if q.namespace in allowed_attr_ns and attr in (_LANG, _XSI_SCHEMA_LOCATION):
                continue
            code = "EXTENSION" if q.namespace not in (None, *allowed_attr_ns) else "UNHANDLED"
            ctx.loss(code, el, f"attribute {attr} = {el.get(attr)!r} not imported")

    check_attrs(root)
    visit(root)


__all__ = [
    "ExchangeError",
    "ExchangeLoss",
    "ExportResult",
    "ImportResult",
    "LOSS_CODES",
    "export_exchange",
    "export_exchange_report",
    "exchange_schema",
    "import_exchange",
    "schema_errors",
]
