"""Command-line entry point: turn a YAML spec into NETCONF XML.

Input YAML shape::

    module: ietf-interfaces      # YANG module holding the root node
    root: interfaces             # top-level data node to build
    data:                        # content of that node
      interface:
        - name: eth0
          type: ethernetCsmacd
          description: uplink
          enabled: true

Optional keys:

    operation: merge             # default operation on the root element
    wrap: bare | edit-config | rpc | get-config | get   # output form (default: bare)
    message-id: 101              # rpc/get-config/get: the message-id attribute
    target: running              # wrap=get-config: datastore to read (default running)
    filter: { ... }              # wrap=get-config/get: subtree filter (spec-data
                                 #   shape; needs module+root). Omit for full retrieval.
    filter-select: "/xpath/..."  # wrap=get-config/get: xpath filter (a string;
                                 #   needs neither module nor root). Omit for full.
    with-defaults: report-all    # wrap=get-config/get: <with-defaults> parameter
                                 #   (RFC 6243); one of report-all / report-all-tagged
                                 #   / trim / explicit. Omit to skip the parameter.

For wrap=get-config / get, the ``data`` key is not used -- ``filter`` (subtree)
or ``filter-select`` (xpath) selects what to retrieve; omit both for a full
retrieval. ``with-defaults`` may be combined with either filter form or used
on its own.

Scaffolding:

    --template MODULE.ROOT       # emit a blank JSON template instead of XML
    --include-state              # with --template: also include state nodes

Reverse (rpc-reply XML -> JSON):

    --from-xml                   # treat `spec` as a <rpc-reply> XML file and
                                 #   parse it back into JSON spec-data
    --data-only                  # with --from-xml: emit only the `data`
                                 #   object, not the {module, root, data} envelope

Examples::

    python -m yang_xml_gen.cli spec.yaml
    python -m yang_xml_gen.cli spec.yaml --wrap edit-config --output out.xml
    python -m yang_xml_gen.cli --list-modules
    python -m yang_xml_gen.cli --roots ietf-interfaces
    python -m yang_xml_gen.cli --template ietf-interfaces.interfaces > ifcfg.json
    python -m yang_xml_gen.cli --template ietf-interfaces.interfaces --include-state > ifread.json
    python -m yang_xml_gen.cli get.yaml --wrap get-config     # subtree filter retrieval
    python -m yang_xml_gen.cli reply.xml --from-xml           # reply -> {module, root, data}
    python -m yang_xml_gen.cli reply.xml --from-xml --data-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .loader import Loader
from .scaffold import template_to_json
from .schema import build_schema
from .wrappers import (
    bare_config,
    edit_config,
    get as get_message,
    get_config as get_config_message,
    rpc_call,
    subtree_filter,
    xpath_filter,
)
from .xml_parser import ParseError, parse_fragment, parse_reply


def _read_text(path: Path) -> str:
    """Read a user-supplied text file, tolerating UTF-8, UTF-8-BOM, and UTF-16.

    NETCONF/YAML/JSON inputs are nominally UTF-8, but Windows tools (Notepad,
    PowerShell redirection) often save UTF-16 or UTF-8-with-BOM. We sniff the
    BOM to pick the right codec instead of hard-coding utf-8 and crashing on a
    stray 0xff byte. Output files we write ourselves stay UTF-8 (see the
    ``write_text`` calls below).
    """
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        # UTF-16 LE/BE BOM: utf-16 detects endianness and strips the BOM.
        return data.decode("utf-16")
    # utf-8-sig accepts both plain UTF-8 and UTF-8-with-BOM (strips EF BB BF).
    return data.decode("utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="yang-xml-gen",
        description="Generate NETCONF XML from a YAML spec against YANG models.",
    )
    p.add_argument("spec", nargs="?", type=Path, help="YAML input file")
    p.add_argument("-o", "--output", type=Path, help="write XML to this file (default: stdout)")
    p.add_argument(
        "--wrap",
        choices=["bare", "edit-config", "rpc", "get-config", "get"],
        default=None,
        help="output form; overrides the `wrap` key in the spec (default: bare)",
    )
    p.add_argument("--models-dir", type=Path, help="override the models directory")
    p.add_argument("--list-modules", action="store_true", help="print loaded module names and exit")
    p.add_argument("--roots", metavar="MODULE", help="print top-level data nodes of MODULE and exit")
    p.add_argument(
        "--template",
        metavar="MODULE.ROOT",
        help="emit a blank JSON template for MODULE.ROOT and exit (no XML conversion)",
    )
    p.add_argument(
        "--include-state",
        action="store_true",
        help="with --template: keep config-false (state) nodes in the template",
    )
    p.add_argument(
        "--from-xml",
        action="store_true",
        help="parse `spec` as a <rpc-reply> XML file back into JSON spec-data",
    )
    p.add_argument(
        "--from-fragment",
        action="store_true",
        help="parse `spec` as a bare data-tree fragment (no <rpc-reply> "
             "envelope) back into JSON spec-data",
    )
    p.add_argument(
        "--data-only",
        action="store_true",
        help="with --from-xml/--from-fragment: emit only the `data` object, "
             "not the {module, root, data} envelope",
    )
    args = p.parse_args(argv)

    if args.from_xml and args.from_fragment:
        p.error("--from-xml and --from-fragment are mutually exclusive")

    loader = Loader(args.models_dir)

    if args.list_modules:
        for name in loader.list_modules():
            print(name)
        return 0
    if args.roots:
        _print_roots(loader, args.roots)
        return 0
    if args.template:
        return _print_template(loader, args.template, include_state=args.include_state, output=args.output)
    if args.from_xml:
        return _parse_xml_reply(loader, args.spec, data_only=args.data_only, output=args.output)
    if args.from_fragment:
        return _parse_xml_fragment(loader, args.spec, data_only=args.data_only, output=args.output)
    if args.spec is None:
        p.error("a spec file is required (or use --list-modules / --roots / --template / --from-xml)")

    spec = yaml.safe_load(_read_text(args.spec))
    if not isinstance(spec, dict):
        p.error("spec must be a YAML mapping with module/root/data keys")

    module = spec.get("module")
    root = spec.get("root")
    data = spec.get("data")
    operation = spec.get("operation")
    wrap = args.wrap or spec.get("wrap") or "bare"

    if wrap in ("get-config", "get"):
        xml = _build_get_message(p, loader, spec, module, root, data, wrap)
    else:
        # bare / edit-config / rpc all need a concrete data tree to emit.
        if not module or not root:
            p.error("spec must include `module` and `root`")
        if data is None:
            p.error("spec must include `data`")
        if wrap == "edit-config":
            message_id = int(spec.get("message-id", 101))
            xml = edit_config(
                loader, module, root, data,
                operation=operation, message_id=message_id,
            )
        elif wrap == "rpc":
            # message-id defaults to 101 (matching edit_config); the spec may
            # override it. operation does not apply to rpc calls.
            message_id = int(spec.get("message-id", 101))
            xml = rpc_call(loader, module, root, data, message_id=message_id)
        else:
            xml = bare_config(loader, module, root, data, operation=operation)

    if args.output:
        args.output.write_text(xml, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(xml)
        if not xml.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _build_get_message(
    p: argparse.ArgumentParser,
    loader: Loader,
    spec: dict,
    module: str | None,
    root: str | None,
    data: Any,
    wrap: str,
) -> str:
    """Build a ``<get-config>`` or ``<get>`` message (RFC 6241 §7.5/§7.7).

    Neither form requires a data tree: omit ``filter``/``filter-select`` for a
    full retrieval. A subtree filter (``filter`` key, a spec-data mapping)
    needs ``module``+``root`` to name the selection subtree's root; an xpath
    filter (``filter-select`` key, a string) needs neither. ``with-defaults``
    (RFC 6243) adds a ``<with-defaults>`` parameter after the filter.
    """
    filter_data = spec.get("filter")
    filter_select = spec.get("filter-select")
    if filter_data is not None and filter_select is not None:
        p.error("spec may set `filter` (subtree) or `filter-select` (xpath), not both")

    message_id = int(spec.get("message-id", 102))
    with_defaults = spec.get("with-defaults")

    filter_element = None
    if filter_select is not None:
        filter_element = xpath_filter(str(filter_select))
    elif filter_data is not None:
        if not module or not root:
            p.error("a subtree `filter` requires `module` and `root`")
        filter_element = subtree_filter(loader, module, root, filter_data)

    if wrap == "get-config":
        target = spec.get("target", "running")
        return get_config_message(
            target=target,
            filter_element=filter_element,
            with_defaults=with_defaults,
            message_id=message_id,
        )
    return get_message(
        filter_element=filter_element,
        with_defaults=with_defaults,
        message_id=message_id,
    )


def _print_roots(loader: Loader, module: str) -> None:
    try:
        tree = build_schema(loader, module)
    except KeyError as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(2)
    for name, child in tree.children.items():
        print(f"{child.kind:10s} {name}")


def _print_template(
    loader: Loader,
    target: str,
    *,
    include_state: bool,
    output: Path | None,
) -> int:
    """Handle ``--template MODULE.ROOT``: emit a blank JSON template."""
    try:
        module, root = target.split(".", 1)
    except ValueError:
        sys.stderr.write(
            f"--template expects MODULE.ROOT, got {target!r}\n"
        )
        return 2
    try:
        text = template_to_json(
            loader, module, root, include_state=include_state
        )
    except KeyError as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    text += "\n"
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _parse_xml_reply(
    loader: Loader,
    spec_path: Path | None,
    *,
    data_only: bool,
    output: Path | None,
) -> int:
    """Handle ``--from-xml``: parse a ``<rpc-reply>`` file into JSON.

    With ``data_only`` the parsed ``data`` object is emitted directly
    (suitable for multi-root full-retrieval replies); otherwise the
    ``{module, root, data}`` envelope is emitted. ``<ok/>`` and
    ``<rpc-error>`` replies always emit their own form regardless of
    ``data_only`` (they carry no module/root).
    """
    if spec_path is None:
        sys.stderr.write("--from-xml requires an input XML file\n")
        return 2
    xml = _read_text(spec_path)
    try:
        result: Any = parse_reply(xml, loader, data_only=data_only)
    except (ParseError, KeyError) as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def _parse_xml_fragment(
    loader: Loader,
    spec_path: Path | None,
    *,
    data_only: bool,
    output: Path | None,
) -> int:
    """Handle ``--from-fragment``: parse a bare data-tree XML fragment.

    Unlike ``--from-xml`` (full ``<rpc-reply>``), the input is a single root
    element such as ``<interfaces>...</interfaces>`` -- the kind of fragment
    you get from an ``<edit-config>``'s ``<config>`` payload or a
    subtree-filter reply with the envelope stripped. Module/root are inferred
    from the root element's xmlns unless the caller supplies them (the CLI
    does not expose explicit module/root flags; inference covers the common
    case). ``--data-only`` toggles between the bare ``data`` object and the
    ``{module, root, data}`` envelope.
    """
    if spec_path is None:
        sys.stderr.write("--from-fragment requires an input XML file\n")
        return 2
    xml = _read_text(spec_path)
    try:
        result: Any = parse_fragment(xml, loader, data_only=data_only)
    except (ParseError, KeyError) as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if output:
        output.write_text(text, encoding="utf-8")
        print(f"wrote {output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
