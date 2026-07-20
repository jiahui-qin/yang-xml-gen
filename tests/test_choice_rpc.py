"""Tests for choice/case flattening, augment, and rpc call generation.

These cover the step-4 additions:

  * ``choice``/``case`` are flattened -- the branch leaves appear directly
    under the parent with no ``<choice>``/``<case>`` element (RFC 7951 §7.9).
  * ``augment`` is already baked into pyang's ``i_children``: an augmented
    leaf carries the *augmenting* module's namespace, not the host's.
  * ``rpc`` is modelled as a node of kind ``"rpc"``; the call serialises as
    ``<rpc-name>input-children</rpc-name>`` (no ``<input>`` wrapper, RFC 6241)
    and ``rpc_call`` wraps it in a ``<rpc message-id=...>`` envelope.

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
from yang_xml_gen.scaffold import generate_template, template_to_json  # noqa: E402
from yang_xml_gen.schema import build_schema  # noqa: E402
from yang_xml_gen.xml_builder import build, build_fragment  # noqa: E402
from yang_xml_gen.wrappers import rpc_call  # noqa: E402

IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
NI_NS = "urn:ietf:params:xml:ns:yang:ietf-network-instance"
NC_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
NETCONF_BASE_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
WITH_DEFAULTS_NS = "urn:ietf:params:xml:ns:yang:ietf-netconf-with-defaults"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse(elem) -> ET.Element:
    """Round-trip a built element through a string so {ns}tag lookups work.

    Mirrors the helper in test_generator.py: the builder keeps xmlns as
    plain attributes on bare-tag elements, so in-memory {ns} paths don't
    match until we serialise and reparse.
    """
    return ET.fromstring(ET.tostring(elem, encoding="unicode"))


class ChoiceFlatteningTests(unittest.TestCase):
    """``choice``/``case`` must not produce XML elements of their own."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_rule_type_branch_leaves_are_direct_children_of_rule(self):
        # ietf-netconf-acm's `rule-type` choice has three cases whose leaves
        # (rpc-name / notification-name / path) must be flattened directly
        # under `rule`, with no `rule-type` node in between.
        rule = (
            build_schema(self.loader, "ietf-netconf-acm", "nacm")
            .child("rule-list")
            .child("rule")
        )
        self.assertEqual(rule.kind, "list")
        self.assertIn("rpc-name", rule.children)
        self.assertIn("notification-name", rule.children)
        self.assertIn("path", rule.children)
        # No synthetic choice/case node survives flattening.
        self.assertNotIn("rule-type", rule.children)
        for name in ("rpc-name", "notification-name", "path"):
            self.assertEqual(rule.children[name].kind, "leaf")

    def test_choice_branch_leaf_emits_no_choice_or_case_element(self):
        # Fill the rpc-name branch and check the XML: <rule> contains
        # <rpc-name>get</rpc-name> directly, with no <rule-type>/<case>.
        data = {
            "rule-list": [
                {
                    "name": "rl1",
                    "rule": [
                        {"name": "r1", "module-name": "*", "rpc-name": "get",
                         "access-operations": "exec", "action": "permit"},
                    ],
                }
            ]
        }
        elem = _parse(build(self.loader, "ietf-netconf-acm", "nacm", data))
        rule = elem.find(".//{urn:ietf:params:xml:ns:yang:ietf-netconf-acm}rule")
        self.assertIsNotNone(rule)
        rpc_name = rule.find(
            "{urn:ietf:params:xml:ns:yang:ietf-netconf-acm}rpc-name"
        )
        self.assertIsNotNone(rpc_name)
        self.assertEqual(rpc_name.text, "get")
        # No choice/case wrapper elements anywhere under the rule.
        child_tags = {_local(c.tag) for c in rule}
        self.assertNotIn("rule-type", child_tags)
        self.assertNotIn("case", child_tags)
        self.assertNotIn("choice", child_tags)

    def test_choice_branch_leaves_all_appear_in_template(self):
        # A template for a parent containing a choice lists every branch
        # leaf side by side; the user picks which to fill (mutual exclusion
        # is the device's job, not ours -- see the plan's "no semantic
        # validation" boundary).
        tpl = generate_template(self.loader, "ietf-netconf-acm", "nacm")
        rule_list_entry = tpl["data"]["rule-list"][0]
        rule_entry = rule_list_entry["rule"][0]
        for name in ("rpc-name", "notification-name", "path"):
            self.assertIn(name, rule_entry)


