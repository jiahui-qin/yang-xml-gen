"""Tests for non-blocking YANG value validation (step 7, Tier 1).

Two layers are exercised:

  * :func:`validate_value` directly -- returns the list of warning strings
    for a (schema leaf, value) pair. We assert on the *contents* of that
    list (empty for valid, non-empty for each constraint kind).
  * :func:`emit_warnings` + the forward builder / reverse parser -- the
    warning strings must surface via :func:`warnings.warn` as
    :class:`YangValidationWarning` *without* blocking generation/parsing.

The validator's contract is "never raise, never block": a bad value
produces a warning, but ``build()`` still succeeds and emits XML. Tests
that want strict behaviour use ``warnings.catch_warnings`` +
``filterwarnings("error", ...)`` to confirm the warning category.

Fixtures are pulled from the real models/ tree (same as the other suites),
one canonical leaf per constraint kind:

  * enumeration : ietf-interfaces interface link-up-down-trap-enable
                  (enum: enabled, disabled)
  * uint32+range: ietf-interfaces interface ipv6 mtu (range "1280..max")
  * string+pattern: ietf-interfaces interface ipv4 address ip
                    (inet:ipv4-address-no-zone)
  * identityref : ietf-interfaces interface type (base interface-type)
  * union       : ietf-yang-library modules-state module revision
                  (union of pattern r'\\d{4}-\\d{2}-\\d{2}' + length 0..max)
  * union-of-bits: ietf-netconf-acm rule access-operations
                  (union of pattern + bits)
  * decimal64   : synthesised Decimal64TypeSpec(2) -- no pure decimal64
                  config leaf is shallow enough to reach ergonomically.
"""

from __future__ import annotations

import sys
import unittest
import warnings
from pathlib import Path
from xml.etree import ElementTree as ET

# Make `src/` importable when tests are run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pyang import types as pt  # noqa: E402

from yang_xml_gen.loader import Loader  # noqa: E402
from yang_xml_gen.schema import SchemaNode, TypeInfo, build_schema  # noqa: E402
from yang_xml_gen.validator import (  # noqa: E402
    YangValidationWarning,
    emit_warnings,
    validate_value,
)
from yang_xml_gen.xml_builder import build, build_fragment  # noqa: E402
from yang_xml_gen.xml_parser import parse_fragment  # noqa: E402

IF_NS = "urn:ietf:params:xml:ns:yang:ietf-interfaces"


def _node(loader: Loader, mod: str, root: str, *path: str) -> SchemaNode:
    """Walk to a schema leaf for direct validate_value tests."""
    node = build_schema(loader, mod, root)
    for p in path:
        node = node.child(p)
    return node


