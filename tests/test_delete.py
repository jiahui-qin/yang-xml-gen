"""Tests for node deletion within ``<edit-config>``.

These cover the delete capability exposed via the ``_operation`` sentinel
(RFC 6241 §7.2). Three delete shapes are supported:

  * **list entry** -- ``{"_operation": "delete"}`` on a list item, with its
    key leaves, yields ``<list-item nc:operation="delete"><key>...</key>...
    </list-item>``; the server deletes the matching entry.
  * **container / subtree** -- ``{"_operation": "delete"}`` on a container
    yields ``<container nc:operation="delete"/>``; the server deletes the
    whole subtree.
  * **leaf** -- ``{"_operation": "delete"}`` (or ``"remove"``) on a leaf
    yields ``<leaf nc:operation="delete"/>`` with no text; the server
    deletes that leaf.

``delete`` (RFC 6241) errors if the node is absent; ``remove`` (RFC 6241
§7.2, the NETCONF ``remove`` operation) is lenient. Both serialise
identically here -- the difference is server-side.

A sentinel-only dict on a leaf for any other operation (``merge`` /
``replace`` / ``create``) is meaningless -- those need a value -- and is
rejected.

They load the real models/ directory, so they depend on step 1's clean
compile.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

# Make `src/` importable when tests are run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yang_xml_gen.loader import Loader  # noqa: E402
from yang_xml_gen.xml_builder import BuildError, build  # noqa: E402
from yang_xml_gen.wrappers import edit_config  # noqa: E402

IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
IP_NS = "urn:ietf:params:xml:ns:yang:ietf-ip"
NC_NS = "urn:ietf:params:netconf:base:1.0"
NC_OP = f"{{{NC_NS}}}operation"

# Paths inside an <edit-config> rpc: config > interfaces > interface > ...
IF_ETH0 = f"{{{IF_NS}}}interfaces/{{{IF_NS}}}interface"
IF_ETH0_IPV4 = f"{IF_ETH0}/{{{IP_NS}}}ipv4"
IF_ETH0_DESC = f"{IF_ETH0}/{{{IF_NS}}}description"

# Paths inside a bare build() result: the root IS <interfaces>, so the
# interface sits directly under it.
BARE_ETH0 = f"{{{IF_NS}}}interface"
BARE_ETH0_DESC = f"{{{IF_NS}}}description"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(xml: str) -> ET.Element:
    """Parse a serialised message so {ns}tag lookups work."""
    return ET.fromstring(xml)


def _config_root(rpc_elem: ET.Element) -> ET.Element:
    """Reach the ``<config>`` child element of an edit-config rpc."""
    edit = rpc_elem.find(f"{{{NC_NS}}}edit-config")
    return edit.find(f"{{{NC_NS}}}config")


class DeleteListEntryTests(unittest.TestCase):
    """``_operation: delete`` on a list entry."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_entry_carries_operation_attribute(self):
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "_operation": "delete"}]},
            message_id=1,
        )
        rpc = _parse(xml)
        iface = _config_root(rpc).find(IF_ETH0)
        self.assertEqual(iface.get(NC_OP), "delete")

    def test_entry_emits_key_leaves_only_when_key_only(self):
        # A key-only delete entry carries just the key leaf(s), no other
        # children -- the minimal instruction that identifies the entry.
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "_operation": "delete"}]},
            message_id=2,
        )
        iface = _config_root(_parse(xml)).find(IF_ETH0)
        self.assertEqual(iface.get(NC_OP), "delete")
        children = [_local(c.tag) for c in iface]
        self.assertEqual(children, ["name"])
        self.assertEqual(iface.find(f"{{{IF_NS}}}name").text, "eth0")

    def test_entry_emits_non_key_leaves_when_present(self):
        # If the caller supplies non-key leaves alongside the operation,
        # they are still emitted (the operation still applies to the whole
        # entry); the server decides what to do with them.
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "_operation": "delete"}]},
            message_id=3,
        )
        iface = _config_root(_parse(xml)).find(IF_ETH0)
        children = [_local(c.tag) for c in iface]
        self.assertIn("name", children)
        self.assertIn("type", children)
        self.assertEqual(iface.get(NC_OP), "delete")


