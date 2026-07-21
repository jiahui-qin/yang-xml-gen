"""Validate leaf values against YANG type constraints.

This is a *non-blocking* check layer: :func:`validate_value` returns a list
of human-readable warning strings (empty when the value is valid). The
forward XML builder and the reverse parser call it and emit each warning via
:func:`warnings.warn` (using :class:`YangValidationWarning`), but they never
raise -- generation/parsing always proceeds. Callers who want strict
behaviour can turn the warning into an error with
``warnings.filterwarnings("error", category=YangValidationWarning)``.

The validator walks pyang's fully-resolved ``i_type_spec`` (stored on
:class:`~yang_xml_gen.schema.TypeInfo` as ``type_spec``) and isinstance-
dispatches on the concrete TypeSpec subclass. pyang's own ``str_to_val`` and
``validate`` methods do the heavy lifting (range/length/pattern/enum/
identityref-derivation/decimal64-precision/union/bits); we just drive them
with throwaway ``[]`` error lists so probes don't accumulate state, and
translate "did not validate" into a warning string.

Covered constraint kinds (one warning each, naming the violated constraint):

  * ``range`` (integers, decimal64) -- value outside a declared range.
  * ``length`` (strings, binary) -- string length outside a declared length.
  * ``pattern`` (strings) -- value fails an XSD pattern.
  * ``enumeration`` -- value is not one of the declared enum names.
  * ``identityref`` -- value does not resolve to an identity derived from
    the leaf's base (covers both "identity not found" and "not derived").
  * ``decimal64`` -- too many fraction digits (caught by ``str_to_val``).
  * ``bits`` -- a bit name is not among the declared bits.
  * ``union`` -- value matches none of the member types.

``mandatory`` / must / choice mutual-exclusion are out of scope (those are
schema-level, not leaf-value-level); this module only checks single leaf
values against their type.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

# pyang's TypeSpec subclasses. Importing pyang here is fine -- pyang is a
# hard dependency of this package, and the loader already imports it.
from pyang import types as pt

if TYPE_CHECKING:
    # SchemaNode / Loader are only used in type annotations; importing them
    # at runtime would create a cycle (schema -> loader -> ... and back via
    # xml_builder -> validator). Keep it TYPE_CHECKING-only.
    from .loader import Loader
    from .schema import SchemaNode


class YangValidationWarning(UserWarning):
    """A leaf value violated a YANG type constraint (non-blocking)."""


def validate_value(node: "SchemaNode", value: Any, loader: "Loader") -> list[str]:
    """Return warning strings for ``value`` against ``node``'s YANG type.

    Empty list means the value is valid. Never raises -- any unexpected
    pyang behaviour is swallowed (returning no warning) so a validator hiccup
    can never block generation or parsing.

    Skipped cases (return ``[]``):

      * ``node`` is not a leaf (container/list/rpc have no value type).
      * ``node.type`` is ``None`` (no type info available).
      * ``node.type.type_spec`` is ``None`` (type failed to resolve in pyang,
        or the TypeInfo was synthesised without a pyang statement).
      * ``value`` is a delete/remove sentinel dict (``{"_operation": ...}``);
        a deleted leaf carries no value to validate.
    """
    t = node.type
    if t is None or t.type_spec is None:
        return []
    if not node.is_leaf:
        return []
    # A delete/remove sentinel on a leaf means "no value"; the forward
    # builder emits an empty element with nc:operation="delete". There is
    # nothing to validate.
    if isinstance(value, dict):
        return []

    spec = t.type_spec
    try:
        return _check(spec, value, node, loader)
    except Exception:
        # The validator must never break generation/parsing. If pyang raises
        # on an edge case we don't handle, swallow it -- the device will do
        # its own validation on edit-config anyway.
        return []


# ----------------------------------------------------------------------
# TypeSpec dispatch
#
# Each handler returns a list of warning strings (empty = valid). We probe
# pyang with throwaway ``[]`` error lists: str_to_val returns None on a
# parse/precision/resolve failure, and validate returns False on a range/
# length/pattern/enum/bits violation.


def _check(spec, value: Any, node: "SchemaNode", loader: "Loader") -> list[str]:
    """Dispatch on the concrete TypeSpec subclass.

    Restriction specs (Range/Length/Pattern/Enum/Bit) carry a ``.base``
    pointing at the underlying primitive spec; pyang's own ``validate``
    methods chain to ``self.base.validate`` first, so calling the outermost
    spec's ``validate`` covers the whole typedef chain. We therefore don't
    walk ``.base`` manually -- we just call the outermost spec.
    """
    # Union first: a value is valid if *any* member type accepts it.
    if isinstance(spec, pt.UnionTypeSpec):
        return _check_union(spec, value, node, loader)

    # identityref is special: str_to_val does both resolution and the
    # derivation check (it calls is_derived_from internally), returning the
    # identity statement on success or None on failure. It needs the leaf's
    # module to resolve prefixes.
    if isinstance(spec, pt.IdentityrefTypeSpec):
        return _check_identityref(spec, value, node, loader)

    # bits: a space-separated (YANG) or comma-separated (our JSON shorthand)
    # list of bit names. pyang's str_to_val splits on whitespace only; we
    # normalise commas to spaces first, then validate.
    if isinstance(spec, pt.BitTypeSpec):
        return _check_bits(spec, value)

    # Pattern/range/length/enum and the primitive specs all follow the same
    # str_to_val-then-validate recipe. Pattern/Range/Length/Enum are
    # restriction specs whose validate() chains to their base, so this one
    # branch covers them and the underlying primitive in one call.
    return _check_via_str_to_val(spec, value, node, loader)


def _check_union(spec, value: Any, node: "SchemaNode", loader: "Loader") -> list[str]:
    """A union is valid if any member type accepts the value."""
    s = _to_str(value)
    for member_type in spec.types:
        member_spec = getattr(member_type, "i_type_spec", None)
        if member_spec is None:
            continue
        if _member_accepts(member_spec, s, node, loader):
            return []
    return [f"value {s!r} matches none of the union member types"]


def _member_accepts(spec, s: str, node: "SchemaNode", loader: "Loader") -> bool:
    """True if ``spec`` accepts string ``s`` (str_to_val + validate both OK).

    Recurses into nested unions (a union member may itself be a union).
    """
    if isinstance(spec, pt.UnionTypeSpec):
        for member_type in spec.types:
            member_spec = getattr(member_type, "i_type_spec", None)
            if member_spec is not None and _member_accepts(member_spec, s, node, loader):
                return True
        return False
    if isinstance(spec, pt.IdentityrefTypeSpec):
        # identityref resolution goes through loader.identities, not pyang's
        # str_to_val (see _check_identityref for why).
        return not _check_identityref(spec, s, node, loader)
    if isinstance(spec, pt.BitTypeSpec):
        # bits inside a union: validate after normalising commas.
        return not _check_bits(spec, s)
    mod = _module(node, loader)
    try:
        v = spec.str_to_val([], None, s, mod)
    except Exception:
        return False
    if v is None:
        return False
    try:
        return bool(spec.validate([], None, v, mod))
    except Exception:
        return False


def _check_identityref(spec, value: Any, node: "SchemaNode", loader: "Loader") -> list[str]:
    """identityref: resolve the value identity and check derivation from base.

    We resolve manually rather than via pyang's ``str_to_val`` because
    ``str_to_val`` requires the value's prefix to be importable from the
    leaf's *defining* module -- but in practice the value's prefix names the
    *identity's* defining module, which the leaf's module need not import
    (e.g. ``ietf-interfaces`` does not import ``iana-if-type``, yet
    ``ianaift:ethernetCsmacd`` is a perfectly valid value for an
    ``interface-type`` leaf). Resolving through :attr:`Loader.identities`
    (a bare-name -> module index over all loaded modules) avoids that
    coupling.

    Accepts both bare (``ethernetCsmacd``) and prefixed
    (``ianaift:ethernetCsmacd``) forms; the prefix is informational only --
    resolution is by bare name across all loaded modules, matching how the
    forward builder resolves identityref values.

    Uses :func:`pyang.types.is_derived_from_or_self` so the base identity
    itself also counts as valid (an identityref may carry its own base).
    """
    s = _to_str(value)
    # Strip any ``prefix:`` -- resolution is by bare name (the forward
    # builder does the same when emitting).
    if ":" in s:
        _prefix, ident = s.split(":", 1)
    else:
        ident = s
    def_mod_name = loader.identities.get(ident)
    if def_mod_name is None:
        return [f"identityref {s!r}: identity {ident!r} not found in any "
                f"loaded module"]
    try:
        def_mod = loader.get_module(def_mod_name)
        val_identity = def_mod.i_identities[ident]
    except (KeyError, AttributeError):
        return [f"identityref {s!r}: identity {ident!r} could not be resolved"]
    # A leaf's identityref may list multiple bases; the value must be derived
    # from (or equal to) at least one of them.
    for base_stmt in spec.idbases:
        base_identity = getattr(base_stmt, "i_identity", None)
        if base_identity is None:
            continue
        if pt.is_derived_from_or_self(val_identity, base_identity, []):
            return []
    base_names = [getattr(getattr(b, "i_identity", None), "arg", "?")
                  for b in spec.idbases]
    return [f"identityref {s!r} is not derived from base(s) {base_names!r}"]


def _check_bits(spec, value: Any) -> list[str]:
    """bits: each name in the value must be a declared bit.

    YANG serialises bits as a space-separated string; our JSON input also
    accepts commas (a common shorthand). Normalise to a name list, then ask
    pyang's validate to check each against ``spec.bits``.
    """
    s = _to_str(value)
    # Accept both "a b c" (YANG wire form) and "a,b,c" (JSON shorthand).
    names = [n for n in s.replace(",", " ").split() if n]
    if not names:
        return []
    valid_names = {name for name, _pos in spec.bits}
    bad = [n for n in names if n not in valid_names]
    if bad:
        return [f"bits {bad!r} not defined; valid bits: {sorted(valid_names)}"]
    return []


def _check_via_str_to_val(spec, value: Any, node: "SchemaNode", loader: "Loader") -> list[str]:
    """Generic path: str_to_val (parse + precision) then validate (range/
    length/pattern/enum). Returns a warning describing which constraint
    failed, or ``[]`` when the value is fully valid.

    For types with no restrictions (plain string, boolean, empty, binary,
    integers without range) str_to_val returns a non-None value and
    validate returns True, so we naturally return ``[]``.
    """
    mod = _module(node, loader)
    s = _to_str(value)
    # str_to_val: parses the string into the type's value object. Returns
    # None on syntax/precision/resolve failure (decimal64 too many fraction
    # digits, integer out of lexical space, etc.).
    try:
        v = spec.str_to_val([], None, s, mod)
    except Exception as e:
        return [f"value {s!r}: {e}"]
    if v is None:
        return [f"value {s!r} is not a valid {node.type.name!r}"]
    # validate: checks range/length/pattern/enum against the parsed value.
    # Restriction specs chain to their base.validate, so one call covers the
    # whole typedef chain.
    try:
        ok = spec.validate([], None, v, mod)
    except Exception as e:
        return [f"value {s!r}: {e}"]
    if not ok:
        return [f"value {s!r} violates a {node.type.name!r} constraint "
                f"(range/length/pattern/enumeration)"]
    return []


# ----------------------------------------------------------------------
# helpers


def _module(node: "SchemaNode", loader: "Loader"):
    """The pyang module object defining ``node`` (for prefix resolution).

    pyang's str_to_val needs the leaf's defining module to resolve prefixed
    identityref values and to pick up module-level type tables. Returns None
    if the module isn't loaded (the caller's str_to_val will then return
    None, which surfaces as a warning -- the right outcome for an
    unresolvable value).
    """
    try:
        return loader.get_module(node.module_name)
    except KeyError:
        return None


def _to_str(value: Any) -> str:
    """Normalise a JSON leaf value to the string form pyang expects.

    Mirrors :func:`xml_builder._to_str`: bool -> "true"/"false", everything
    else -> ``str()``. pyang's str_to_val parses from the string form.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def emit_warnings(node: "SchemaNode", value: Any, loader: "Loader") -> None:
    """Validate ``value`` and ``warnings.warn`` each problem (convenience).

    Used by the XML builder and reverse parser at the single chokepoint
    where a leaf value is formatted/coerced, so both directions get the same
    validation without duplicating the dispatch.
    """
    for msg in validate_value(node, value, loader):
        warnings.warn(
            f"leaf {node.name!r}: {msg}",
            YangValidationWarning,
            stacklevel=2,
        )
