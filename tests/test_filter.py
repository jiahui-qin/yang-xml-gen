"""Tests for NETCONF get/get-config filter construction (step 5).

Covers the read-path additions:

  * :func:`subtree_filter` builds a ``<filter type="subtree">`` whose content
    is a selection subtree produced by reusing the XML builder (RFC 6241
    §6.2) -- list entries may carry only their key leaves to select specific
    entries, and empty containers select whole subtrees.
  * :func:`xpath_filter` builds a ``<filter type="xpath" select="...">``
    element (RFC 6241 §6.4); the XPath expression is passed through verbatim.
  * :func:`get` / :func:`get_config` wrap a filter (or none, for full
    retrieval) in the ``<rpc>`` envelope -- ``<get>`` has no ``<target>``
    (§7.7), ``<get-config>`` does (§7.5).
  * ``with_defaults`` adds a ``<with-defaults>`` parameter (RFC 6243) after
    the filter; only the four enumeration modes are accepted, and the
    element is emitted in the with-defaults namespace.

These load the real models/ directory, so they depend on step 1's clean
compile.
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
from yang_xml_gen.wrappers import (  # noqa: E402
    WITH_DEFAULTS_MODES,
    WITH_DEFAULTS_NS,
    get as get_message,
    get_config as get_config_message,
    subtree_filter,
    xpath_filter,
)

NC_NS = "urn:ietf:params:netconf:base:1.0"
IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
IF_IP_NS = "urn:ietf:params:xml:ns:yang:ietf-ip"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_xml(xml: str) -> ET.Element:
    """Parse a pretty-printed XML string back into an element tree.

    The wrappers emit xmlns as namespace declarations, so parsing the string
    resolves them into {uri}local tags the way a NETCONF server would see.
    """
    return ET.fromstring(xml)


def _parse_element(elem: ET.Element) -> ET.Element:
    """Round-trip a single element through a string (for subtree_filter,
    which returns an Element, not a string)."""
    return ET.fromstring(ET.tostring(elem, encoding="unicode"))


class SubtreeFilterTests(unittest.TestCase):
    """``subtree_filter`` produces a ``<filter type="subtree">`` element."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_filter_element_has_subtree_type(self):
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces", {"interface": []}
        )
        f = _parse_element(f)
        self.assertEqual(_local(f.tag), "filter")
        self.assertEqual(f.get("type"), "subtree")

    def test_key_only_entry_selects_specific_list_entry(self):
        # A subtree filter with a list entry carrying only its key leaf
        # selects that specific entry (RFC 6241 §6.2.5: a leaf with content
        # is a "content match node").
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0"}]},
        )
        f = _parse_element(f)
        ifaces = f.find(f"{{{IF_NS}}}interfaces")
        self.assertIsNotNone(ifaces)
        iface = ifaces.find(f"{{{IF_NS}}}interface")
        self.assertIsNotNone(iface)
        name = iface.find(f"{{{IF_NS}}}name")
        self.assertIsNotNone(name)
        self.assertEqual(name.text, "eth0")
        # Only the key leaf is present -- nothing else leaked in.
        self.assertEqual([_local(c.tag) for c in iface], ["name"])

    def test_empty_container_selects_whole_subtree(self):
        # An empty container under a selected entry requests that whole
        # subtree (RFC 6241 §6.2.3: an empty container is a "selection node").
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "ipv4": {}}]},
        )
        f = _parse_element(f)
        ipv4 = f.find(f".//{{{IF_IP_NS}}}ipv4")
        self.assertIsNotNone(ipv4)
        # The ipv4 selection node carries no children.
        self.assertEqual(list(ipv4), [])

    def test_multiple_entries_select_each(self):
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0"}, {"name": "eth1"}]},
        )
        f = _parse_element(f)
        names = [
            n.text for n in f.iter(f"{{{IF_NS}}}name")
        ]
        self.assertEqual(names, ["eth0", "eth1"])


class XpathFilterTests(unittest.TestCase):
    """``xpath_filter`` produces a ``<filter type="xpath" select="...">``."""

    def test_filter_element_has_xpath_type_and_select(self):
        expr = '/if:interfaces/if:interface[if:name="eth0"]'
        f = xpath_filter(expr)
        f = _parse_element(f)
        self.assertEqual(_local(f.tag), "filter")
        self.assertEqual(f.get("type"), "xpath")
        self.assertEqual(f.get("select"), expr)

    def test_xpath_expression_passed_through_verbatim(self):
        # We do not validate the XPath; whatever the caller gives is emitted
        # as the select attribute, including odd-but-syntactically-fine text.
        expr = "/top/middle[node='x']/leaf"
        f = xpath_filter(expr)
        f = _parse_element(f)
        self.assertEqual(f.get("select"), expr)


