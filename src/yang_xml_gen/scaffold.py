"""Generate a blank JSON template from a YANG module's schema.

The intended workflow is two steps::

    1. python -m yang_xml_gen.cli --template ietf-interfaces.interfaces > ifcfg.json
    2. <edit ifcfg.json, fill in values>
    3. python -m yang_xml_gen.cli ifcfg.json --wrap edit-config

This module produces the template for step 1: a spec-shaped JSON skeleton
(``module`` / ``root`` / ``data``) where every writable leaf is present but
empty, lists carry one placeholder entry, and state (config false) nodes
are omitted unless ``include_state=True``.

We do not validate anything here -- the template is structural. Values are
all empty strings (or single-element lists for leaf-lists); the user fills
them in and the device does the validation on edit-config.

Container children are sorted by YANG declaration order (the order pyang
yields them in ``i_children``), which matches how engineers read the model.
"""

from __future__ import annotations

import json
from typing import Any

from .loader import Loader
from .schema import SchemaNode, build_schema


def generate_template(
    loader: Loader,
    module: str,
    root: str,
    *,
    include_state: bool = False,
) -> dict:
    """Return a spec-shaped dict (ready for ``json.dumps``).

    ``include_state=False`` (default) drops config-false nodes, which is what
    you want before an edit-config -- state leaves are not writable. Pass
    ``True`` to keep them, e.g. when building a skeleton for a get/get-config
    filter where you want to see the full data tree.
    """
    schema = build_schema(loader, module, root)
    data = _skeleton(schema, include_state=include_state)
    return {"module": module, "root": root, "data": data}


def template_to_json(
    loader: Loader,
    module: str,
    root: str,
    *,
    include_state: bool = False,
    indent: int = 2,
) -> str:
    """Convenience: build the template and return it as a JSON string."""
    tpl = generate_template(
        loader, module, root, include_state=include_state
    )
    return json.dumps(tpl, indent=indent, ensure_ascii=False)


# ----------------------------------------------------------------------

def _skeleton(node: SchemaNode, *, include_state: bool) -> Any:
    """Recursively build the JSON skeleton for one schema node.

    Filtering of state (config false) nodes is handled by the container/list
    helpers below, which skip such children when ``include_state`` is False.
    This keeps the responsibility in one place per parent kind.

    An ``rpc`` skeleton is its input parameters laid out like a container --
    the user fills in whichever parameters the call takes. (We never generate
    rpc output, only the call direction.)
    """
    if node.kind == "container":
        return _container_skeleton(node, include_state)
    if node.kind == "rpc":
        # rpc input serializes like a container's children (no <input>
        # wrapper); the skeleton is just those input parameters.
        return _container_skeleton(node, include_state)
    if node.kind == "list":
        return _list_skeleton(node, include_state)
    if node.kind == "leaf-list":
        return [""]  # one empty entry as a placeholder
    # leaf
    return ""


def _container_skeleton(node: SchemaNode, include_state: bool) -> dict:
    result: dict[str, Any] = {}
    for name, child in node.children.items():
        if not include_state and not child.is_config:
            # Skip state children entirely in config-only templates.
            continue
        result[name] = _skeleton(child, include_state=include_state)
    return result


def _list_skeleton(node: SchemaNode, include_state: bool) -> list:
    """A list becomes a single placeholder entry.

    Key leaves get a placeholder value ``"<keyname>"`` so the entry is
    immediately usable (a NETCONF list entry must carry its keys). Non-key
    writable leaves are present as empty strings, ready to be filled.
    """
    entry: dict[str, Any] = {}
    keyset = set(node.keys)
    for name, child in node.children.items():
        if not include_state and not child.is_config:
            continue
        if name in keyset:
            entry[name] = f"<{name}>"
        else:
            entry[name] = _skeleton(child, include_state=include_state)
    return [entry]
