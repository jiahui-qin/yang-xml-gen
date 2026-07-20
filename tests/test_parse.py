"""Tests for the reverse parser: <rpc-reply> XML -> JSON spec-data (step 6).

These cover the inverse of the forward builder (:mod:`xml_builder`):

  * :func:`parse_reply` recognises the three NETCONF reply shapes --
    ``<data>`` (data-bearing), ``<ok/>``, and ``<rpc-error>`` (RFC 6241).
  * A data-bearing reply is walked schema-driven against the inferred
    module (namespace -> module via :class:`Loader`), reproducing the
    spec-data shape: containers as mappings, lists as arrays of entries,
    leaf-lists as arrays of scalars, leaves as scalars. The result
    round-trips -- it can be fed back to :func:`build` to re-emit the XML.
  * Type coercion is symmetric with the forward builder: ``boolean`` <-> bool,
    ``empty`` <-> True, identityref keeps its ``prefix:ident`` text, other
    types stay as their string text.
  * ``nc:operation`` round-trips as the ``_operation`` sentinel on
    containers/list-entries and on delete/remove leaves (mirrors the
    forward builder's sentinel handling).

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
from yang_xml_gen.xml_builder import build  # noqa: E402
from yang_xml_gen.xml_parser import ParseError, parse_reply  # noqa: E402

NC_NS = "urn:ietf:params:netconf:base:1.0"
IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"
IF_IP_NS = "urn:ietf:params:xml:ns:yang:ietf-ip"
NI_NS = "urn:ietf:params:xml:ns:yang:ietf-network-instance"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _reply(payload_xml: str, *, message_id: str = "101") -> str:
    """Wrap a payload element string in a <rpc-reply><data> envelope.

    The payload is whatever sits inside <data> (typically a single root
    element like <interfaces>...</interfaces>). The envelope uses the nc
    prefix for the NETCONF base namespace, matching what a device sends.
    """
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n'
        f'<nc:rpc-reply xmlns:nc="{NC_NS}" message-id="{message_id}">\n'
        f"  <nc:data>\n"
        f"{payload_xml}\n"
        f"  </nc:data>\n"
        f"</nc:rpc-reply>"
    )


def _reply_from_element(elem: ET.Element, *, message_id: str = "101") -> str:
    """Build a <rpc-reply><data> envelope around a built Element.

    Used by the round-trip tests: the forward builder produces an Element
    (e.g. <interfaces>); we serialise it and wrap it in a reply, then
    parse it back and compare to the original spec-data.
    """
    return _reply(ET.tostring(elem, encoding="unicode"), message_id=message_id)


class RoundTripTests(unittest.TestCase):
    """Forward build() -> wrap in reply -> parse_reply reproduces spec-data."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def _round_trip(self, data: dict) -> dict:
        elem = build(self.loader, "ietf-interfaces", "interfaces", data)
        reply = _reply_from_element(elem)
        return parse_reply(reply, self.loader, data_only=True)

    def test_list_with_multiple_entries_and_nested_container(self):
        # The canonical example: two interface entries, one with an ipv4
        # sub-tree carrying a nested list (address). The identityref `type`
        # uses its prefixed form (`ianaift:ethernetCsmacd`): the forward
        # builder emits the prefix and the reverse parser keeps it verbatim,
        # so the round-trip is only exact when the input already carries it.
        data = {
            "interface": [
                {
                    "name": "eth0",
                    "description": "uplink to core",
                    "type": "ianaift:ethernetCsmacd",
                    "enabled": True,
                    "ipv4": {
                        "enabled": True,
                        "address": [
                            {"ip": "10.0.0.1", "netmask": "255.255.255.0"},
                        ],
                    },
                },
                {"name": "eth1", "type": "ianaift:ethernetCsmacd", "enabled": True},
            ]
        }
        result = self._round_trip(data)
        self.assertEqual(result, data)

    def test_identityref_keeps_prefixed_text(self):
        # The forward builder emits <type>ianaift:ethernetCsmacd</type>
        # (resolving the identity's module prefix). The reverse parser
        # keeps that prefixed string verbatim -- the forward builder
        # accepts both bare and prefixed identityref values, so this
        # round-trips.
        data = {"interface": [{"name": "eth0", "type": "ethernetCsmacd"}]}
        result = self._round_trip(data)
        self.assertEqual(result["interface"][0]["type"], "ianaift:ethernetCsmacd")

    def test_boolean_round_trips_as_bool(self):
        # enabled is type boolean; forward emits "true"/"false", reverse
        # coerces back to Python bool.
        data = {
            "interface": [
                {"name": "eth0", "type": "ethernetCsmacd", "enabled": True},
                {"name": "eth1", "type": "ethernetCsmacd", "enabled": False},
            ]
        }
        result = self._round_trip(data)
        self.assertIs(result["interface"][0]["enabled"], True)
        self.assertIs(result["interface"][1]["enabled"], False)

    def test_leaf_list_round_trips_as_array(self):
        # higher-layer-if is a leaf-list; forward emits one sibling per
        # value, reverse collects them into an array.
        data = {
            "interface": [
                {"name": "eth0", "type": "ethernetCsmacd",
                 "higher-layer-if": ["eth0.0", "eth0.1"]},
            ]
        }
        result = self._round_trip(data)
        self.assertEqual(result["interface"][0]["higher-layer-if"],
                         ["eth0.0", "eth0.1"])

    def test_empty_type_leaf_becomes_true(self):
        # ietf-netconf get-config's <running/> is type empty; its presence
        # (no text) is the value. The reverse parser yields True, matching
        # the forward builder which emits <running/> for any truthy value.
        # Build the rpc element (kind="rpc") so the schema is walked.
        elem = build(self.loader, "ietf-netconf", "get-config",
                     {"source": {"running": True}})
        reply = _reply_from_element(elem)
        result = parse_reply(reply, self.loader, data_only=True)
        self.assertEqual(result, {"source": {"running": True}})

    def test_augmented_leaf_round_trips_in_own_namespace(self):
        # bind-ni-name is augmented into interface by ietf-network-instance;
        # it lives in NI_NS, not IF_NS. The schema lookup is by local name
        # (already flattened into interface.children), so the reverse parser
        # places it correctly regardless of its different namespace.
        data = {
            "interface": [
                {"name": "eth0", "type": "ethernetCsmacd",
                 "bind-ni-name": "default"},
            ]
        }
        result = self._round_trip(data)
        self.assertEqual(result["interface"][0]["bind-ni-name"], "default")

    def test_list_entry_delete_operation_round_trips(self):
        # A delete operation on a list entry round-trips as the _operation
        # sentinel on that entry (mirrors the forward builder).
        data = {"interface": [{"name": "eth0", "_operation": "delete"}]}
        result = self._round_trip(data)
        self.assertEqual(result, data)

    def test_leaf_delete_sentinel_round_trips(self):
        # A delete on a leaf (no value) round-trips as the leaf sentinel
        # {"_operation": "delete"} -- the exact form the forward builder
        # consumes to re-emit <name nc:operation="delete"/>.
        data = {
            "interface": [
                {"name": "eth0", "description": {"_operation": "delete"}},
            ]
        }
        result = self._round_trip(data)
        self.assertEqual(result, data)


