"""Wrap pyang statements into a small, stable schema-tree model.

pyang's statement objects are rich and somewhat awkward to use directly
(hierarchical ``i_children`` navigation, namespace lookup through
``i_module``, type info behind ``search_one('type')``). The generator only
needs a handful of facts about each node, so we flatten that into
``SchemaNode`` -- a frozen, easy-to-test view over the data tree.

Only the subset of YANG the generator currently emits is modelled:
``container``, ``list``, ``leaf``, ``leaf-list``, plus ``rpc`` (call
direction only). ``choice``/``case`` are flattened away (per RFC 7951 the
chosen branch's leaves appear directly under the parent; the choice/case
nodes emit nothing), and ``augment`` is already baked into pyang's
``i_children`` so it needs no handling here. ``action``, ``notification``
and rpc output are out of scope for now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .loader import Loader

# Data-node keywords we know how to emit. pyang's ``i_children`` already
# filters non-data nodes out, but we keep this for an explicit, helpful
# error when something unsupported is encountered.
_CONTAINER_KINDS = {"container", "list"}
# anyxml is treated as a leaf that carries arbitrary text content -- good
# enough for the common NETCONF <filter> case (omit it for full retrieval,
# or supply a string). Structured anyxml subtrees are a future concern.
_LEAF_KINDS = {"leaf", "leaf-list", "anyxml"}
# choice/case produce no XML element of their own -- their branch leaves
# are flattened into the parent (RFC 7951 §7.9). pyang guarantees unique
# leaf names across cases, so flattening into one dict is safe.
_FLATTEN_KINDS = {"choice", "case"}


@dataclass(frozen=True)
class TypeInfo:
    """What the XML builder needs to know about a leaf's type."""

    name: str  # e.g. "string", "boolean", "identityref", "enumeration"
    # For identityref: the module name that defines the identity values
    # are drawn from (the base identity's module). ``None`` otherwise.
    identity_base_module: str | None = None
    # For enumeration: the permitted enum names. Empty for other types.
    enums: tuple[str, ...] = ()

    @property
    def is_identityref(self) -> bool:
        return self.name == "identityref"


@dataclass
class SchemaNode:
    """A single node in the data tree (container/list/leaf/leaf-list)."""

    name: str  # XML element name (the YANG identifier)
    kind: str  # "container" | "list" | "leaf" | "leaf-list" | "rpc"
    namespace: str  # xmlns URI for this node's defining module
    module_name: str  # name of the module that defines this node
    # list only: key leaf names in declared order; empty for other kinds.
    keys: tuple[str, ...] = ()
    # leaf/leaf-list only; ``None`` for container/list.
    type: TypeInfo | None = None
    # container/list only; children indexed by name.
    children: dict[str, "SchemaNode"] = field(default_factory=dict)
    # True for config (writable) nodes, False for state (config false).
    # pyang populates ``i_config`` on every data node, including those
    # expanded from groupings, so this is reliable across the tree.
    is_config: bool = True

    # -- navigation helpers -------------------------------------------

    @property
    def is_list(self) -> bool:
        return self.kind == "list"

    @property
    def is_leaf(self) -> bool:
        return self.kind in _LEAF_KINDS

    def child(self, name: str) -> "SchemaNode":
        """Look up a child by element name."""
        try:
            return self.children[name]
        except KeyError:
            raise KeyError(
                f"no child {name!r} under {self.name!r}; "
                f"valid: {sorted(self.children)}"
            ) from None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SchemaNode({self.kind} {self.module_name}:{self.name})"


def build_schema(loader: Loader, module_name: str, root: str | None = None) -> SchemaNode:
    """Build a :class:`SchemaNode` tree for ``module_name``.

    If ``root`` is given, return that top-level node directly
    (e.g. ``"interfaces"`` for a data container or ``"get-config"`` for an
    rpc); otherwise return a synthetic root whose children are the module's
    top-level data nodes and rpcs.
    """
    mod = loader.get_module(module_name)
    tree = _build(mod, mod.i_children)
    if root is None:
        return tree
    try:
        return tree.child(root)
    except KeyError:
        raise KeyError(
            f"module {module_name!r} has no top-level node {root!r}; "
            f"valid: {sorted(tree.children)}"
        ) from None


# ----------------------------------------------------------------------

