"""Turn structured data + a schema tree into NETCONF XML.

Design notes (these are the things that are easy to get wrong):

* **Namespaces** -- each element carries the namespace of *its defining
  module* as a default ``xmlns``. Identityref *values* additionally need a
  prefix bound to the module that defines the value identity; that prefix
  is declared on the leaf element as ``xmlns:<prefix>``.

* **Key order** -- for a ``list``, the key leaves are emitted first, in
  declared order, before any non-key children. NETCONF servers rely on
  this.

* **leaf-list** -- a leaf-list maps to repeated sibling elements, one per
  value.

* **operation** -- an optional ``operation`` key in the data dict injects a
  ``<... operation="merge">`` attribute on that element. The caller picks
  the namespace (base NETCONF vs. a specific one); we only attach the
  attribute. This keeps the builder free of NETCONF-protocol knowledge.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .loader import Loader
from .schema import SchemaNode, build_schema
from .validator import emit_warnings

# Sentinel key in data dicts that turns into an `operation=` attribute.
# Picked to be unlikely to collide with real YANG leaf names.
OPERATION_KEY = "_operation"

# NETCONF edit-config operations (RFC 6241 §7.2).
VALID_OPERATIONS = {"merge", "replace", "create", "delete", "remove"}


class BuildError(ValueError):
    """Raised when the input data does not fit the schema."""


@dataclass
class _PrefixTable:
    """Tracks xmlns declarations needed on an element.

    A default namespace (no prefix) is only emitted when it differs from
    the parent's -- inheriting the parent's default ns is the NETCONF
    convention and keeps the output free of redundant declarations.
    Prefixed namespaces (for identityref values) are always emitted.
    """

    default_ns: str | None = None
    prefixed: dict[str, str] = field(default_factory=dict)

    def apply(self, elem: ET.Element, parent_ns: str | None = None) -> None:
        if self.default_ns is not None and self.default_ns != parent_ns:
            elem.set("xmlns", self.default_ns)
        for prefix, uri in self.prefixed.items():
            elem.set(f"xmlns:{prefix}", uri)


def build(
    loader: Loader,
    module_name: str,
    root: str,
    data: Any,
    operation: str | None = None,
) -> ET.Element:
    """Build the XML tree for one top-level container/list.

    ``data`` is the content of ``root`` (a dict for a container, a list of
    dicts for a top-level list). ``operation``, if given, is attached to
    the root element.
    """
    schema = build_schema(loader, module_name, root)
    builder = _Builder(loader)
    return builder.build_node(schema, data, root_operation=operation)


def build_fragment(
    loader: Loader,
    module_name: str,
    root: str,
    data: Any,
    operation: str | None = None,
) -> str:
    """Convenience wrapper returning pretty-printed XML as a string."""
    elem = build(loader, module_name, root, data, operation=operation)
    return _pretty(elem)


# ----------------------------------------------------------------------

class _Builder:
    def __init__(self, loader: Loader):
        self.loader = loader

    def build_node(
        self,
        node: SchemaNode,
        data: Any,
        *,
        root_operation: str | None = None,
    ) -> ET.Element:
        if node.kind == "container":
            return self._build_container(node, data, root_operation, parent_ns=None)
        if node.kind == "list":
            return self._build_list_wrapper(node, data, root_operation)
        if node.kind == "rpc":
            # An rpc call serializes as <rpc-name>input-children</rpc-name>
            # (no <input> wrapper, per RFC 6241) -- structurally a container
            # whose children are the rpc's input parameters.
            return self._build_container(node, data, root_operation, parent_ns=None)
        if node.is_leaf:  # leaf, leaf-list, or anyxml -- text-bearing nodes
            return self._build_leaf(node, data, root_operation, parent_ns=None)
        raise BuildError(f"unsupported node kind {node.kind!r} for {node.name!r}")

    # -- containers ----------------------------------------------------

    def _build_container(
        self,
        node: SchemaNode,
        data: dict | None,
        operation: str | None,
        parent_ns: str | None = None,
    ) -> ET.Element:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise BuildError(
                f"{node.name!r} is a container; expected a mapping, got "
                f"{type(data).__name__}"
            )
        elem = ET.Element(node.name)
        _PrefixTable(default_ns=node.namespace).apply(elem, parent_ns)
        _apply_operation(elem, operation)
        self._emit_children(elem, node, data)
        return elem

    def _emit_children(self, parent: ET.Element, node: SchemaNode, data: dict) -> None:
        parent_ns = node.namespace
        for name, value in data.items():
            if name == OPERATION_KEY:
                continue
            child_schema = self._lookup_child(node, name)
            if child_schema.kind == "leaf-list":
                self._emit_leaf_list(parent, child_schema, value, parent_ns)
            elif child_schema.kind == "list":
                for item in _as_list(value, child_schema.name):
                    parent.append(self._build_list_item(child_schema, item, parent_ns))
            elif child_schema.kind == "container":
                parent.append(
                    self._build_container(child_schema, value, _op_of(value), parent_ns)
                )
            elif child_schema.is_leaf:  # leaf or anyxml -- text-bearing node
                parent.append(
                    self._build_leaf(child_schema, value, _op_of(value), parent_ns)
                )
            else:
                raise BuildError(
                    f"unsupported child kind {child_schema.kind!r} for "
                    f"{child_schema.name!r} under {node.name!r}"
                )

    def _lookup_child(self, node: SchemaNode, name: str) -> SchemaNode:
        try:
            return node.child(name)
        except KeyError as e:
            raise BuildError(str(e)) from None

    # -- lists ---------------------------------------------------------

    def _build_list_wrapper(
        self,
        node: SchemaNode,
        data: list,
        operation: str | None,
    ) -> ET.Element:
        """A top-level list is wrapped in a container named after the list.

        NETCONF has no bare list at the top of a config; the list sits
        inside its parent container. When the caller asks to build a list
        directly, we synthesise that parent so the output is well-formed.
        """
        wrapper = ET.Element(node.name)
        _PrefixTable(default_ns=node.namespace).apply(wrapper, parent_ns=None)
        _apply_operation(wrapper, operation)
        for item in _as_list(data, node.name):
            wrapper.append(self._build_list_item(node, item, node.namespace))
        return wrapper

    def _build_list_item(
        self, node: SchemaNode, data: Any, parent_ns: str | None = None
    ) -> ET.Element:
        if not isinstance(data, dict):
            raise BuildError(
                f"list {node.name!r} entry must be a mapping, got "
                f"{type(data).__name__}"
            )
        self._require_keys(node, data)
        elem = ET.Element(node.name)
        _PrefixTable(default_ns=node.namespace).apply(elem, parent_ns)
        _apply_operation(elem, _op_of(data))

        # Emit key leaves first, in declared order, then the rest.
        emitted: set[str] = set()
        for key_name in node.keys:
            if key_name in data:
                key_schema = node.child(key_name)
                elem.append(self._build_leaf(key_schema, data[key_name], None, node.namespace))
                emitted.add(key_name)

        for name, value in data.items():
            if name in emitted or name == OPERATION_KEY:
                continue
            child_schema = self._lookup_child(node, name)
            if child_schema.kind == "leaf-list":
                self._emit_leaf_list(elem, child_schema, value, node.namespace)
            elif child_schema.kind == "list":
                for item in _as_list(value, child_schema.name):
                    elem.append(self._build_list_item(child_schema, item, node.namespace))
            elif child_schema.kind == "container":
                elem.append(self._build_container(child_schema, value, _op_of(value), node.namespace))
            elif child_schema.is_leaf:  # leaf or anyxml -- text-bearing node
                elem.append(
                    self._build_leaf(child_schema, value, _op_of(value), node.namespace)
                )
            else:
                raise BuildError(
                    f"unsupported child kind {child_schema.kind!r} for "
                    f"{child_schema.name!r} under list {node.name!r}"
                )
        return elem

    def _require_keys(self, node: SchemaNode, data: dict) -> None:
        missing = [k for k in node.keys if k not in data]
        if missing:
            raise BuildError(
                f"list {node.name!r} entry missing key leaf(s): {missing}"
            )

    # -- leaves --------------------------------------------------------

    def _build_leaf(
        self,
        node: SchemaNode,
        value: Any,
        operation: str | None = None,
        parent_ns: str | None = None,
    ) -> ET.Element:
        # A leaf can carry the operation sentinel as a dict, e.g.
        # {"description": {"_operation": "delete"}} -> <description nc:operation="delete"/>.
        # delete/remove on a leaf carry no value (RFC 6241 §7.2: the element
        # itself, with its operation, is the whole instruction). Any other
        # operation needs a concrete value, so a sentinel-only dict for
        # merge/replace/create is meaningless and we reject it.
        if isinstance(value, dict) and OPERATION_KEY in value:
            if operation not in ("delete", "remove"):
                raise BuildError(
                    f"leaf {node.name!r} has an _operation sentinel "
                    f"({operation!r}) without a value; only 'delete'/"
                    f"'remove' may omit the value"
                )
            value = None

        elem = ET.Element(node.name)
        table = _PrefixTable(default_ns=node.namespace)
        text, extra_ns = (None, {})
        # An `empty` type leaf serialises as <name/> with no text, regardless
        # of any value the user supplied (its mere presence is the value).
        if value is not None and not _is_empty_type(node):
            text, extra_ns = self._format_value(node, value)
            table.prefixed.update(extra_ns)
        table.apply(elem, parent_ns)
        _apply_operation(elem, operation)
        elem.text = text
        return elem

    def _emit_leaf_list(
        self, parent: ET.Element, node: SchemaNode, value: Any, parent_ns: str | None
    ) -> None:
        if value is None:
            return
        for item in _as_list(value, node.name):
            parent.append(self._build_leaf(node, item, None, parent_ns))

    def _format_value(self, node: SchemaNode, value: Any) -> tuple[str, dict[str, str]]:
        """Return (text, extra prefixed-namespaces) for a leaf value.

        For identityref, the value is ``<module-prefix>:<identity>``; we
        resolve the identity's defining module and emit a matching
        ``xmlns:<prefix>``. The prefix is derived from the module name for
        determinism.

        Validation runs first (non-blocking): any YANG type-constraint
        violation (range/length/pattern/enum/identityref-derivation/
        decimal64-precision/union/bits) is emitted as a
        :class:`~yang_xml_gen.validator.YangValidationWarning` via
        :func:`warnings.warn`. Generation always proceeds; the device does
        its own validation on edit-config.
        """
        emit_warnings(node, value, self.loader)
        t = node.type
        if t is not None and t.is_identityref:
            return self._format_identityref(value)
        return _to_str(value), {}

    def _format_identityref(self, value: str) -> tuple[str, dict[str, str]]:
        ident = _to_str(value)
        # Allow either a bare identity name ("ethernetCsmacd") or an already
        # prefixed one ("ianaift:ethernetCsmacd"). We always (re)resolve so
        # the prefix matches our own xmlns declaration.
        if ":" in ident:
            _existing_prefix, ident = ident.split(":", 1)
        mod_name = self.loader.identities.get(ident)
        if mod_name is None:
            raise BuildError(
                f"identity {ident!r} not found in any loaded module"
            )
        mod = self.loader.ctx.get_module(mod_name)
        ns = mod.search_one("namespace").arg
        prefix = _module_prefix(mod)
        return f"{prefix}:{ident}", {prefix: ns}


# ----------------------------------------------------------------------

# Namespace for the NETCONF base protocol; used for the `operation`
# attribute and the edit-config wrapper.
NC_NS = "urn:ietf:params:netconf:base:1.0"
_NC_OPERATION_ATTR = f"{{{NC_NS}}}operation"

# Bind the NETCONF base namespace to its conventional "nc" prefix.
# register_namespace is process-global, which is fine here: "nc" is a
# fixed convention across all NETCONF XML this tool emits, and a single
# well-known prefix is preferable to ElementTree's auto-assigned ns0/ns1.
ET.register_namespace("nc", NC_NS)


def _apply_operation(elem: ET.Element, operation: str | None) -> None:
    if operation is None:
        return
    if operation not in VALID_OPERATIONS:
        raise BuildError(
            f"invalid operation {operation!r}; expected one of "
            f"{sorted(VALID_OPERATIONS)}"
        )
    # Use the namespaced attribute form `nc:operation`. ElementTree emits
    # the xmlns:nc declaration automatically when a {uri}local attribute
    # is set.
    elem.set(_NC_OPERATION_ATTR, operation)


def _op_of(value: Any) -> str | None:
    """Extract the operation sentinel from a child value, if present."""
    if isinstance(value, dict) and OPERATION_KEY in value:
        return value[OPERATION_KEY]
    return None


def _as_list(value: Any, name: str) -> list:
    if isinstance(value, list):
        return value
    # A single dict for a list/leaf-list is convenient shorthand.
    if isinstance(value, dict):
        return [value]
    raise BuildError(
        f"{name!r} expects a list, got {type(value).__name__}"
    )


def _to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _is_empty_type(node: SchemaNode) -> bool:
    """True if the leaf's type is YANG ``empty``.

    Empty leaves carry no text -- their presence in the XML is the value.
    We don't resolve typedef chains, so this only catches a directly-typed
    ``empty`` (common in practice); typedefs hiding empty are rare and the
    value still serialises acceptably via _to_str.
    """
    return node.type is not None and node.type.name == "empty"


def _module_prefix(mod) -> str:
    """The XML prefix to use for a module, derived from its YANG prefix.

    We prefer the module's own prefix statement; if that collides we'd need
    disambiguation, but within one document the caller-controlled scope
    keeps collisions unlikely in practice.
    """
    pfx = mod.search_one("prefix")
    return pfx.arg if pfx is not None else mod.arg


def _pretty(elem: ET.Element) -> str:
    """Indent and serialise, with an XML declaration."""
    ET.indent(elem, space="  ")
    return ET.tostring(elem, encoding="unicode", xml_declaration=True)
