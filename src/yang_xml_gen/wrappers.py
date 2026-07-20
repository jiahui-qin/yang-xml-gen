"""Wrap a generated config fragment in NETCONF protocol envelopes.

The XML builder produces a bare ``<config>`` payload -- just the data tree
under a top-level container. To actually send it to a device you wrap it in
a ``<rpc><edit-config>...<config>...</config></edit-config></rpc>`` envelope
(RFC 6241). This module does that wrapping, and also offers a plain
``<get-config>`` builder for the read path.

Envelopes are built with ElementTree's namespaced-tag form (``{uri}name``)
so the ``nc`` prefix registered by ``xml_builder`` applies consistently.
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET

from .loader import Loader
from .xml_builder import NC_NS, build, _pretty

_NETCONF_BASE = "urn:ietf:params:netconf:base:1.0"

# Namespace for the <with-defaults> parameter (RFC 6243). The leaf is
# augmented into get/get-config input by ietf-netconf-with-defaults, so it
# lives in that module's namespace, not the NETCONF base namespace.
WITH_DEFAULTS_NS = "urn:ietf:params:xml:ns:yang:ietf-netconf-with-defaults"

# Valid <with-defaults> modes (RFC 6243 §3). The model defines these as an
# enumeration; we mirror them here so an invalid mode is caught before the
# message leaves the tool.
WITH_DEFAULTS_MODES = ("report-all", "report-all-tagged", "trim", "explicit")


def _with_defaults_element(mode: str) -> ET.Element:
    """Build a ``<with-defaults>mode</with-defaults>`` element (RFC 6243).

    The element is the only child of ``<get>``/``<get-config>`` that lives in
    the with-defaults namespace, so we declare it as the element's default
    namespace (``xmlns=...``, no prefix) rather than letting ElementTree
    allocate an ``ns1:`` prefix. This matches the form in RFC 6243 §4.5.1.
    """
    if mode not in WITH_DEFAULTS_MODES:
        raise ValueError(
            f"invalid with-defaults mode {mode!r}; expected one of "
            f"{list(WITH_DEFAULTS_MODES)}"
        )
    el = ET.Element("with-defaults")
    el.set("xmlns", WITH_DEFAULTS_NS)
    el.text = mode
    return el


# Namespaced tag helpers -- ElementTree wants {uri}local for elements in a
# namespace; with `nc` registered this renders as <nc:local>.
def _nc(tag: str) -> str:
    return f"{{{_NETCONF_BASE}}}{tag}"


def edit_config(
    loader: Loader,
    module_name: str,
    root: str,
    data: Any,
    *,
    target: str = "running",
    operation: str | None = None,
    message_id: int = 101,
) -> str:
    """Build a full ``<rpc><edit-config>`` message.

    ``target`` is the datastore to edit (``running``, ``candidate``, ...).
    ``operation`` sets a default operation on the whole config (the per-node
    ``_operation`` in ``data`` still overrides it for individual nodes).
    """
    config_tree = build(loader, module_name, root, data, operation=operation)

    rpc = ET.Element(_nc("rpc"), attrib={"message-id": str(message_id)})
    edit = ET.SubElement(rpc, _nc("edit-config"))
    target_el = ET.SubElement(edit, _nc("target"))
    ET.SubElement(target_el, _nc(target))
    if operation is not None:
        edit.set(_nc("operation"), operation)

    # <config> carries the generated tree (its root is the top-level
    # container, e.g. <interfaces>), appended as a child of <config>.
    config_el = ET.SubElement(edit, _nc("config"))
    config_el.append(config_tree)
    return _pretty(rpc)


def get_config(
    *,
    target: str = "running",
    filter_element: ET.Element | None = None,
    with_defaults: str | None = None,
    message_id: int = 102,
) -> str:
    """Build a ``<rpc><get-config>`` message with an optional filter.

    ``filter_element`` is a complete ``<filter>`` element (the caller builds
    it, since filters vary widely). Omit it for a full retrieval.
    ``with_defaults`` adds a ``<with-defaults>`` parameter (RFC 6243) after
    the filter; one of ``report-all`` / ``report-all-tagged`` / ``trim`` /
    ``explicit``.
    """
    rpc = ET.Element(_nc("rpc"), attrib={"message-id": str(message_id)})
    get = ET.SubElement(rpc, _nc("get-config"))
    target_el = ET.SubElement(get, _nc("target"))
    ET.SubElement(target_el, _nc(target))
    if filter_element is not None:
        get.append(filter_element)
    if with_defaults is not None:
        get.append(_with_defaults_element(with_defaults))
    return _pretty(rpc)


def get(
    *,
    filter_element: ET.Element | None = None,
    with_defaults: str | None = None,
    message_id: int = 103,
) -> str:
    """Build a ``<rpc><get>`` message with an optional filter.

    Unlike :func:`get_config`, ``<get>`` retrieves from the running datastore
    merged with any current operational state -- there is no ``<target>``
    child (RFC 6241 §7.7). Omit ``filter_element`` for a full retrieval.
    ``with_defaults`` adds a ``<with-defaults>`` parameter (RFC 6243) after
    the filter; one of ``report-all`` / ``report-all-tagged`` / ``trim`` /
    ``explicit``.
    """
    rpc = ET.Element(_nc("rpc"), attrib={"message-id": str(message_id)})
    get_el = ET.SubElement(rpc, _nc("get"))
    if filter_element is not None:
        get_el.append(filter_element)
    if with_defaults is not None:
        get_el.append(_with_defaults_element(with_defaults))
    return _pretty(rpc)


# -- filter element construction --------------------------------------
#
# Two filter types are supported (RFC 6241 §6):
#   * subtree -- content match and subtree selection nodes; we build the
#     content by reusing the XML builder (a subtree filter's "selection
#     nodes" are exactly the data-tree fragments `build` already produces).
#   * xpath   -- a `select` attribute holding an XPath 1.0 expression.


def subtree_filter(
    loader: Loader,
    module_name: str,
    root: str,
    data: Any,
) -> ET.Element:
    """Build a ``<filter type="subtree">`` element (RFC 6241 §6.2).

    ``data`` is the selection subtree: it uses the same spec-data shape the
    XML builder takes (containers as mappings, lists as arrays of entries
    that may carry only their key leaves to select specific entries). The
    generated filter content is exactly what :func:`build` would emit, placed
    inside the ``<filter>`` element.
    """
    filter_el = ET.Element(_nc("filter"), attrib={"type": "subtree"})
    filter_el.append(build(loader, module_name, root, data))
    return filter_el


def xpath_filter(select: str) -> ET.Element:
    """Build a ``<filter type="xpath" select="...">`` element (RFC 6241 §6.4).

    ``select`` is an XPath 1.0 expression evaluated against the conceptual
    data tree. The caller is responsible for the expression's correctness;
    we do not validate it.
    """
    return ET.Element(_nc("filter"), attrib={"type": "xpath", "select": select})


def bare_config(
    loader: Loader,
    module_name: str,
    root: str,
    data: Any,
    *,
    operation: str | None = None,
) -> str:
    """Just the generated config fragment (no rpc envelope).

    Useful for pasting into an interactive netopeer session or composing
    into a larger message by hand.
    """
    return _pretty(build(loader, module_name, root, data, operation=operation))


def rpc_call(
    loader: Loader,
    module_name: str,
    rpc_name: str,
    data: Any,
    *,
    message_id: int = 101,
) -> str:
    """Build a ``<rpc><rpc-name>...</rpc-name></rpc>`` message (RFC 6241).

    ``rpc_name`` is the YANG rpc identifier (e.g. ``"get-config"``) and
    ``data`` is its input parameters as a mapping. The rpc element itself is
    generated by the XML builder (no ``<input>`` wrapper -- per RFC 6241 the
    input parameters sit directly under the rpc element) and is appended as
    the sole child of the ``<rpc>`` envelope.

    Unlike :func:`edit_config` there is no ``<config>``/``<target>`` wrapper;
    rpc input is the rpc element's content. Use ``message_id`` to set the
    ``message-id`` attribute (it should be unique within a session).
    """
    rpc_body = build(loader, module_name, rpc_name, data)
    rpc = ET.Element(_nc("rpc"), attrib={"message-id": str(message_id)})
    rpc.append(rpc_body)
    return _pretty(rpc)
