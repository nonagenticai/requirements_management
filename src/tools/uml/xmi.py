"""OMG XMI 2.5.1 / UML 2.5.1 export of a stored UML model (pure: snapshot in, bytes out).

The document is one ``xmi:XMI`` holding one ``uml:Model``:

* elements are ``packagedElement`` of their owning package (or of the model when they
  have no owner); a port is an ``ownedAttribute`` (``uml:Port``) of its class/component.
  Properties, operations (with parameters and a ``return`` parameter), enumeration
  literals and ``isAbstract`` are emitted as UML features; ``type`` / ``general`` /
  ``client`` / ``supplier`` are idrefs.
* relationships: a generalization is a ``generalization`` of its specific classifier, an
  interface realization an ``interfaceRealization`` of its implementing classifier;
  realization / dependency / usage are ``packagedElement`` (``client`` → ``supplier``) of
  the source's package; an association is a ``packagedElement`` with two ``ownedEnd``
  properties (``navigableOwnedEnd`` for navigable ends); a connector is an
  ``ownedConnector`` of the component that owns its source port (else of the source).
* interactions are ``uml:Interaction`` with lifelines, message occurrences, executions,
  messages and combined fragments (an operand's occurrences are that operand's fragments).
* What UML has no slot for — a diagram's kind and which elements / relationships /
  lifelines / messages it shows, and a port's stored provided / required interfaces — is
  carried in one ``xmi:Extension extender="requirements-api"`` per model, as idrefs.

``xmi:id`` values are the stored ids prefixed with ``_`` (an xsd:ID must not start with a
digit, and a UUID may). Ids are unique model-wide (UML-REF-002), so derived ids
(``…-lower``, ``…-return``) cannot collide with a stored one.
"""

from __future__ import annotations

import re
from typing import Any

from lxml import etree

from schemas.uml import (
    UmlElementIn,
    UmlInteractionIn,
    UmlOperation,
    UmlProperty,
    UmlRelationshipIn,
    UmlSnapshot,
)
from tools.rendering.svg import clean_text

XMI_NS = "http://www.omg.org/spec/XMI/20131001"
UML_NS = "http://www.omg.org/spec/UML/20161101"
EXTENDER = "requirements-api"

_XMI = f"{{{XMI_NS}}}"
_ELEMENT_TYPES = {
    "package": "uml:Package",
    "class": "uml:Class",
    "interface": "uml:Interface",
    "data_type": "uml:DataType",
    "enumeration": "uml:Enumeration",
    "primitive_type": "uml:PrimitiveType",
    "component": "uml:Component",
    "port": "uml:Port",
    "actor": "uml:Actor",
}
_DIRECTED_TYPES = {
    "realization": "uml:Realization",
    "dependency": "uml:Dependency",
    "usage": "uml:Usage",
}
_MESSAGE_SORTS = {
    "synch_call": "synchCall",
    "asynch_call": "asynchCall",
    "asynch_signal": "asynchSignal",
    "reply": "reply",
    "create": "createMessage",
    "delete": "deleteMessage",
}
_MULTIPLICITY = re.compile(r"^\s*(\d+|\*)\s*(?:\.\.\s*(\d+|\*)\s*)?$")


def xid(value: str) -> str:
    """A stored id as an xsd:ID (NCName): ``_`` + the id with non-NCName characters
    replaced."""
    return "_" + re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _el(parent: etree._Element, tag: str, xmi_type: str | None, ident: str | None, **attrs):
    node = etree.SubElement(parent, tag)
    if xmi_type is not None:
        node.set(f"{_XMI}type", xmi_type)
    if ident is not None:
        node.set(f"{_XMI}id", ident)
    for key, value in attrs.items():
        if value is not None and value != "":
            node.set(key, clean_text(value))
    return node


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _multiplicity(node: etree._Element, ident: str, value: str | None) -> None:
    if not value:
        return
    match = _MULTIPLICITY.match(value)
    if match is None:
        return
    lower, upper = match.group(1), match.group(2)
    if upper is None:
        lower, upper = ("0", "*") if lower == "*" else (lower, lower)
    if lower == "*":
        lower = "0"
    _el(node, "lowerValue", "uml:LiteralInteger", f"{ident}-lower", value=lower)
    _el(node, "upperValue", "uml:LiteralUnlimitedNatural", f"{ident}-upper", value=upper)


