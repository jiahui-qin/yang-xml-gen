"""End-to-end tests for the YANG -> NETCONF XML generator.

These load the real models/ directory, so they depend on step 1 being
healthy (clean compile). They assert on the *structure* of the produced
XML rather than exact whitespace, using ElementTree to parse it back.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

# Make `src/` importable when tests are run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from yang_xml_gen.loader import Loader  # noqa: E402
from yang_xml_gen.xml_builder import build, build_fragment, BuildError, OPERATION_KEY  # noqa: E402
from yang_xml_gen.wrappers import bare_config, edit_config  # noqa: E402

IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
IANA_IF_NS = "urn:ietf:params:xml:ns:yang:iana-if-type"
NC_NS = "urn:ietf:params:netconf:base:1.0"

_ONE_INTERFACE = {
    "interface": [
        {"name": "eth0", "type": "ethernetCsmacd",
         "description": "uplink", "enabled": True}
    ]
}


def _local(tag: str) -> str:
    """Strip the {ns} prefix ElementTree puts on namespaced tags."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(elem) -> ET.Element:
    """Serialise a built element and parse it back.

    The builder keeps xmlns as plain attributes on bare-tag elements, so
    in-memory lookups with {ns} paths don't match. Round-tripping through a
    string lets ElementTree resolve namespaces the same way a NETCONF
    server would, which is what we actually want to assert on.
    """
    return ET.fromstring(ET.tostring(elem, encoding="unicode"))


class BuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    # -- happy path ----------------------------------------------------

    def test_container_with_list_emits_namespace_and_keys(self):
        elem = _parse(build(self.loader, "ietf-interfaces", "interfaces", _ONE_INTERFACE))
        # After round-trip, the namespace shows up on the tag, not as an
        # xmlns attribute (ElementTree consumes the declaration).
        self.assertEqual(elem.tag, f"{{{IF_NS}}}interfaces")

        ifaces = elem.findall(f"{{{IF_NS}}}interface")
        self.assertEqual(len(ifaces), 1)
        # Key leaf `name` must come first.
        children = [_local(c.tag) for c in ifaces[0]]
        self.assertEqual(children[0], "name")
        self.assertIn("type", children)
        self.assertIn("enabled", children)

    def test_identityref_value_gets_value_module_prefix(self):
        # Assert on the serialised string: the iana-if-type namespace must
        # be declared on the <type> element and the value prefixed with it.
        # This is exactly what the device receives, so it's the meaningful
        # check.
        xml = build_fragment(self.loader, "ietf-interfaces", "interfaces", _ONE_INTERFACE)
        self.assertIn(
            'xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type"', xml
        )
        self.assertIn("ianaift:ethernetCsmacd", xml)

    def test_boolean_serialised_as_yang_true_false(self):
        elem = _parse(build(self.loader, "ietf-interfaces", "interfaces", _ONE_INTERFACE))
        enabled = elem.find(f".//{{{IF_NS}}}enabled")
        self.assertIsNotNone(enabled)
        self.assertEqual(enabled.text, "true")

    # -- operation -----------------------------------------------------

    def test_operation_injected_as_nc_attribute(self):
        data = {"interface": [{"name": "eth0", OPERATION_KEY: "delete"}]}
        elem = _parse(build(self.loader, "ietf-interfaces", "interfaces", data))
        iface = elem.find(f"{{{IF_NS}}}interface")
        self.assertEqual(iface.get(f"{{{NC_NS}}}operation"), "delete")

    def test_invalid_operation_rejected(self):
        data = {"interface": [{"name": "eth0", OPERATION_KEY: "bogus"}]}
        with self.assertRaises(BuildError):
            build(self.loader, "ietf-interfaces", "interfaces", data)

    # -- error handling ------------------------------------------------

    def test_missing_list_key_rejected(self):
        data = {"interface": [{"type": "ethernetCsmacd"}]}  # no `name`
        with self.assertRaises(BuildError):
            build(self.loader, "ietf-interfaces", "interfaces", data)

    def test_unknown_child_rejected(self):
        data = {"interface": [{"name": "eth0", "no_such_leaf": 1}]}
        with self.assertRaises(BuildError):
            build(self.loader, "ietf-interfaces", "interfaces", data)

    def test_unknown_root_rejected(self):
        with self.assertRaises(KeyError):
            build(self.loader, "ietf-interfaces", "no_such_root", {})


class WrapperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_edit_config_envelope_structure(self):
        xml = edit_config(self.loader, "ietf-interfaces", "interfaces",
                          _ONE_INTERFACE, operation="merge")
        # Parses back cleanly.
        rpc = ET.fromstring(xml)
        self.assertEqual(_local(rpc.tag), "rpc")
        self.assertIsNotNone(rpc.get("message-id"))
        edit = rpc.find(f"{{{NC_NS}}}edit-config")
        self.assertIsNotNone(edit)
        self.assertEqual(edit.get(f"{{{NC_NS}}}operation"), "merge")
        running = rpc.find(f".//{{{NC_NS}}}running")
        self.assertIsNotNone(running)
        config = rpc.find(f".//{{{NC_NS}}}config")
        self.assertIsNotNone(config)
        # The generated <interfaces> sits inside <config>.
        self.assertEqual(_local(config[0].tag), "interfaces")

    def test_bare_config_has_no_rpc_envelope(self):
        xml = bare_config(self.loader, "ietf-interfaces", "interfaces", _ONE_INTERFACE)
        rpc = ET.fromstring(xml)
        self.assertEqual(_local(rpc.tag), "interfaces")  # no <rpc> wrapper


class FragmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_fragment_is_pretty_xml_with_declaration(self):
        xml = build_fragment(self.loader, "ietf-interfaces", "interfaces", _ONE_INTERFACE)
        self.assertTrue(xml.startswith("<?xml"))
        # Round-trips through a parser.
        ET.fromstring(xml)


if __name__ == "__main__":
    unittest.main()