class ValidateValueTests(unittest.TestCase):
    """Direct validate_value() returns: [] for valid, [msg,...] for each
    constraint kind. Exercises every dispatch branch."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    # -- enumeration ---------------------------------------------------

    def test_enumeration_valid_value_no_warning(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "link-up-down-trap-enable")
        self.assertEqual(validate_value(node, "enabled", self.loader), [])
        self.assertEqual(validate_value(node, "disabled", self.loader), [])

    def test_enumeration_unknown_value_warns(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "link-up-down-trap-enable")
        warns = validate_value(node, "maybe", self.loader)
        self.assertEqual(len(warns), 1)
        self.assertIn("enumeration", warns[0])

    # -- range (uint32) ------------------------------------------------

    def test_uint32_in_range_no_warning(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "ipv6", "mtu")
        self.assertEqual(validate_value(node, "1500", self.loader), [])
        # Boundary: 1280 is the range floor ("1280..max").
        self.assertEqual(validate_value(node, "1280", self.loader), [])

    def test_uint32_below_range_warns(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "ipv6", "mtu")
        warns = validate_value(node, "100", self.loader)
        self.assertEqual(len(warns), 1)
        # Message must name the violated constraint kind.
        self.assertIn("range", warns[0])

    def test_uint32_non_integer_warns(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "ipv6", "mtu")
        warns = validate_value(node, "not-a-number", self.loader)
        self.assertEqual(len(warns), 1)

    # -- pattern (string) ----------------------------------------------

    def test_string_matching_pattern_no_warning(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "ipv4", "address", "ip")
        self.assertEqual(validate_value(node, "10.0.0.1", self.loader), [])

    def test_string_failing_pattern_warns(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "ipv4", "address", "ip")
        warns = validate_value(node, "999.999.999.999", self.loader)
        self.assertEqual(len(warns), 1)
        self.assertIn("pattern", warns[0])

    # -- identityref ----------------------------------------------------

    def test_identityref_derived_value_no_warning(self):
        # ethernetCsmacd is an identity in iana-if-type derived from
        # interface-type (the leaf's base) -> valid.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "type")
        self.assertEqual(
            validate_value(node, "ianaift:ethernetCsmacd", self.loader), []
        )
        # Bare form (no prefix) is also accepted by the resolver.
        self.assertEqual(
            validate_value(node, "ethernetCsmacd", self.loader), []
        )

    def test_identityref_unknown_identity_warns(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "type")
        warns = validate_value(node, "ianaift:not-a-real-type", self.loader)
        self.assertEqual(len(warns), 1)
        self.assertIn("not found", warns[0])

    def test_identityref_exists_but_not_derived_warns(self):
        # `running` is a real identity (ietf-datastores, base `datastore`)
        # but is NOT derived from `interface-type` -> derivation failure.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "type")
        warns = validate_value(node, "ds:running", self.loader)
        self.assertEqual(len(warns), 1)
        self.assertIn("not derived", warns[0])

    # -- union (pattern \d{4}-\d{2}-\d{2} + length 0..max) -------------

    def test_union_member_matches_no_warning(self):
        # revision is union(pattern \d{4}-\d{2}-\d{2}, length 0..max). A
        # well-formed date matches the first (pattern) member.
        node = _node(self.loader, "ietf-yang-library", "modules-state",
                     "module", "revision")
        self.assertEqual(
            validate_value(node, "2024-01-15", self.loader), []
        )

    def test_union_no_member_matches_warns(self):
        # 'not-a-date' fails the pattern AND, being non-empty, satisfies
        # only the length's lower bound (0) but length alone doesn't
        # accept arbitrary strings -- it constrains a string type, which
        # the value isn't lexically. pyang rejects it through both
        # members, so the union rejects.
        node = _node(self.loader, "ietf-yang-library", "modules-state",
                     "module", "revision")
        warns = validate_value(node, "not-a-date", self.loader)
        self.assertEqual(len(warns), 1)
        self.assertIn("union", warns[0].lower())

    # -- union of bits (access-operations) -----------------------------

    def test_union_of_bits_valid_names_no_warning(self):
        # access-operations is union(pattern, bits). A bits value (one or
        # more of create/read/update/delete/exec, space- or comma-separated)
        # matches the bits member.
        node = _node(self.loader, "ietf-netconf-acm", "nacm",
                     "rule-list", "rule", "access-operations")
        self.assertEqual(validate_value(node, "create", self.loader), [])
        self.assertEqual(
            validate_value(node, "create,read,update", self.loader), []
        )
        # YANG wire form (space-separated) also accepted.
        self.assertEqual(
            validate_value(node, "create read exec", self.loader), []
        )

    def test_union_of_bits_unknown_bit_warns(self):
        node = _node(self.loader, "ietf-netconf-acm", "nacm",
                     "rule-list", "rule", "access-operations")
        # "nonexistent" is not a declared bit and doesn't match the
        # pattern member either -> union rejects.
        warns = validate_value(node, "create,nonexistent", self.loader)
        self.assertEqual(len(warns), 1)

    # -- decimal64 (synthesised spec) ----------------------------------

    def test_decimal64_correct_precision_no_warning(self):
        node = self._decimal64_node(fraction_digits=2)
        self.assertEqual(validate_value(node, "12.50", self.loader), [])
        self.assertEqual(validate_value(node, "12.5", self.loader), [])
        # Integer-valued decimal64 is fine.
        self.assertEqual(validate_value(node, "12", self.loader), [])

    def test_decimal64_too_many_fraction_digits_warns(self):
        node = self._decimal64_node(fraction_digits=2)
        # fraction-digits=2 -> "12.345" has 3 fraction digits, too many.
        warns = validate_value(node, "12.345", self.loader)
        self.assertEqual(len(warns), 1)

    def _decimal64_node(self, *, fraction_digits: int) -> SchemaNode:
        """Build a fake leaf node carrying a Decimal64TypeSpec.

        No shallow decimal64 leaf exists in the bundled config trees, so
        we synthesise the TypeSpec pyang would have built and attach it to
        a minimal SchemaNode. validate_value only reads node.type.type_spec
        and node.module_name, so a hand-built node is sufficient.

        pyang's ``Decimal64TypeSpec.__init__`` calls
        ``int(fraction_digits.arg)`` -- i.e. it expects a *statement* with
        an ``.arg`` attribute, not a raw int. We wrap the int in a tiny
        stub that quacks like the statement pyang builds internally.
        """
        class _StubStmt:
            def __init__(self, arg):
                self.arg = arg

        spec = pt.Decimal64TypeSpec(_StubStmt(fraction_digits))
        type_info = TypeInfo(name="decimal64", type_spec=spec)
        return SchemaNode(
            name="synth-decimal",
            kind="leaf",
            namespace=IF_NS,
            module_name="ietf-interfaces",
            children={},
            type=type_info,
        )

    # -- skip cases (no warning, never raise) --------------------------

    def test_delete_sentinel_skipped(self):
        # A leaf carrying a delete/remove sentinel has no value to
        # validate; validate_value must return [] without raising.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "link-up-down-trap-enable")
        self.assertEqual(
            validate_value(node, {"_operation": "delete"}, self.loader), []
        )

    def test_container_node_skipped(self):
        # Non-leaf nodes have no value type.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "ipv4")
        self.assertEqual(validate_value(node, "anything", self.loader), [])

    def test_boolean_value_handled(self):
        # bool values are normalised to "true"/"false" before pyang sees
        # them; a boolean leaf with a Python bool must not warn.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "enabled")
        self.assertEqual(validate_value(node, True, self.loader), [])
        self.assertEqual(validate_value(node, False, self.loader), [])

    def test_never_raises_on_unexpected_input(self):
        # The contract is "never raise". Feed it nonsense shapes that
        # might trip up pyang's str_to_val; each must return a list
        # (possibly non-empty) rather than raising.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "type")
        for v in [None, [], 3.14, object()]:
            result = validate_value(node, v, self.loader)
            self.assertIsInstance(result, list)


class EmitWarningsTests(unittest.TestCase):
    """emit_warnings() drives warnings.warn with the right category and
    stacklevel. The category is what callers filter on to make validation
    strict."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_valid_value_emits_no_warning(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "link-up-down-trap-enable")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            emit_warnings(node, "enabled", self.loader)
        self.assertEqual(
            [w for w in caught if issubclass(w.category, YangValidationWarning)],
            [],
        )

    def test_invalid_value_emits_yang_validation_warning(self):
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "link-up-down-trap-enable")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            emit_warnings(node, "maybe", self.loader)
        yang_warns = [
            w for w in caught if issubclass(w.category, YangValidationWarning)
        ]
        self.assertEqual(len(yang_warns), 1)
        # Message names the leaf and the constraint.
        msg = str(yang_warns[0].message)
        self.assertIn("link-up-down-trap-enable", msg)
        self.assertIn("enumeration", msg)

    def test_filterwarnings_error_makes_warning_raise(self):
        # The documented escape hatch: turning the category into an error
        # makes a bad value abort. This is the "strict mode" the README
        # describes.
        node = _node(self.loader, "ietf-interfaces", "interfaces",
                     "interface", "link-up-down-trap-enable")
        with warnings.catch_warnings():
            warnings.simplefilter("always")
            warnings.filterwarnings(
                "error", category=YangValidationWarning
            )
            with self.assertRaises(YangValidationWarning):
                emit_warnings(node, "maybe", self.loader)