def _property(parent: etree._Element, prop: UmlProperty) -> None:
    ident = xid(prop.id)
    node = _el(
        parent,
        "ownedAttribute",
        "uml:Property",
        ident,
        name=prop.name,
        visibility=prop.visibility,
        type=xid(prop.type_ref) if prop.type_ref else None,
        isStatic=_bool(prop.is_static) if prop.is_static else None,
    )
    _multiplicity(node, ident, prop.multiplicity)
    if prop.default is not None:
        _el(node, "defaultValue", "uml:LiteralString", f"{ident}-default", value=prop.default)


def _operation(parent: etree._Element, op: UmlOperation) -> None:
    ident = xid(op.id)
    node = _el(
        parent,
        "ownedOperation",
        "uml:Operation",
        ident,
        name=op.name,
        visibility=op.visibility,
        isAbstract=_bool(op.is_abstract) if op.is_abstract else None,
        isStatic=_bool(op.is_static) if op.is_static else None,
    )
    for p in op.parameters:
        pid = xid(p.id)
        pnode = _el(
            node,
            "ownedParameter",
            "uml:Parameter",
            pid,
            name=p.name,
            direction=p.direction,
            type=xid(p.type_ref) if p.type_ref else None,
        )
        _multiplicity(pnode, pid, p.multiplicity)
    if op.return_type_ref:
        _el(
            node,
            "ownedParameter",
            "uml:Parameter",
            f"{ident}-return",
            direction="return",
            type=xid(op.return_type_ref),
        )