class DeleteMultipleEntriesTests(unittest.TestCase):
    """Deleting several list entries in one edit-config (RFC 6241 §7.2).

    A list maps to an array; each entry that carries ``_operation: delete``
    becomes its own ``<list-item nc:operation="delete">`` sibling. This is
    the standard NETCONF form -- each entry is an independent operation
    target, so multi-entry delete is just multiple single-entry deletes in
    one config tree.
    """

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def _interface_elems(self, xml: str) -> list:
        return _config_root(_parse(xml)).findall(IF_ETH0)

    def test_three_entries_each_carry_delete(self):
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [
                {"name": "eth0", "_operation": "delete"},
                {"name": "eth1", "_operation": "delete"},
                {"name": "eth2", "_operation": "delete"},
            ]},
            message_id=40,
        )
        ifaces = self._interface_elems(xml)
        self.assertEqual(len(ifaces), 3)
        # Every entry carries the operation and only its key leaf.
        for iface in ifaces:
            self.assertEqual(iface.get(NC_OP), "delete")
            self.assertEqual([_local(c.tag) for c in iface], ["name"])

    def test_entry_names_preserve_input_order(self):
        # The order of <interface> siblings follows the array order; the
        # server processes them in document order.
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [
                {"name": "eth2", "_operation": "delete"},
                {"name": "eth0", "_operation": "delete"},
                {"name": "eth1", "_operation": "delete"},
            ]},
            message_id=41,
        )
        names = [
            iface.find(f"{{{IF_NS}}}name").text
            for iface in self._interface_elems(xml)
        ]
        self.assertEqual(names, ["eth2", "eth0", "eth1"])

    def test_mix_of_deleted_and_normal_entries(self):
        # A delete entry and a normal (merge) entry can coexist in one
        # edit-config: the merge entry has no operation, the delete entry
        # carries nc:operation="delete".
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [
                {"name": "eth0", "_operation": "delete"},
                {"name": "eth1", "type": "ethernetCsmacd", "enabled": True},
            ]},
            message_id=42,
        )
        ifaces = self._interface_elems(xml)
        self.assertEqual(len(ifaces), 2)
        self.assertEqual(ifaces[0].get(NC_OP), "delete")
        self.assertIsNone(ifaces[1].get(NC_OP))  # default merge, no attr
        self.assertEqual(ifaces[1].find(f"{{{IF_NS}}}name").text, "eth1")


class DeleteContainerTests(unittest.TestCase):
    """``_operation: delete`` on a container deletes the whole subtree."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_container_gets_operation_and_no_children(self):
        # Deleting a container yields <container nc:operation="delete"/> with
        # no children -- the server removes the entire subtree.
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "ipv4": {"_operation": "delete"}}]},
            message_id=10,
        )
        ipv4 = _config_root(_parse(xml)).find(IF_ETH0_IPV4)
        self.assertEqual(ipv4.get(NC_OP), "delete")
        self.assertEqual(list(ipv4), [])  # no children

    def test_container_namespace_is_defining_module(self):
        # ipv4 is defined in ietf-ip, not ietf-interfaces; the operation
        # must sit on an element in the correct namespace.
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "ipv4": {"_operation": "delete"}}]},
            message_id=11,
        )
        ipv4 = _config_root(_parse(xml)).find(IF_ETH0_IPV4)
        self.assertIsNotNone(ipv4)  # namespace matched


class DeleteLeafTests(unittest.TestCase):
    """``_operation: delete`` / ``remove`` on a single leaf."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_delete_leaf_has_no_text(self):
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "description": {"_operation": "delete"}}]},
            message_id=20,
        )
        desc = _config_root(_parse(xml)).find(IF_ETH0_DESC)
        self.assertEqual(desc.get(NC_OP), "delete")
        self.assertIsNone(desc.text)

    def test_remove_leaf_serialises_like_delete(self):
        # `remove` is the lenient variant (no error if absent) but
        # serialises identically to `delete`.
        xml = edit_config(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "description": {"_operation": "remove"}}]},
            message_id=21,
        )
        desc = _config_root(_parse(xml)).find(IF_ETH0_DESC)
        self.assertEqual(desc.get(NC_OP), "remove")
        self.assertIsNone(desc.text)

    def test_merge_sentinel_on_leaf_without_value_is_rejected(self):
        # merge/replace/create need a value; a sentinel-only dict is
        # meaningless and must be rejected.
        for op in ("merge", "replace", "create"):
            with self.subTest(op=op):
                with self.assertRaises(BuildError) as cm:
                    edit_config(
                        self.loader, "ietf-interfaces", "interfaces",
                        {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                                        "description": {"_operation": op}}]},
                        message_id=22,
                    )
                self.assertIn("description", str(cm.exception))
                self.assertIn(op, str(cm.exception))