class GetConfigMessageTests(unittest.TestCase):
    """``get_config`` wraps a filter in ``<rpc><get-config>`` (RFC 6241 §7.5)."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_full_retrieval_has_no_filter(self):
        xml = get_config_message(target="running", message_id=1)
        root = _parse_xml(xml)
        self.assertEqual(_local(root.tag), "rpc")
        self.assertEqual(root.get("message-id"), "1")
        get_cfg = root.find(f"{{{NC_NS}}}get-config")
        self.assertIsNotNone(get_cfg)
        target = get_cfg.find(f"{{{NC_NS}}}target")
        self.assertIsNotNone(target)
        self.assertIsNotNone(target.find(f"{{{NC_NS}}}running"))
        # No <filter> child for a full retrieval.
        self.assertIsNone(get_cfg.find(f"{{{NC_NS}}}filter"))

    def test_subtree_filter_appears_under_get_config(self):
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0"}]},
        )
        xml = get_config_message(filter_element=f, target="candidate", message_id=2)
        root = _parse_xml(xml)
        get_cfg = root.find(f"{{{NC_NS}}}get-config")
        flt = get_cfg.find(f"{{{NC_NS}}}filter")
        self.assertIsNotNone(flt)
        self.assertEqual(flt.get("type"), "subtree")
        # The selection subtree sits inside the filter.
        self.assertIsNotNone(flt.find(f"{{{IF_NS}}}interfaces"))
        # Target honoured.
        self.assertIsNotNone(
            get_cfg.find(f"{{{NC_NS}}}target/{{{NC_NS}}}candidate")
        )

    def test_xpath_filter_appears_under_get_config(self):
        f = xpath_filter("/if:interfaces")
        xml = get_config_message(filter_element=f, message_id=3)
        root = _parse_xml(xml)
        flt = root.find(f"{{{NC_NS}}}get-config/{{{NC_NS}}}filter")
        self.assertIsNotNone(flt)
        self.assertEqual(flt.get("type"), "xpath")
        self.assertEqual(flt.get("select"), "/if:interfaces")


class GetMessageTests(unittest.TestCase):
    """``get`` wraps a filter in ``<rpc><get>`` with no ``<target>`` (§7.7)."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_get_has_no_target(self):
        # <get> retrieves running + state; there is no <target> child.
        xml = get_message(message_id=4)
        root = _parse_xml(xml)
        get_el = root.find(f"{{{NC_NS}}}get")
        self.assertIsNotNone(get_el)
        self.assertIsNone(get_el.find(f"{{{NC_NS}}}target"))
        self.assertIsNone(get_el.find(f"{{{NC_NS}}}filter"))

    def test_get_with_subtree_filter(self):
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0", "ipv4": {}}]},
        )
        xml = get_message(filter_element=f, message_id=5)
        root = _parse_xml(xml)
        get_el = root.find(f"{{{NC_NS}}}get")
        flt = get_el.find(f"{{{NC_NS}}}filter")
        self.assertIsNotNone(flt)
        self.assertEqual(flt.get("type"), "subtree")
        # No target on <get>.
        self.assertIsNone(get_el.find(f"{{{NC_NS}}}target"))
        # Nested subtree selection honoured.
        self.assertIsNotNone(flt.find(f".//{{{IF_IP_NS}}}ipv4"))