class BuilderIntegrationTests(unittest.TestCase):
    """The forward builder emits YangValidationWarning but does NOT block:
    build() returns an Element regardless. Both directions (build and
    parse) share the same emit_warnings chokepoint, so we cover the
    forward path here and the reverse path in ParseIntegrationTests."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_bad_identityref_warns_but_build_succeeds(self):
        # type=ds:running -> `running` is a real identity (ietf-datastores)
        # so namespace resolution succeeds and XML is produced, BUT it's
        # not derived from the leaf's base `interface-type`, so the
        # validator warns. This is the case where "non-blocking" is
        # observable end-to-end: a completely unknown identity would
        # still raise BuildError (the builder needs a namespace to
        # declare; that's a structural failure, not a type-constraint
        # violation).
        data = {"interface": [
            {"name": "eth0", "type": "ds:running"}
        ]}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            elem = build(self.loader, "ietf-interfaces", "interfaces", data)
        yang_warns = [
            w for w in caught if issubclass(w.category, YangValidationWarning)
        ]
        self.assertEqual(len(yang_warns), 1)
        self.assertIn("running", str(yang_warns[0].message))
        # The element was still built and carries the (derivation-bad)
        # value, with the ietf-datastores namespace declared.
        xml = ET.tostring(elem, encoding="unicode")
        self.assertIn("ds:running", xml)

    def test_valid_identityref_emits_no_warning(self):
        data = {"interface": [
            {"name": "eth0", "type": "ianaift:ethernetCsmacd"}
        ]}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            build(self.loader, "ietf-interfaces", "interfaces", data)
        self.assertEqual(
            [w for w in caught if issubclass(w.category, YangValidationWarning)],
            [],
        )

    def test_out_of_range_mtu_warns_but_build_succeeds(self):
        data = {"interface": [
            {"name": "eth0", "type": "ianaift:ethernetCsmacd",
             "ipv6": {"mtu": "100"}}
        ]}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            elem = build(self.loader, "ietf-interfaces", "interfaces", data)
        yang_warns = [
            w for w in caught if issubclass(w.category, YangValidationWarning)
        ]
        self.assertEqual(len(yang_warns), 1)
        self.assertIn("mtu", str(yang_warns[0].message))
        xml = ET.tostring(elem, encoding="unicode")
        self.assertIn("100", xml)

    def test_delete_sentinel_does_not_trigger_validation(self):
        # Deleting a leaf must not produce an "empty value violates
        # enumeration" warning -- the sentinel carries no value.
        data = {"interface": [
            {"name": "eth0", "type": "ianaift:ethernetCsmacd",
             "description": {"_operation": "delete"}}
        ]}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            build(self.loader, "ietf-interfaces", "interfaces", data)
        self.assertEqual(
            [w for w in caught if issubclass(w.category, YangValidationWarning)],
            [],
        )


class ParseIntegrationTests(unittest.TestCase):
    """The reverse parser emits YangValidationWarning for bad values in
    inbound XML, symmetric with the forward builder. Parsing still
    succeeds and returns the (bad) value as-is."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_reverse_bad_enum_warns_but_parse_succeeds(self):
        # Build a fragment with a bogus enum value, then parse it back.
        # The forward build itself would warn; to isolate the reverse
        # path we craft the XML by hand so only parse_fragment runs.
        xml = (
            f'<interfaces xmlns="{IF_NS}">'
            '<interface>'
            '<name>eth0</name>'
            '<type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">'
            'ianaift:ethernetCsmacd</type>'
            '<link-up-down-trap-enable>bogus</link-up-down-trap-enable>'
            '</interface>'
            '</interfaces>'
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = parse_fragment(xml, self.loader)
        yang_warns = [
            w for w in caught if issubclass(w.category, YangValidationWarning)
        ]
        self.assertEqual(len(yang_warns), 1)
        self.assertIn("link-up-down-trap-enable", str(yang_warns[0].message))
        # Parse returned the bad value verbatim (no coercion, no drop).
        self.assertEqual(
            result["interface"][0]["link-up-down-trap-enable"], "bogus"
        )

    def test_reverse_valid_value_emits_no_warning(self):
        xml = (
            f'<interfaces xmlns="{IF_NS}">'
            '<interface>'
            '<name>eth0</name>'
            '<type xmlns:ianaift="urn:ietf:params:xml:ns:yang:iana-if-type">'
            'ianaift:ethernetCsmacd</type>'
            '<link-up-down-trap-enable>enabled</link-up-down-trap-enable>'
            '</interface>'
            '</interfaces>'
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            parse_fragment(xml, self.loader)
        self.assertEqual(
            [w for w in caught if issubclass(w.category, YangValidationWarning)],
            [],
        )


class RoundTripValidationTests(unittest.TestCase):
    """A spec with a bad value, built to XML and parsed back, must survive
    the round trip (warnings both ways, value preserved). This pins the
    'validation never blocks' contract end-to-end."""

    @classmethod
    def setUpClass(cls):
        cls.loader = Loader()

    def test_bad_value_round_trips_through_build_then_parse(self):
        data = {"interface": [
            {"name": "eth0", "type": "ianaift:ethernetCsmacd",
             "link-up-down-trap-enable": "bogus"}
        ]}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", YangValidationWarning)
            xml = build_fragment(
                self.loader, "ietf-interfaces", "interfaces", data
            )
        # parse_fragment on the produced XML re-validates and warns again,
        # but the value comes back unchanged.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = parse_fragment(xml, self.loader)
        self.assertTrue(any(
            issubclass(w.category, YangValidationWarning) for w in caught
        ))
        self.assertEqual(
            result["interface"][0]["link-up-down-trap-enable"], "bogus"
        )


if __name__ == "__main__":
    unittest.main()
