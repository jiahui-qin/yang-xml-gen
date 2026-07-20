"""Parse a NETCONF ``<rpc-reply>`` back into the JSON spec-data shape.

The forward direction (:mod:`xml_builder`) turns a spec-data mapping into
XML. This module is the inverse: given a ``<rpc-reply>`` string (typically
from a ``<get>``/``<get-config>`` request) it walks the payload against the
YANG schema and reproduces the same container/list/leaf structure the
forward builder consumes, so a reply can be turned back into a spec and
re-emitted (round-trip).

Three reply shapes are recognised (RFC 6241):

  * ``<rpc-reply><data>...</data></rpc-reply>`` -- a data-bearing reply.
    The ``<data>`` payload is the data tree, parsed schema-driven.
  * ``<rpc-reply><ok/></rpc-reply>`` -- a bare success acknowledgement;
    yields ``{"ok": true}``.
  * ``<rpc-reply><rpc-error>...</rpc-error></rpc-reply>`` -- one or more
    errors; ``<rpc-error>`` is protocol-level (not YANG-modelled) so its
    children are passed through as a generic structure.

The data payload's module is inferred from the payload element's xmlns via
:func:`Loader.module_by_namespace`, so the caller only supplies the XML.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from .loader import Loader
from .schema import SchemaNode, build_schema
from .xml_builder import NC_NS, OPERATION_KEY, VALID_OPERATIONS

# Namespaced key for the nc:operation attribute on a parsed element.
_NC_OPERATION_ATTR = f"{{{NC_NS}}}operation"


class ParseError(ValueError):
    """Raised when a reply cannot be parsed (malformed or unexpected shape)."""


def parse_reply(xml: str, loader: Loader, *, data_only: bool = False) -> Any:
    """Parse a ``<rpc-reply>`` string into the JSON spec-data shape.

    For a data-bearing reply with a single payload root, returns
    ``{"module": ..., "root": ..., "data": ...}`` by default, or just the
    ``data`` object when ``data_only=True``. A multi-root ``<data>``
    (full-retrieval reply) is only supported with ``data_only=True`` --
    it returns ``{root_name: data, ...}``; the envelope form is single-root
    by construction and raises :class:`ParseError` for multi-root payloads.

    ``<ok/>`` replies yield ``{"ok": true}`` regardless of ``data_only``.
    ``<rpc-error>`` replies yield ``{"rpc-error": [...]}``.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise ParseError(f"not well-formed XML: {e}") from e

    if _local(root.tag) != "rpc-reply":
        raise ParseError(
            f"expected <rpc-reply> root, got <{_local(root.tag)}>"
        )

    children = list(root)
    if not children:
        raise ParseError("<rpc-reply> has no child element")

    first = children[0]
    first_local = _local(first.tag)

    if first_local == "ok":
        # <ok/> is a bare success ack; there is no data to wrap.
        return {"ok": True}

    if first_local == "rpc-error":
        # One or more <rpc-error>; protocol-level, parsed generically.
        errors = [_parse_generic(c) for c in children if _local(c.tag) == "rpc-error"]
        return {"rpc-error": errors}

    if first_local == "data":
        return _parse_data(first, loader, data_only=data_only)

    raise ParseError(
        f"unsupported <rpc-reply> child <{first_local}>; "
        f"expected <data>, <ok>, or <rpc-error>"
    )


# -- data-bearing replies -------------------------------------------
#
# <data> holds the data tree. The common case is a single root element
# (e.g. a subtree-filter reply returning <interfaces>); the envelope form
# wraps that root. A full-retrieval reply may carry multiple top-level
# roots, which only fits the data-only form (the envelope is single-root).


def _parse_data(data_elem: ET.Element, loader: Loader, *, data_only: bool) -> Any:
    payload = list(data_elem)
    if not payload:
        # Empty <data/> -- nothing was returned. Emit an empty data object
        # (or an empty envelope), matching the forward direction's empty
        # container/list representation.
        if data_only:
            return {}
        raise ParseError(
            "<data> is empty; no module/root to infer (use --data-only for "
            "an empty data object)"
        )

    if len(payload) == 1:
        root_elem = payload[0]
        module, root_name = _infer_module_and_root(root_elem, loader)
        schema = build_schema(loader, module, root_name)
        data = _walk_container(schema, root_elem, loader)
        if data_only:
            return data
        return {"module": module, "root": root_name, "data": data}

    # Multi-root <data> (full retrieval): only the data-only form fits.
    if not data_only:
        raise ParseError(
            "<data> has multiple top-level roots; the envelope form is "
            "single-root -- use --data-only (or narrow with a subtree filter)"
        )
    result: dict[str, Any] = {}
    for root_elem in payload:
        module, root_name = _infer_module_and_root(root_elem, loader)
        schema = build_schema(loader, module, root_name)
        result[root_name] = _walk_container(schema, root_elem, loader)
    return result


def _infer_module_and_root(elem: ET.Element, loader: Loader) -> tuple[str, str]:
    """Infer (module, root-name) from a payload element's namespace.

    ElementTree resolves xmlns into the ``{uri}name`` tag form, so the
    namespace is everything between the braces. The root name is the local
    part (the top-level data node's YANG identifier).
    """
    tag = elem.tag
    if "}" in tag:
        ns, local = tag[1:].split("}", 1)
    else:
        raise ParseError(
            f"payload element <{_local(tag)}> has no namespace; cannot infer "
            f"its YANG module"
        )
    module = loader.module_by_namespace(ns)
    return module, local