class WithDefaultsTests(unittest.TestCase):
    """``with_defaults`` adds a ``<with-defaults>`` parameter (RFC 6243)."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_get_config_with_defaults_after_target(self):
        # No filter: <with-defaults> is the sole child after <target>.
        xml = get_config_message(with_defaults="report-all", message_id=1)
        root = _parse_xml(xml)
        get_cfg = root.find(f"{{{NC_NS}}}get-config")
        wd = get_cfg.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertIsNotNone(wd)
        self.assertEqual(wd.text, "report-all")
        # It must come after <target> (RFC 6241 §7.5 child order, with the
        # augmented parameter appended per RFC 6243 §4.5.1).
        children = [_local(c.tag) for c in get_cfg]
        self.assertEqual(children, ["target", "with-defaults"])

    def test_get_with_defaults_only_child(self):
        # No filter on <get>: <with-defaults> is the only child of <get>.
        xml = get_message(with_defaults="trim", message_id=2)
        root = _parse_xml(xml)
        get_el = root.find(f"{{{NC_NS}}}get")
        wd = get_el.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertIsNotNone(wd)
        self.assertEqual(wd.text, "trim")
        children = [_local(c.tag) for c in get_el]
        self.assertEqual(children, ["with-defaults"])

    def test_with_defaults_placed_after_filter(self):
        # With a filter present, <with-defaults> follows the filter.
        f = subtree_filter(
            self.loader, "ietf-interfaces", "interfaces",
            {"interface": [{"name": "eth0"}]},
        )
        xml = get_config_message(filter_element=f, with_defaults="explicit", message_id=3)
        root = _parse_xml(xml)
        get_cfg = root.find(f"{{{NC_NS}}}get-config")
        children = [_local(c.tag) for c in get_cfg]
        self.assertEqual(children, ["target", "filter", "with-defaults"])
        wd = get_cfg.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertEqual(wd.text, "explicit")

    def test_with_defaults_after_xpath_filter_on_get(self):
        f = xpath_filter("/if:interfaces")
        xml = get_message(filter_element=f, with_defaults="report-all-tagged", message_id=4)
        root = _parse_xml(xml)
        get_el = root.find(f"{{{NC_NS}}}get")
        children = [_local(c.tag) for c in get_el]
        self.assertEqual(children, ["filter", "with-defaults"])
        wd = get_el.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertEqual(wd.text, "report-all-tagged")

    def test_with_defaults_uses_its_own_namespace(self):
        # The element must live in the with-defaults namespace, not the
        # NETCONF base namespace -- RFC 6243 §4.5.1 declares it that way
        # (it's augmented in from ietf-netconf-with-defaults).
        xml = get_config_message(with_defaults="report-all")
        root = _parse_xml(xml)
        wd = root.find(f".//{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertIsNotNone(wd)
        # And there is no nc:with-defaults (the wrong-namespace form).
        get_cfg = root.find(f"{{{NC_NS}}}get-config")
        self.assertIsNone(get_cfg.find(f"{{{NC_NS}}}with-defaults"))

    def test_all_four_modes_accepted(self):
        for mode in WITH_DEFAULTS_MODES:
            xml = get_config_message(with_defaults=mode)
            root = _parse_xml(xml)
            wd = root.find(f".//{{{WITH_DEFAULTS_NS}}}with-defaults")
            self.assertEqual(wd.text, mode)

    def test_omitting_with_defaults_produces_no_element(self):
        # Regression: the default (no with_defaults) must not emit the element.
        xml = get_config_message()
        root = _parse_xml(xml)
        self.assertIsNone(root.find(f".//{{{WITH_DEFAULTS_NS}}}with-defaults"))
        xml2 = get_message()
        root2 = _parse_xml(xml2)
        self.assertIsNone(root2.find(f".//{{{WITH_DEFAULTS_NS}}}with-defaults"))

    def test_invalid_mode_rejected(self):
        # An unknown mode is caught before any XML is produced.
        with self.assertRaises(ValueError):
            get_config_message(with_defaults="bogus")
        with self.assertRaises(ValueError):
            get_message(with_defaults="report-all-extras")


class CliGetTests(unittest.TestCase):
    """End-to-end CLI paths for get-config / get (via main())."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()
        # main() reads the spec from a file; write temp specs to a temp dir.
        import tempfile
        cls._tmp = tempfile.mkdtemp()

    def _run(self, spec: dict) -> str:
        import io, json, contextlib
        from yang_xml_gen.cli import main
        path = Path(self._tmp) / f"spec_{id(spec)}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(path)])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def test_cli_get_config_subtree(self):
        xml = self._run({
            "module": "ietf-interfaces", "root": "interfaces",
            "wrap": "get-config", "message-id": 10,
            "filter": {"interface": [{"name": "eth0"}]},
        })
        root = _parse_xml(xml)
        self.assertEqual(root.get("message-id"), "10")
        flt = root.find(f"{{{NC_NS}}}get-config/{{{NC_NS}}}filter")
        self.assertEqual(flt.get("type"), "subtree")
        self.assertIsNotNone(flt.find(f"{{{IF_NS}}}interfaces"))

    def test_cli_get_config_xpath_no_module_root(self):
        # xpath filter needs neither module nor root.
        xml = self._run({
            "wrap": "get-config",
            "filter-select": "/if:interfaces",
        })
        root = _parse_xml(xml)
        flt = root.find(f"{{{NC_NS}}}get-config/{{{NC_NS}}}filter")
        self.assertEqual(flt.get("type"), "xpath")
        self.assertEqual(flt.get("select"), "/if:interfaces")

    def test_cli_get_full_retrieval(self):
        xml = self._run({"wrap": "get"})
        root = _parse_xml(xml)
        get_el = root.find(f"{{{NC_NS}}}get")
        self.assertIsNotNone(get_el)
        self.assertIsNone(get_el.find(f"{{{NC_NS}}}filter"))

    def test_cli_get_config_default_target_running(self):
        xml = self._run({
            "module": "ietf-interfaces", "root": "interfaces",
            "wrap": "get-config",
            "filter": {"interface": []},
        })
        root = _parse_xml(xml)
        tgt = root.find(f"{{{NC_NS}}}get-config/{{{NC_NS}}}target")
        self.assertIsNotNone(tgt)
        self.assertIsNotNone(tgt.find(f"{{{NC_NS}}}running"))

    def test_cli_both_filters_rejected(self):
        # Setting both `filter` and `filter-select` is a spec error: main()
        # calls p.error(), which raises SystemExit(2).
        from yang_xml_gen.cli import main
        import json
        path = Path(self._tmp) / "bad_both.json"
        path.write_text(json.dumps({
            "wrap": "get",
            "filter": {"interface": []},
            "filter-select": "/x",
        }), encoding="utf-8")
        with self.assertRaises(SystemExit):
            main([str(path)])

    def test_cli_subtree_filter_without_module_rejected(self):
        from yang_xml_gen.cli import main
        import json
        path = Path(self._tmp) / "bad_nomod.json"
        path.write_text(json.dumps({
            "root": "interfaces", "wrap": "get-config",
            "filter": {"interface": []},
        }), encoding="utf-8")
        with self.assertRaises(SystemExit):
            main([str(path)])

    def test_cli_with_defaults_on_get_config(self):
        # The `with-defaults` spec key flows through to the wrapper, landing
        # as the last child of <get-config> (after <target> and <filter>).
        xml = self._run({
            "module": "ietf-interfaces", "root": "interfaces",
            "wrap": "get-config", "message-id": 30, "target": "candidate",
            "filter": {"interface": [{"name": "eth0"}]},
            "with-defaults": "report-all",
        })
        root = _parse_xml(xml)
        get_cfg = root.find(f"{{{NC_NS}}}get-config")
        self.assertEqual(root.get("message-id"), "30")
        children = [_local(c.tag) for c in get_cfg]
        self.assertEqual(children, ["target", "filter", "with-defaults"])
        wd = get_cfg.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertEqual(wd.text, "report-all")
        # Target honoured alongside with-defaults.
        self.assertIsNotNone(
            get_cfg.find(f"{{{NC_NS}}}target/{{{NC_NS}}}candidate")
        )

    def test_cli_with_defaults_on_get_no_filter(self):
        # On <get> with no filter, <with-defaults> is the only child.
        xml = self._run({
            "wrap": "get", "with-defaults": "trim",
        })
        root = _parse_xml(xml)
        get_el = root.find(f"{{{NC_NS}}}get")
        children = [_local(c.tag) for c in get_el]
        self.assertEqual(children, ["with-defaults"])
        wd = get_el.find(f"{{{WITH_DEFAULTS_NS}}}with-defaults")
        self.assertEqual(wd.text, "trim")

    def test_cli_invalid_with_defaults_rejected(self):
        # An invalid mode surfaces as a ValueError from the wrapper (the CLI
        # does not pre-validate the value; the wrapper does).
        from yang_xml_gen.cli import main
        import json
        path = Path(self._tmp) / "bad_wd.json"
        path.write_text(json.dumps({
            "wrap": "get", "with-defaults": "no-such-mode",
        }), encoding="utf-8")
        with self.assertRaises(ValueError):
            main([str(path)])


if __name__ == "__main__":
    unittest.main()