class EnvelopeFormTests(unittest.TestCase):
    """The envelope form yields {module, root, data}; data_only yields just data."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def _build_reply(self, data: dict) -> str:
        elem = build(self.loader, "ietf-interfaces", "interfaces", data)
        return _reply_from_element(elem)

    def test_envelope_includes_module_and_root_inferred_from_namespace(self):
        # module/root come from the payload element's xmlns: <interfaces>
        # is in the ietf-interfaces namespace -> module "ietf-interfaces".
        reply = self._build_reply(
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd"}]}
        )
        result = parse_reply(reply, self.loader, data_only=False)
        self.assertEqual(result["module"], "ietf-interfaces")
        self.assertEqual(result["root"], "interfaces")
        self.assertIn("interface", result["data"])

    def test_data_only_omits_envelope(self):
        reply = self._build_reply(
            {"interface": [{"name": "eth0", "type": "ethernetCsmacd"}]}
        )
        result = parse_reply(reply, self.loader, data_only=True)
        self.assertNotIn("module", result)
        self.assertNotIn("root", result)
        self.assertIn("interface", result)

    def test_multi_root_data_only_returns_dict_keyed_by_root_name(self):
        # A full-retrieval reply may carry multiple top-level roots. The
        # data-only form returns {root_name: data, ...}; the envelope form
        # is single-root and must reject this.
        payload = (
            f'<interfaces xmlns="{IF_NS}"/>'
            f'<interfaces xmlns="{IF_NS}">'
            f'  <interface><name>eth0</name></interface>'
            f'</interfaces>'
        )
        reply = _reply(payload)
        result = parse_reply(reply, self.loader, data_only=True)
        self.assertIn("interfaces", result)
        self.assertEqual(result["interfaces"]["interface"][0]["name"], "eth0")

    def test_multi_root_envelope_rejected(self):
        payload = (
            f'<interfaces xmlns="{IF_NS}"/>'
            f'<interfaces xmlns="{IF_NS}"/>'
        )
        reply = _reply(payload)
        with self.assertRaises(ParseError) as cm:
            parse_reply(reply, self.loader, data_only=False)
        self.assertIn("multiple top-level roots", str(cm.exception))

    def test_empty_data_rejected_in_envelope_form(self):
        # No payload -> no module/root to infer. The data-only form yields
        # an empty object; the envelope form raises.
        reply = _reply("")
        with self.assertRaises(ParseError):
            parse_reply(reply, self.loader, data_only=False)
        self.assertEqual(parse_reply(reply, self.loader, data_only=True), {})


class OkAndErrorTests(unittest.TestCase):
    """<ok/> and <rpc-error> replies yield their own forms (RFC 6241)."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_ok_reply_yields_ok_true(self):
        reply = f'<rpc-reply xmlns="{NC_NS}"><ok/></rpc-reply>'
        self.assertEqual(parse_reply(reply, self.loader), {"ok": True})

    def test_ok_reply_ignores_data_only(self):
        # <ok/> has no module/root; data_only doesn't change the form.
        reply = f'<rpc-reply xmlns="{NC_NS}"><ok/></rpc-reply>'
        self.assertEqual(parse_reply(reply, self.loader, data_only=True), {"ok": True})

    def test_single_rpc_error_yields_list(self):
        # <rpc-error> children are protocol-level (NETCONF base ns), parsed
        # generically (no YANG schema). The result is a one-element list.
        reply = f'''<rpc-reply xmlns="{NC_NS}" message-id="101">
  <rpc-error>
    <error-type>application</error-type>
    <error-tag>unknown-element</error-tag>
    <error-severity>error</error-severity>
    <error-path>/ietf-interfaces:interfaces</error-path>
    <error-message xml:lang="en">unrecognized element 'foo'</error-message>
  </rpc-error>
</rpc-reply>'''
        result = parse_reply(reply, self.loader)
        self.assertIn("rpc-error", result)
        err = result["rpc-error"]
        self.assertIsInstance(err, list)
        self.assertEqual(len(err), 1)
        self.assertEqual(err[0]["error-type"], "application")
        self.assertEqual(err[0]["error-tag"], "unknown-element")
        self.assertEqual(err[0]["error-severity"], "error")
        self.assertEqual(err[0]["error-path"], "/ietf-interfaces:interfaces")
        self.assertEqual(err[0]["error-message"], "unrecognized element 'foo'")

    def test_multiple_rpc_errors_yield_list(self):
        reply = f'''<rpc-reply xmlns="{NC_NS}">
  <rpc-error>
    <error-type>application</error-type>
    <error-tag>unknown-element</error-tag>
    <error-severity>error</error-severity>
  </rpc-error>
  <rpc-error>
    <error-type>application</error-type>
    <error-tag>missing-element</error-tag>
    <error-severity>error</error-severity>
  </rpc-error>
</rpc-reply>'''
        result = parse_reply(reply, self.loader)
        self.assertEqual(len(result["rpc-error"]), 2)
        self.assertEqual(result["rpc-error"][0]["error-tag"], "unknown-element")
        self.assertEqual(result["rpc-error"][1]["error-tag"], "missing-element")


