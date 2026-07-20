"""Tests for the JSON template scaffolding and tolerant type serialisation.

These load the real models/ directory (so they depend on step 1's clean
compile). They cover three things:

  * generate_template produces the right structural skeleton and filters
    state (config false) nodes by default.
  * A filled-in template round-trips through the builder into NETCONF XML.
  * The tolerant serialisation handles empty / decimal64 / union leaves
    the way a device expects, without doing any value validation.
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
from yang_xml_gen.scaffold import generate_template, template_to_json  # noqa: E402
from yang_xml_gen.xml_builder import build, build_fragment  # noqa: E402

IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
IF_IP_NS = "urn:ietf:params:xml:ns:yang:ietf-ip"
# NB: the openconfig optical-amplifier namespace is genuinely misspelled
# "amplfier" (missing the second 'i') in the published model -- match it.
OC_AMP_NS = "http://openconfig.net/yang/optical-amplfier"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(elem) -> ET.Element:
    """Round-trip a built element through a string so {ns}tag lookups work.

    Mirrors the helper in test_generator.py: the builder keeps xmlns as
    plain attributes on bare-tag elements, so in-memory {ns} paths don't
    match until we serialise and reparse.
    """
    return ET.fromstring(ET.tostring(elem, encoding="unicode"))


class TemplateStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_config_template_has_spec_envelope(self):
        tpl = generate_template(self.loader, "ietf-interfaces", "interfaces")
        self.assertEqual(tpl["module"], "ietf-interfaces")
        self.assertEqual(tpl["root"], "interfaces")
        self.assertIsInstance(tpl["data"], dict)
        # Round-trips through json without error.
        json.loads(json.dumps(tpl))

    def test_list_becomes_single_placeholder_with_key_sentinel(self):
        data = generate_template(self.loader, "ietf-interfaces", "interfaces")["data"]
        ifaces = data["interface"]
        self.assertIsInstance(ifaces, list)
        self.assertEqual(len(ifaces), 1)  # single placeholder entry
        entry = ifaces[0]
        # The key leaf carries a placeholder value, not an empty string.
        self.assertEqual(entry["name"], "<name>")

    def test_state_leaves_omitted_by_default(self):
        data = generate_template(self.loader, "ietf-interfaces", "interfaces")["data"]
        entry = data["interface"][0]
        # oper-status / if-index / statistics are config=false on the
        # interface entry; they must be absent from a config template.
        for state_leaf in ("oper-status", "if-index", "statistics", "last-change"):
            self.assertNotIn(state_leaf, entry, f"{state_leaf} should be omitted")

    def test_state_leaves_present_when_include_state(self):
        tpl = generate_template(
            self.loader, "ietf-interfaces", "interfaces", include_state=True
        )
        entry = tpl["data"]["interface"][0]
        # The state leaves show up when explicitly requested.
        self.assertIn("oper-status", entry)
        self.assertIn("if-index", entry)
        self.assertIn("statistics", entry)
        # Their values are placeholders, not real data.
        self.assertEqual(entry["oper-status"], "")

    def test_leaf_list_placeholder_is_single_empty_entry(self):
        # ietf-interfaces has leaf-lists under state (higher-layer-if etc.).
        tpl = generate_template(
            self.loader, "ietf-interfaces", "interfaces", include_state=True
        )
        # Walk to a known state leaf-list if present; otherwise just confirm
        # the mechanism: any leaf-list skeleton is [""], not "".
        def has_leaf_list_placeholder(obj) -> bool:
            if isinstance(obj, dict):
                return any(has_leaf_list_placeholder(v) for v in obj.values())
            if isinstance(obj, list):
                # A leaf-list placeholder is a list whose single entry is "".
                if len(obj) == 1 and obj[0] == "":
                    return True
                return any(has_leaf_list_placeholder(v) for v in obj)
            return False
        self.assertTrue(
            has_leaf_list_placeholder(tpl["data"]),
            "expected at least one leaf-list placeholder [\"\"]",
        )

    def test_template_to_json_is_valid_json(self):
        text = template_to_json(self.loader, "ietf-interfaces", "interfaces")
        parsed = json.loads(text)
        self.assertEqual(parsed["module"], "ietf-interfaces")
        # Indented output is easier to edit by hand.
        self.assertIn("\n", text)


class TemplateToXmlTests(unittest.TestCase):
    """End-to-end: fill a generated template and feed it to the builder."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def _fill_interface_template(self) -> dict:
        """Take a config template and populate one interface entry."""
        tpl = generate_template(self.loader, "ietf-interfaces", "interfaces")
        entry = tpl["data"]["interface"][0]
        entry["name"] = "eth0"
        entry["type"] = "ethernetCsmacd"
        entry["description"] = "uplink"
        entry["enabled"] = True
        # Drop the nested ipv4/ipv6 subtrees to keep the assertion focused;
        # the empty strings they were given are valid placeholders too, but
        # a real edit would either fill or remove them.
        entry.pop("ipv4", None)
        entry.pop("ipv6", None)
        return tpl

    def test_filled_template_builds_into_namespaced_xml(self):
        tpl = self._fill_interface_template()
        elem = _parse(
            build(self.loader, tpl["module"], tpl["root"], tpl["data"])
        )
        self.assertEqual(elem.tag, f"{{{IF_NS}}}interfaces")
        ifaces = elem.findall(f"{{{IF_NS}}}interface")
        self.assertEqual(len(ifaces), 1)
        # Key leaf `name` comes first; the value we filled in survives.
        children = [_local(c.tag) for c in ifaces[0]]
        self.assertEqual(children[0], "name")
        self.assertEqual(ifaces[0].find(f"{{{IF_NS}}}name").text, "eth0")
        # Boolean leaf serialised as YANG's true/false, not Python's True.
        self.assertEqual(ifaces[0].find(f"{{{IF_NS}}}enabled").text, "true")