class AugmentNamespaceTests(unittest.TestCase):
    """An augmented leaf carries the augmenting module's namespace."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_bind_ni_name_uses_augmenting_module_namespace(self):
        # `bind-ni-name` is augmented into ietf-interfaces' `interface` by
        # ietf-network-instance. Its namespace must be ietf-network-instance's,
        # not ietf-interfaces' -- pyang already bakes this into i_children.
        iface = build_schema(self.loader, "ietf-interfaces", "interfaces").child(
            "interface"
        )
        bnn = iface.child("bind-ni-name")
        self.assertEqual(bnn.namespace, NI_NS)
        self.assertEqual(bnn.module_name, "ietf-network-instance")
        self.assertNotEqual(bnn.namespace, IF_NS)

    def test_bind_ni_name_emitted_in_augmenting_namespace(self):
        # End-to-end: the <bind-ni-name> element in the generated XML must
        # declare the ietf-network-instance namespace.
        data = {
            "interface": [
                {"name": "eth0", "type": "ethernetCsmacd", "bind-ni-name": "default"}
            ]
        }
        elem = _parse(build(self.loader, "ietf-interfaces", "interfaces", data))
        bnn = elem.find(f".//{{{NI_NS}}}bind-ni-name")
        self.assertIsNotNone(bnn, "bind-ni-name must be in the ietf-network-instance namespace")
        self.assertEqual(bnn.text, "default")


class RpcCallTests(unittest.TestCase):
    """``rpc`` is modelled and serialised per RFC 6241."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_get_config_rpc_is_modelled_with_input_children(self):
        rpc = build_schema(self.loader, "ietf-netconf", "get-config")
        self.assertEqual(rpc.kind, "rpc")
        self.assertEqual(rpc.namespace, NETCONF_BASE_NS)
        # input children: source (container, holding the flattened
        # config-source choice), filter (anyxml), with-defaults (leaf).
        self.assertIn("source", rpc.children)
        self.assertIn("filter", rpc.children)
        self.assertIn("with-defaults", rpc.children)
        # The config-source choice inside <source> is flattened too:
        # candidate/running/startup sit directly under source.
        source = rpc.children["source"]
        for name in ("candidate", "running", "startup"):
            self.assertIn(name, source.children)

    def test_get_config_serialises_without_input_wrapper(self):
        # Per RFC 6241 the rpc input parameters sit directly under the rpc
        # element -- no <input> wrapper. `running` is type empty, so its
        # presence serialises as <running/> with no text.
        data = {"source": {"running": True}, "with-defaults": "report-all"}
        elem = _parse(build(self.loader, "ietf-netconf", "get-config", data))
        self.assertEqual(_local(elem.tag), "get-config")
        # No <input> wrapper anywhere in the tree.
        self.assertIsNone(elem.find(".//input"))
        # source and its children sit in the netconf base namespace (they
        # belong to ietf-netconf, like the rpc itself).
        source = elem.find(f"{{{NETCONF_BASE_NS}}}source")
        self.assertIsNotNone(source)
        running = source.find(f"{{{NETCONF_BASE_NS}}}running")
        self.assertIsNotNone(running)
        self.assertIsNone(running.text)  # type empty -> no text
        # with-defaults is augmented from ietf-netconf-with-defaults.
        wd = elem.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertIsNotNone(wd)
        self.assertEqual(wd.text, "report-all")

    def test_rpc_call_wraps_in_rpc_envelope_with_message_id(self):
        data = {"source": {"running": True}}
        xml = rpc_call(self.loader, "ietf-netconf", "get-config", data, message_id=7)
        # Parse the pretty-printed string back.
        root = ET.fromstring(xml)
        self.assertEqual(_local(root.tag), "rpc")
        self.assertEqual(root.get("message-id"), "7")
        # The rpc body is the <get-config> element.
        children = list(root)
        self.assertEqual(len(children), 1)
        self.assertEqual(_local(children[0].tag), "get-config")

    def test_rpc_input_kept_in_template(self):
        # rpc parameters carry i_config=None in pyang (config does not apply),
        # which must NOT cause them to be dropped from a config-only template.
        tpl = json.loads(template_to_json(self.loader, "ietf-netconf", "get-config"))
        self.assertEqual(tpl["module"], "ietf-netconf")
        self.assertEqual(tpl["root"], "get-config")
        data = tpl["data"]
        self.assertIn("source", data)
        # The flattened config-source choice leaves are all present.
        for name in ("candidate", "running", "startup"):
            self.assertIn(name, data["source"])
        self.assertIn("filter", data)
        self.assertIn("with-defaults", data)

    def test_unknown_rpc_raises_key_error(self):
        # build_schema raises KeyError with a helpful "valid:" list when the
        # named top-level node (rpc or otherwise) doesn't exist.
        with self.assertRaises(KeyError) as cm:
            build_schema(self.loader, "ietf-netconf", "no-such-rpc")
        self.assertIn("no-such-rpc", str(cm.exception))
        self.assertIn("get-config", str(cm.exception))  # valid list includes rpcs


if __name__ == "__main__":
    unittest.main()