def _build(defining_module, children: Iterable) -> SchemaNode:
    """Build a container node from a pyang statement's ``i_children``.

    ``defining_module`` is the module whose namespace the *children* belong
    to (pyang stores it on each child as ``i_module``); it is passed in so
    the synthetic root gets the right namespace too.

    ``_from_statement`` returns a *list* of nodes (choice/case flatten into
    their branch leaves, so one pyang statement may contribute several
    children); we merge them all into ``children`` by name.
    """
    ns = defining_module.search_one("namespace").arg
    node = SchemaNode(
        name=defining_module.arg,
        kind="container",
        namespace=ns,
        module_name=defining_module.arg,
    )
    for ch in children:
        for child_node in _from_statement(ch):
            node.children[child_node.name] = child_node
    return node


def _from_statement(stmt) -> list[SchemaNode]:
    """Turn one pyang statement into a list of :class:`SchemaNode`.

    Returns a list because ``choice``/``case`` flatten away: a choice
    contributes all of its cases' branch leaves directly to the parent
    (RFC 7951 §7.9 -- choice/case emit no XML element of their own), so one
    choice statement may yield several sibling nodes. Every other kind
    returns a single-element list.

    ``rpc`` becomes a node of kind ``"rpc"`` whose children are the rpc's
    input data nodes (we only model the call direction, not the output).
    pyang sets ``i_config=None`` on rpc input trees (config does not apply),
    so we force ``is_config=True`` there -- rpc parameters are not state and
    must not be filtered out of config-only templates.
    """
    kind = stmt.keyword

    # choice/case flatten: recurse into each case's data children. For a
    # choice, i_children holds case statements; for a case, i_children holds
    # the data nodes directly. Recursing through _from_statement handles
    # both, plus nested choices inside cases.
    if kind in _FLATTEN_KINDS:
        result: list[SchemaNode] = []
        for ch in stmt.i_children:
            result.extend(_from_statement(ch))
        return result

    mod = stmt.i_module
    namespace = mod.search_one("namespace").arg

    if kind == "rpc":
        # rpc.i_children is always [input, output] (pyang synthesizes the
        # missing ones empty). We take only input -- the call direction.
        input_stmt = next(
            (c for c in stmt.i_children if c.keyword == "input"), None
        )
        node = SchemaNode(
            name=stmt.arg,
            kind="rpc",
            namespace=namespace,
            module_name=mod.arg,
            is_config=True,  # rpc params are not state; never filter them
        )
        if input_stmt is not None:
            for ch in input_stmt.i_children:
                for child_node in _from_statement(ch):
                    # pyang sets i_config=None across the *entire* rpc input
                    # subtree (config does not apply to rpc parameters), which
                    # would otherwise surface as bool(None)=False and get
                    # dropped from config-only templates. Coerce the whole
                    # input subtree to is_config=True so rpc arguments are
                    # always kept.
                    _force_config_true(child_node)
                    node.children[child_node.name] = child_node
        return [node]

    node = SchemaNode(
        name=stmt.arg,
        kind=kind,
        namespace=namespace,
        module_name=mod.arg,
        is_config=bool(getattr(stmt, "i_config", True)),
    )

    if kind == "list":
        # i_key is a tuple of leaf statements (empty for keyless lists).
        node.keys = tuple(k.arg for k in getattr(stmt, "i_key", ()) or ())

    if kind in _LEAF_KINDS:
        node.type = _type_info(stmt)

    if kind in _CONTAINER_KINDS:
        for ch in stmt.i_children:
            for child_node in _from_statement(ch):
                node.children[child_node.name] = child_node

    return [node]


def _force_config_true(node: SchemaNode) -> None:
    """Recursively mark a subtree as config (writable).

    pyang sets ``i_config=None`` across the entire rpc input subtree (config
    does not apply to rpc parameters), which our ``bool(i_config)`` mapping
    turns into ``False`` -- causing scaffold to drop rpc arguments as if they
    were state. This walks the subtree and forces ``is_config=True`` so rpc
    call templates keep every input node.
    """
    node.is_config = True
    for child in node.children.values():
        _force_config_true(child)


def _type_info(leaf_stmt) -> TypeInfo:
    t = leaf_stmt.search_one("type")
    if t is None:
        return TypeInfo(name="string")

    name = t.arg
    if name == "identityref":
        base = t.search_one("base")
        base_mod = None
        if base is not None:
            # base.i_identity is the resolved base identity statement; its
            # defining module is where identity values live (e.g. values for
            # an interface-type leaf come from iana-if-type).
            base_id = getattr(base, "i_identity", None)
            if base_id is not None:
                base_mod = base_id.i_module.arg
        return TypeInfo(name="identityref", identity_base_module=base_mod)

    if name == "enumeration":
        enums = tuple(e.arg for e in t.search("enum"))
        return TypeInfo(name="enumeration", enums=enums)

    return TypeInfo(name=name)