class _Writer:
    def __init__(self, snapshot: UmlSnapshot) -> None:
        self.snapshot = snapshot
        self.elements = {e.id: e for e in snapshot.elements}
        self.nodes: dict[str, etree._Element] = {}

    # -- elements ------------------------------------------------------------------

    def element(self, parent: etree._Element, e: UmlElementIn) -> None:
        tag = "ownedAttribute" if e.kind == "port" else "packagedElement"
        node = _el(
            parent,
            tag,
            _ELEMENT_TYPES.get(e.kind, "uml:Class"),
            xid(e.id),
            name=e.name,
            isAbstract=_bool(e.attributes.is_abstract) if e.attributes.is_abstract else None,
        )
        self.nodes[e.id] = node
        if e.kind == "port":
            return
        for prop in e.attributes.properties:
            _property(node, prop)
        for op in e.attributes.operations:
            _operation(node, op)
        for index, literal in enumerate(e.attributes.literals):
            _el(
                node,
                "ownedLiteral",
                "uml:EnumerationLiteral",
                f"{xid(e.id)}-literal-{index}",
                name=literal,
            )
        for child in self.snapshot.elements:
            if child.owner_id == e.id:
                self.element(node, child)

    def container_of(self, element_id: str | None, root: etree._Element) -> etree._Element:
        """The package node that holds ``element_id`` (the model when it has none)."""
        e = self.elements.get(element_id or "")
        while e is not None:
            if e.kind == "package" and e.id in self.nodes:
                return self.nodes[e.id]
            e = self.elements.get(e.owner_id or "")
        return root

    # -- relationships -------------------------------------------------------------

    def relationship(self, r: UmlRelationshipIn, root: etree._Element) -> None:
        ident = xid(r.id)
        source = self.nodes.get(r.source or "")
        if r.kind == "generalization" and source is not None and r.target:
            _el(source, "generalization", "uml:Generalization", ident, general=xid(r.target))
        elif r.kind == "interface_realization" and source is not None and r.target:
            _el(
                source,
                "interfaceRealization",
                "uml:InterfaceRealization",
                ident,
                name=r.name,
                client=xid(r.source or ""),
                supplier=xid(r.target),
                contract=xid(r.target),
                implementingClassifier=xid(r.source or ""),
            )
        elif r.kind in _DIRECTED_TYPES and r.source and r.target:
            _el(
                self.container_of(r.source, root),
                "packagedElement",
                _DIRECTED_TYPES[r.kind],
                ident,
                name=r.name,
                client=xid(r.source),
                supplier=xid(r.target),
            )
        elif r.kind == "association" and r.ends:
            self.association(r, root)
        elif r.kind == "connector" and r.source and r.target:
            self.connector(r, root)

    def association(self, r: UmlRelationshipIn, root: etree._Element) -> None:
        ends = r.ends or []
        ident = xid(r.id)
        end_ids = [f"{ident}-end-{i}" for i in range(len(ends))]
        node = _el(
            self.container_of(ends[0].element_ref, root),
            "packagedElement",
            "uml:Association",
            ident,
            name=r.name,
            memberEnd=" ".join(end_ids),
        )
        navigable = [eid for eid, end in zip(end_ids, ends, strict=True) if end.navigable]
        if navigable:
            node.set("navigableOwnedEnd", " ".join(navigable))
        for eid, end in zip(end_ids, ends, strict=True):
            enode = _el(
                node,
                "ownedEnd",
                "uml:Property",
                eid,
                name=end.role,
                type=xid(end.element_ref),
                association=ident,
                aggregation=end.aggregation if end.aggregation != "none" else None,
            )
            _multiplicity(enode, eid, end.multiplicity)

    def connector(self, r: UmlRelationshipIn, root: etree._Element) -> None:
        source = self.elements.get(r.source or "")
        owner_id = source.owner_id if source is not None and source.kind == "port" else r.source
        owner = self.nodes.get(owner_id or "")
        ident = xid(r.id)
        if owner is None:
            owner = self.container_of(r.source, root)
        node = _el(
            owner,
            "ownedConnector",
            "uml:Connector",
            ident,
            name=r.name,
            kind=r.connector_kind,
        )
        _el(node, "end", "uml:ConnectorEnd", f"{ident}-end-0", role=xid(r.source or ""))
        _el(node, "end", "uml:ConnectorEnd", f"{ident}-end-1", role=xid(r.target or ""))

    # -- interactions --------------------------------------------------------------

    def interaction(self, inter: UmlInteractionIn, root: etree._Element) -> None:
        owner = self.nodes.get(inter.owner_id or "")
        if owner is not None and self.elements[inter.owner_id or ""].kind != "package":
            node = _el(owner, "ownedBehavior", "uml:Interaction", xid(inter.id), name=inter.name)
        else:
            node = _el(
                self.container_of(inter.owner_id, root),
                "packagedElement",
                "uml:Interaction",
                xid(inter.id),
                name=inter.name,
            )
        for ll in inter.lifelines:
            lid = xid(ll.id)
            represents = None
            if ll.represents_ref:
                represents = f"{lid}-represents"
                _el(
                    node,
                    "ownedAttribute",
                    "uml:Property",
                    represents,
                    name=ll.name,
                    type=xid(ll.represents_ref),
                )
            _el(node, "lifeline", "uml:Lifeline", lid, name=ll.name, represents=represents)
        message_of: dict[str, str] = {}
        for m in inter.messages:
            message_of[m.send_occurrence] = m.id
            message_of[m.receive_occurrence] = m.id
        in_operand = {o for f in inter.fragments for op in f.operands for o in op.occurrences}
        occurrences = {o.id: o for o in inter.occurrences}

        def occurrence(parent: etree._Element, oid: str) -> None:
            occ = occurrences.get(oid)
            if occ is None:
                return
            msg = message_of.get(oid)
            _el(
                parent,
                "fragment",
                "uml:MessageOccurrenceSpecification" if msg else "uml:OccurrenceSpecification",
                xid(oid),
                covered=xid(occ.lifeline),
                message=xid(msg) if msg else None,
            )

        for occ in inter.occurrences:
            if occ.id not in in_operand:
                occurrence(node, occ.id)
        for ex in inter.executions:
            _el(
                node,
                "fragment",
                "uml:BehaviorExecutionSpecification",
                xid(ex.id),
                covered=xid(ex.lifeline),
                start=xid(ex.start),
                finish=xid(ex.finish),
            )
        for frag in inter.fragments:
            fid = xid(frag.id)
            fnode = _el(
                node,
                "fragment",
                "uml:CombinedFragment",
                fid,
                interactionOperator=frag.operator,
                covered=" ".join(xid(c) for c in frag.covered) or None,
            )
            for index, operand in enumerate(frag.operands):
                oid = f"{fid}-operand-{index}"
                onode = _el(fnode, "operand", "uml:InteractionOperand", oid)
                if operand.guard:
                    guard = _el(onode, "guard", "uml:InteractionConstraint", f"{oid}-guard")
                    _el(
                        guard,
                        "specification",
                        "uml:LiteralString",
                        f"{oid}-guard-spec",
                        value=operand.guard,
                    )
                for occ_id in operand.occurrences:
                    occurrence(onode, occ_id)
        for m in inter.messages:
            mnode = _el(
                node,
                "message",
                "uml:Message",
                xid(m.id),
                name=m.name,
                messageSort=_MESSAGE_SORTS.get(m.kind),
                sendEvent=xid(m.send_occurrence),
                receiveEvent=xid(m.receive_occurrence),
                signature=xid(m.operation_ref) if m.operation_ref else None,
            )
            for index, arg in enumerate(m.arguments):
                _el(
                    mnode,
                    "argument",
                    "uml:LiteralString",
                    f"{xid(m.id)}-arg-{index}",
                    value=arg,
                )

    # -- extension -----------------------------------------------------------------

    def extension(self, parent: etree._Element) -> None:
        ext = _el(parent, f"{_XMI}Extension", None, None, extender=EXTENDER)
        for e in self.snapshot.elements:
            if e.kind == "port" and (e.attributes.provided or e.attributes.required):
                port = _el(ext, "port", None, None, element=xid(e.id))
                for ref in e.attributes.provided:
                    _el(port, "provided", None, None, interface=xid(ref))
                for ref in e.attributes.required:
                    _el(port, "required", None, None, interface=xid(ref))
        for d in self.snapshot.diagrams or []:
            dnode = _el(ext, "diagram", None, None, id=xid(d.id), name=d.name, kind=d.kind)
            for o in d.objects:
                if o.element_ref:
                    _el(dnode, "element", None, None, ref=xid(o.element_ref))
                if o.lifeline_id:
                    _el(dnode, "lifeline", None, None, ref=xid(o.lifeline_id))
            for c in d.connections:
                if c.relationship_ref:
                    _el(dnode, "relationship", None, None, ref=xid(c.relationship_ref))
                if c.message_id:
                    _el(dnode, "message", None, None, ref=xid(c.message_id))


def export_xmi(
    snapshot: UmlSnapshot, *, model_id: str, name: str, documentation: str | None = None
) -> bytes:
    """The model as an XMI 2.5.1 document (UTF-8, with XML declaration)."""
    root = etree.Element(f"{_XMI}XMI", nsmap={"xmi": XMI_NS, "uml": UML_NS})
    model = _el(root, f"{{{UML_NS}}}Model", None, xid(model_id), name=name)
    if documentation:
        comment = _el(model, "ownedComment", "uml:Comment", f"{xid(model_id)}-doc")
        body = etree.SubElement(comment, "body")
        body.text = clean_text(documentation)
    writer = _Writer(snapshot)
    for e in snapshot.elements:
        if not e.owner_id or e.owner_id not in writer.elements:
            writer.element(model, e)
    for r in snapshot.relationships:
        writer.relationship(r, model)
    for inter in snapshot.interactions:
        writer.interaction(inter, model)
    writer.extension(root)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def manifest_header(manifest: dict[str, Any]) -> tuple[str, str, str | None]:
    """(model id, name, documentation) of a manifest's ``model`` header."""
    header = manifest.get("model") or {}
    return str(header.get("id", "")), header.get("title") or "", header.get("description")