class ErrorCasesTests(unittest.TestCase):
    """Malformed or unexpected inputs raise ParseError or KeyError."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_non_rpc_reply_root_rejected(self):
        with self.assertRaises(ParseError) as cm:
            parse_reply("<foo/>", self.loader)
        self.assertIn("expected <rpc-reply>", str(cm.exception))

    def test_malformed_xml_rejected(self):
        with self.assertRaises(ParseError):
            parse_reply("<rpc-reply>not closed", self.loader)

    def test_empty_rpc_reply_rejected(self):
        reply = f'<rpc-reply xmlns="{NC_NS}"></rpc-reply>'
        with self.assertRaises(ParseError) as cm:
            parse_reply(reply, self.loader)
        self.assertIn("no child element", str(cm.exception))

    def test_unknown_rpc_reply_child_rejected(self):
        reply = f'<rpc-reply xmlns="{NC_NS}"><bogus/></rpc-reply>'
        with self.assertRaises(ParseError) as cm:
            parse_reply(reply, self.loader)
        self.assertIn("expected <data>, <ok>, or <rpc-error>", str(cm.exception))

    def test_unknown_leaf_under_payload_rejected(self):
        # An element not in the schema surfaces as a ParseError listing the
        # valid children (mirrors SchemaNode.child's helpful message).
        payload = f'<interfaces xmlns="{IF_NS}"><bogus-leaf/></interfaces>'
        with self.assertRaises(ParseError) as cm:
            parse_reply(_reply(payload), self.loader)
        self.assertIn("bogus-leaf", str(cm.exception))
        self.assertIn("interface", str(cm.exception))

    def test_repeated_leaf_rejected(self):
        # A leaf is single-valued; two same-named leaf siblings are an error
        # (not silently merged, not turned into an array).
        payload = (
            f'<interfaces xmlns="{IF_NS}">'
            f'  <interface><name>eth0</name><name>eth1</name></interface>'
            f'</interfaces>'
        )
        with self.assertRaises(ParseError) as cm:
            parse_reply(_reply(payload), self.loader)
        self.assertIn("appears 2 times", str(cm.exception))

    def test_repeated_container_rejected(self):
        # A container is single-valued too; two ipv4 siblings are an error.
        payload = (
            f'<interfaces xmlns="{IF_NS}">'
            f'  <interface>'
            f'    <name>eth0</name>'
            f'    <ipv4 xmlns="{IF_IP_NS}"/>'
            f'    <ipv4 xmlns="{IF_IP_NS}"/>'
            f'  </interface>'
            f'</interfaces>'
        )
        with self.assertRaises(ParseError) as cm:
            parse_reply(_reply(payload), self.loader)
        self.assertIn("appears 2 times", str(cm.exception))

    def test_unknown_namespace_raises_key_error(self):
        # A payload element whose xmlns matches no loaded module cannot be
        # schema-walked; the loader's namespace index raises KeyError.
        payload = '<foo xmlns="urn:bogus:ns"/>'
        with self.assertRaises(KeyError) as cm:
            parse_reply(_reply(payload), self.loader)
        self.assertIn("urn:bogus:ns", str(cm.exception))

    def test_payload_without_namespace_rejected(self):
        # A bare element with no xmlns has no namespace to infer a module
        # from -- the parser surfaces this explicitly.
        payload = "<interfaces/>"
        with self.assertRaises(ParseError) as cm:
            parse_reply(_reply(payload), self.loader)
        self.assertIn("no namespace", str(cm.exception))


class CliFromXmlTests(unittest.TestCase):
    """End-to-end CLI: `cli reply.xml --from-xml` emits JSON."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()
        import tempfile
        cls._tmp = tempfile.mkdtemp()

    def _run(self, args: list[str], *, write: str | None = None) -> str:
        import io
        import contextlib
        from yang_xml_gen.cli import main
        path = Path(self._tmp) / f"reply_{abs(hash(tuple(args)))}.xml"
        if write is not None:
            path.write_text(write, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main([str(path), *args])
        self.assertEqual(rc, 0, buf.getvalue())
        return buf.getvalue()

    def test_cli_envelope_form(self):
        elem = build(self.loader, "ietf-interfaces", "interfaces",
                     {"interface": [{"name": "eth0", "type": "ethernetCsmacd"}]})
        reply = _reply_from_element(elem, message_id="42")
        out = self._run(["--from-xml"], write=reply)
        result = json.loads(out)
        self.assertEqual(result["module"], "ietf-interfaces")
        self.assertEqual(result["root"], "interfaces")
        self.assertEqual(result["data"]["interface"][0]["name"], "eth0")

    def test_cli_data_only_form(self):
        elem = build(self.loader, "ietf-interfaces", "interfaces",
                     {"interface": [{"name": "eth0", "type": "ethernetCsmacd"}]})
        reply = _reply_from_element(elem)
        out = self._run(["--from-xml", "--data-only"], write=reply)
        result = json.loads(out)
        self.assertNotIn("module", result)
        self.assertEqual(result["interface"][0]["name"], "eth0")

    def test_cli_ok_reply(self):
        reply = f'<rpc-reply xmlns="{NC_NS}"><ok/></rpc-reply>'
        out = self._run(["--from-xml"], write=reply)
        self.assertEqual(json.loads(out), {"ok": True})

    def test_cli_rpc_error_reply(self):
        reply = f'''<rpc-reply xmlns="{NC_NS}">
  <rpc-error>
    <error-type>application</error-type>
    <error-tag>lock-denied</error-tag>
    <error-severity>error</error-severity>
  </rpc-error>
</rpc-reply>'''
        out = self._run(["--from-xml"], write=reply)
        result = json.loads(out)
        self.assertEqual(result["rpc-error"][0]["error-tag"], "lock-denied")

    def test_cli_parse_error_exits_nonzero(self):
        # A malformed reply surfaces as a non-zero exit (the helper writes
        # to stderr and returns 2), not a raised exception.
        import io
        import contextlib
        from yang_xml_gen.cli import main
        path = Path(self._tmp) / "bad.xml"
        path.write_text("<not-a-reply/>", encoding="utf-8")
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = main([str(path), "--from-xml"])
        self.assertNotEqual(rc, 0)
        self.assertIn("rpc-reply", buf_err.getvalue())

    def test_cli_from_xml_without_file_exits_nonzero(self):
        # --from-xml with no positional spec file: the CLI returns 2
        # (handled in _parse_xml_reply, not via argparse error).
        import io
        import contextlib
        from yang_xml_gen.cli import main
        buf_err = io.StringIO()
        with contextlib.redirect_stderr(buf_err):
            rc = main(["--from-xml"])
        self.assertEqual(rc, 2)
        self.assertIn("input XML file", buf_err.getvalue())


if __name__ == "__main__":
    unittest.main()