class TolerantTypeTests(unittest.TestCase):
    """Special leaf types must serialise without validation, device-friendly."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_empty_leaf_emits_empty_element(self):
        # `is-router` under interfaces/interface/ipv6/neighbor is an empty
        # state leaf. Its presence (not value) carries meaning, so the
        # element must have no text regardless of the input value.
        data = {
            "interface": [
                {"name": "eth0", "ipv6": {"neighbor": [
                    {"ip": "fe80::1", "is-router": True}
                ]}}
            ]
        }
        elem = _parse(build(self.loader, "ietf-interfaces", "interfaces", data))
        # `is-router` is defined in ietf-ip (the ipv6 subtree's module), so
        # look it up under that namespace, not ietf-interfaces.
        is_router = elem.find(f".//{{{IF_IP_NS}}}is-router")
        self.assertIsNotNone(is_router)
        self.assertIsNone(is_router.text)  # <is-router/> has no text

    def test_decimal64_leaf_serialised_as_given_string(self):
        # target-gain is decimal64 fraction-digits 2 in openconfig-optical-
        # amplifier. We must not reformat or validate; just emit the value.
        data = {"amplifiers": {"amplifier": [
            {"name": "amp1", "config": {"target-gain": "12.50"}}
        ]}}
        xml = build_fragment(
            self.loader, "openconfig-optical-amplifier", "optical-amplifier", data
        )
        # The textual value is preserved exactly as the user supplied it.
        self.assertIn("<target-gain>12.50</target-gain>", xml)

    def test_decimal64_accepts_numeric_value(self):
        # A bare number (not a string) is also tolerated; we just str() it.
        data = {"amplifiers": {"amplifier": [
            {"name": "amp1", "config": {"target-gain": 12.5}}
        ]}}
        elem = _parse(
            build(
                self.loader,
                "openconfig-optical-amplifier",
                "optical-amplifier",
                data,
            )
        )
        gain = elem.find(f".//{{{OC_AMP_NS}}}target-gain")
        self.assertIsNotNone(gain)
        self.assertEqual(gain.text, "12.5")

    def test_union_leaf_list_accepts_plain_string(self):
        # `group` under ietf-netconf-acm rule-list is a union leaf-list.
        # Unions are not unwrapped -- the user's string is emitted verbatim.
        data = {"rule-list": [{"name": ["admins"], "group": ["limited"]}]}
        xml = build_fragment(self.loader, "ietf-netconf-acm", "nacm", data)
        self.assertIn("<group>limited</group>", xml)


class ScaffoldErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_unknown_module_raises(self):
        with self.assertRaises(KeyError):
            generate_template(self.loader, "no-such-module", "whatever")

    def test_unknown_root_raises(self):
        with self.assertRaises(KeyError):
            generate_template(self.loader, "ietf-interfaces", "no-such-root")


if __name__ == "__main__":
    unittest.main()