class DeleteInBareBuildTests(unittest.TestCase):
    """The same behaviour at the ``build()`` (fragment) level, no envelope."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_bare_build_delete_entry(self):
        elem = build(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "_operation": "delete"}]},
        )
        reparsed = ET.fromstring(ET.tostring(elem, encoding="unicode"))
        iface = reparsed.find(BARE_ETH0)
        self.assertEqual(iface.get(NC_OP), "delete")

    def test_bare_build_delete_leaf(self):
        elem = build(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                            "description": {"_operation": "delete"}}]},
        )
        reparsed = ET.fromstring(ET.tostring(elem, encoding="unicode"))
        desc = reparsed.find(f"{BARE_ETH0}/{BARE_ETH0_DESC}")
        self.assertEqual(desc.get(NC_OP), "delete")
        self.assertIsNone(desc.text)


class CliDeleteTests(unittest.TestCase):
    """End-to-end CLI paths for deletion via ``--wrap edit-config``."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.mkdtemp()

    def _run(self, spec: dict) -> str:
        import io, contextlib
        from yang_xml_gen.cli import main
        path = Path(self._tmp) / f"spec_{id(spec)}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(path)])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def test_cli_delete_entry(self):
        xml = self._run({
            "module": "ietf-interfaces", "root": "interfaces",
            "wrap": "edit-config", "message-id": 30,
            "data": {"interface": [{"name": "eth0", "_operation": "delete"}]},
        })
        rpc = _parse(xml)
        self.assertEqual(rpc.get("message-id"), "30")
        iface = _config_root(rpc).find(IF_ETH0)
        self.assertEqual(iface.get(NC_OP), "delete")
        self.assertEqual([_local(c.tag) for c in iface], ["name"])

    def test_cli_delete_leaf(self):
        xml = self._run({
            "module": "ietf-interfaces", "root": "interfaces",
            "wrap": "edit-config", "message-id": 31,
            "data": {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                                    "description": {"_operation": "delete"}}]},
        })
        desc = _config_root(_parse(xml)).find(IF_ETH0_DESC)
        self.assertEqual(desc.get(NC_OP), "delete")
        self.assertIsNone(desc.text)

    def test_cli_delete_container(self):
        xml = self._run({
            "module": "ietf-interfaces", "root": "interfaces",
            "wrap": "edit-config", "message-id": 32,
            "data": {"interface": [{"name": "eth0", "type": "ethernetCsmacd",
                                    "ipv4": {"_operation": "delete"}}]},
        })
        ipv4 = _config_root(_parse(xml)).find(IF_ETH0_IPV4)
        self.assertEqual(ipv4.get(NC_OP), "delete")
        self.assertEqual(list(ipv4), [])


if __name__ == "__main__":
    unittest.main()