# -- schema-driven walk ---------------------------------------------
#
# _walk_container reproduces the spec-data shape the forward builder
# consumes: containers as mappings, lists as arrays of entry mappings,
# leaf-lists as arrays of scalars, leaves as scalars. Schema dispatches
# whether repeated same-named siblings are an array (list/leaf-list) or
# an error (leaf/container -- the forward builder emits each once).


def _walk_container(node: SchemaNode, elem: ET.Element, loader: Loader) -> dict:
    """Parse a container/list-entry element into a spec-data mapping."""
    result: dict[str, Any] = {}

    # Group children by local name so we can detect repeats and apply
    # list/leaf-list semantics in one pass per name.
    groups: dict[str, list[ET.Element]] = {}
    for child in elem:
        groups.setdefault(_local(child.tag), []).append(child)

    for name, siblings in groups.items():
        try:
            child_schema = node.child(name)
        except KeyError as e:
            raise ParseError(str(e)) from None

        if child_schema.is_list:
            entries = [
                _walk_container(child_schema, sib, loader) for sib in siblings
            ]
            result[name] = entries
        elif child_schema.kind == "leaf-list":
            result[name] = [
                _coerce_value(child_schema, sib.text) for sib in siblings
            ]
        elif child_schema.is_leaf:
            if len(siblings) > 1:
                raise ParseError(
                    f"leaf {name!r} appears {len(siblings)} times under "
                    f"{node.name!r}; a leaf is single-valued"
                )
            result[name] = _parse_leaf(child_schema, siblings[0])
        elif child_schema.kind == "container":
            if len(siblings) > 1:
                raise ParseError(
                    f"container {name!r} appears {len(siblings)} times under "
                    f"{node.name!r}; a container is single-valued"
                )
            result[name] = _walk_container(child_schema, siblings[0], loader)
        else:  # pragma: no cover - schema only yields the kinds above
            raise ParseError(
                f"unsupported child kind {child_schema.kind!r} for {name!r}"
            )

    # An nc:operation on the container/list-entry itself round-trips as a
    # sentinel key (mirrors the forward builder's _op_of extraction).
    op = _operation_of(elem)
    if op is not None:
        result[OPERATION_KEY] = op

    return result


def _parse_leaf(node: SchemaNode, elem: ET.Element) -> Any:
    """Parse a leaf element, preserving the delete/remove sentinel.

    A leaf carrying ``nc:operation="delete"`` (or ``remove``) with no text
    round-trips as ``{"_operation": "delete"}`` -- the exact sentinel the
    forward builder consumes to re-emit ``<name nc:operation="delete"/>``.
    A leaf with both a value and an operation (which the forward tool never
    produces) keeps the value and drops the operation.
    """
    op = _operation_of(elem)
    if op in ("delete", "remove") and (elem.text is None or not elem.text.strip()):
        return {OPERATION_KEY: op}
    return _coerce_value(node, elem.text)


def _coerce_value(node: SchemaNode, text: str | None) -> Any:
    """Convert a leaf's text to the JSON type matching the forward builder.

    Symmetric with :func:`xml_builder._to_str`: boolean <-> bool, ``empty``
    <-> True (presence is the value), everything else <-> string. identityref
    keeps its ``prefix:ident`` text verbatim -- the forward builder accepts
    both bare and prefixed identityref values.
    """
    t = node.type
    # An empty-type leaf has no text; its presence in the XML *is* the
    # value. Check this before the None short-circuit below (an <name/>
    # element has text=None but still means "present").
    if t is not None and t.name == "empty":
        return True
    if text is None:
        return None
    if t is None:
        return text
    if t.name == "boolean":
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
        # Unexpected boolean text -- pass through as a string rather than
        # guessing; the device emitted it, the caller can inspect.
        return text
    return text


# -- generic (schema-less) parsing ----------------------------------
#
# <rpc-error> and its children are protocol-level (NETCONF base namespace)
# and not modelled in YANG, so we parse them structurally: elements with
# children become nested mappings, repeated same-named children become
# arrays, leaf elements become their text. This is a pass-through -- the
# caller gets the error structure as the device sent it.


def _parse_generic(elem: ET.Element) -> Any:
    """Parse an element with no YANG schema: structure mirrors the XML.

    Elements with children become ``{local_name: value, ...}`` (repeats
    become arrays); childless elements become their trimmed text (or True
    if empty, mirroring the empty-leaf convention).
    """
    children = list(elem)
    if not children:
        text = elem.text.strip() if elem.text else ""
        return text if text else True

    grouped: dict[str, list[Any]] = {}
    for child in children:
        grouped.setdefault(_local(child.tag), []).append(_parse_generic(child))

    result: dict[str, Any] = {}
    for name, values in grouped.items():
        result[name] = values if len(values) > 1 else values[0]
    return result


# -- helpers --------------------------------------------------------


def _local(tag: str) -> str:
    """Strip the ``{uri}`` namespace prefix from an ElementTree tag."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _operation_of(elem: ET.Element) -> str | None:
    """Return the ``nc:operation`` attribute on ``elem``, if any."""
    op = elem.attrib.get(_NC_OPERATION_ATTR)
    if op is None:
        return None
    if op not in VALID_OPERATIONS:
        # An unknown operation attribute is left alone -- we don't model it,
        # but we also don't silently drop it. (The forward builder only ever
        # emits the valid set, so this branch only fires for hand-crafted
        # input; surface it rather than mis-round-trip.)
        raise ParseError(
            f"element <{_local(elem.tag)}> has unknown nc:operation {op!r}; "
            f"expected one of {sorted(VALID_OPERATIONS)}"
        )
    return op
