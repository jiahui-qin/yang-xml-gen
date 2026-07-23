# yang-xml-gen

**YANG-driven NETCONF XML generator and reverse parser.**

[English](README.md) | [中文](README_zh.md)

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)
![pyang](https://img.shields.io/badge/depends%20on-pyang-orange.svg)

`yang-xml-gen` turns a small YAML/JSON spec into NETCONF XML you can send to a
device, and parses `<rpc-reply>` XML back into JSON — schema-driven against your
own YANG models. You describe *what* data you want; the tool resolves namespaces,
identityref prefixes, key ordering, and `nc:operation` for you.

**Highlights**

- **Forward**: spec → `<edit-config>` / `<rpc>` / `<get-config>` / `<get>` XML,
  or a bare `<config>` fragment. Namespace, identityref prefix, and list-key
  order are derived from YANG.
- **Reverse**: `<rpc-reply>` XML (or a bare data fragment) → JSON spec-data that
  round-trips back through `build()`.
- **Scaffold**: `--template` emits a blank JSON skeleton from the schema, so you
  never hand-write a spec from scratch.
- **Validation**: leaf values are checked against YANG type constraints
  (`range`/`length`/`pattern`/`enumeration`/`identityref`/`bits`/`union`/
  `decimal64`) and emitted as **non-blocking** `YangValidationWarning`s — the
  device stays the final authority.
- **Packaged**: `pip install` gives you the `yang-xml-gen` command; `py.typed`
  ships for type checkers.

## Table of contents

- [Installation](#installation)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [Spec file format](#spec-file-format)
- [Library API](#library-api)
- [RPC worker (embedded mode)](#rpc-worker-embedded-mode)
- [Value validation](#value-validation)
- [Reverse parsing](#reverse-parsing)
- [Read path: get / get-config](#read-path-get--get-config)
- [Packaging](#packaging)
- [Testing](#testing)
- [Limitations](#limitations)
- [Project structure](#project-structure)
- [License](#license)

## Installation

Requires **Python 3.10+** and **pyang**. Two ways to get going:

### Option A — `pip install` (recommended)

```bash
# From the repo root (editable, so code edits take effect immediately)
python -m pip install -e .

# You now have the yang-xml-gen command
yang-xml-gen --list-modules --models-dir models
```

### Option B — `PYTHONPATH` (no install, for development)

```bash
python -m pip install pyang pyyaml
export PYTHONPATH=src          # Linux / macOS / Git Bash
# Windows PowerShell: $env:PYTHONPATH="src"
```

### About the `models/` directory

The wheel **does not bundle** YANG models — they're large upstream artifacts.
After a non-editable `pip install`, `Loader()` cannot find them automatically
and raises `RuntimeError`. Point it at your models one of two ways:

- CLI flag: `--models-dir /path/to/models`
- Environment variable: `export YANG_XML_GEN_MODELS_DIR=/path/to/models`

**Editable installs from the repo** are an exception: `__file__` points at the
source tree, so `Loader()` still auto-discovers the repo's `models/`. Only a
real `site-packages` install triggers the "must specify explicitly" rule. See
[`models/README.md`](models/README.md) for how to obtain the standard IETF /
OpenConfig models.

The repo ships one small **demo model**, `models/example-toaster@2026-07-17.yang`,
used throughout this README — it's committed (unlike the upstream models).

## Quick start

End-to-end loop with the bundled `example-toaster` model:
**explore → template → fill → emit XML → parse a reply back**. All commands run
from the repo root.

> Using option A (`pip install`)? Replace `python -m yang_xml_gen.cli` below with
> `yang-xml-gen`. Using option B? Make sure `PYTHONPATH=src` is set first.

### 1. Explore the model

```bash
$ python -m yang_xml_gen.cli --list-modules | grep toaster
example-toaster

$ python -m yang_xml_gen.cli --roots example-toaster
container  toaster
rpc        make-toast
rpc        cancel-toast
```

### 2. Generate a blank template

```bash
python -m yang_xml_gen.cli --template example-toaster.toaster > examples/toaster-template.json
```

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "data": {
    "darkness": "",
    "toast-type": "",
    "mode": "",
    "label": ""
  }
}
```

### 3. Fill in data and emit XML

Edit the template into [`examples/toaster-config.yaml`](examples/toaster-config.yaml):

```yaml
module: example-toaster
root: toaster
operation: merge
wrap: edit-config
data:
  darkness: 7
  toast-type: wheat-bread
  mode: defrost
  label: Kitchen counter
```

Generate the NETCONF XML:

```bash
$ python -m yang_xml_gen.cli examples/toaster-config.yaml
```

```xml
<?xml version='1.0' encoding='utf-8'?>
<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0" message-id="101">
  <nc:edit-config nc:operation="merge">
    <nc:target>
      <nc:running />
    </nc:target>
    <nc:config>
      <toaster xmlns="urn:example:toaster" nc:operation="merge">
        <darkness>7</darkness>
        <toast-type xmlns:toaster="urn:example:toaster">toaster:wheat-bread</toast-type>
        <mode>defrost</mode>
        <label>Kitchen counter</label>
      </toaster>
    </nc:config>
  </nc:edit-config>
</nc:rpc>
```

Note the identityref `toast-type` automatically carries the `toaster:` prefix and
its namespace declaration — you write the bare identity name, the tool resolves
the prefix from the global identity index.

### 4. Parse a device reply back into JSON

Given [`examples/toaster-reply.xml`](examples/toaster-reply.xml) (a `<get-config>`
reply for the toaster):

```bash
$ python -m yang_xml_gen.cli examples/toaster-reply.xml --from-xml
```

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "data": {
    "darkness": "7",
    "toast-type": "toaster:wheat-bread",
    "mode": "defrost",
    "label": "Kitchen counter"
  }
}
```

The `module` and `root` are inferred from the payload's `xmlns`. The result
round-trips — feed it back to `build()` and you get the same XML.

### Full round-trip in one block

```bash
# Write: spec -> edit-config XML -> send to device
python -m yang_xml_gen.cli examples/toaster-config.yaml -o edit.xml

# Read: build a get-config request, send to device, get reply.xml
# (request generation shown in "Read path" below)

# Parse: reply.xml -> JSON (diff it, edit it, or rebuild the XML)
python -m yang_xml_gen.cli examples/toaster-reply.xml --from-xml --data-only -o data.json
```

## CLI reference

```bash
python -m yang_xml_gen.cli [spec] [options]
# or, after pip install:  yang-xml-gen [spec] [options]
# or:                     python -m yang_xml_gen [spec] [options]
```

| Flag | Argument | Description |
|---|---|---|
| `spec` (positional) | path | YAML/JSON spec file (or XML file with `--from-xml`/`--from-fragment`) |
| `-o`, `--output` | path | Write XML to this file (default: stdout) |
| `--wrap` | `bare` \| `edit-config` \| `rpc` \| `get-config` \| `get` | Output form; overrides the `wrap` key in the spec (default: `bare`) |
| `--models-dir` | path | Override the models directory |
| `--list-modules` | — | Print loaded module names and exit |
| `--roots` | `MODULE` | Print top-level data nodes of `MODULE` and exit |
| `--template` | `MODULE.ROOT` | Emit a blank JSON template for `MODULE.ROOT` and exit |
| `--include-state` | — | With `--template`: keep `config false` (state) nodes |
| `--from-xml` | — | Treat `spec` as a `<rpc-reply>` XML file; parse back into JSON |
| `--from-fragment` | — | Treat `spec` as a bare data-tree fragment; parse back into JSON |
| `--data-only` | — | With `--from-xml`/`--from-fragment`: emit only `data`, not the `{module, root, data}` envelope |

`--from-xml` and `--from-fragment` are mutually exclusive.

## Spec file format

A spec is a YAML or JSON mapping describing the root node and its content.

```yaml
module: example-toaster      # YANG module holding the root node
root: toaster                # top-level data node to build
operation: merge             # default nc:operation on the root element (optional)
wrap: edit-config            # output form (optional; default bare, or see --wrap)
message-id: 101              # rpc / get-config / get: the message-id attribute
data:                        # content of the root node
  darkness: 7
  toast-type: wheat-bread
  mode: defrost
  label: Kitchen counter
```

| Key | Required | Applies to | Meaning |
|---|---|---|---|
| `module` | yes (forward) | all | YANG module holding the root node |
| `root` | yes (forward) | all | Top-level data node (or rpc name with `wrap: rpc`) |
| `data` | yes (forward) | edit-config / rpc / bare | Content of the root node |
| `wrap` | optional | all | `bare` \| `edit-config` \| `rpc` \| `get-config` \| `get` |
| `operation` | optional | edit-config / bare | Default `nc:operation` on the root element |
| `message-id` | optional | rpc / get-config / get | The `message-id` attribute |
| `target` | optional | get-config | Datastore to read (default `running`) |
| `filter` | optional | get-config / get | Subtree filter (spec-data shape; needs `module`+`root`) |
| `filter-select` | optional | get-config / get | XPath filter (string; needs neither `module` nor `root`) |
| `with-defaults` | optional | get-config / get | RFC 6243 mode: `report-all` / `report-all-tagged` / `trim` / `explicit` |

For `wrap: get-config` / `get`, the `data` key is **not** used — `filter`
(subtree) or `filter-select` (xpath) selects what to retrieve; omit both for a
full retrieval. See [Read path](#read-path-get--get-config).

### Deleting nodes (`_operation` sentinel)

Express `nc:operation="delete"` (RFC 6241 §7.2) with the `_operation` key. It
works on three node kinds:

| Target | Data shape | Generated XML | Semantics |
|---|---|---|---|
| **list entry** | `{interface: [{name: eth0, _operation: delete}]}` | `<interface nc:operation="delete"><name>eth0</name></interface>` | Delete the key-matched entry; only key leaf needed |
| **container / subtree** | `{... ipv4: {_operation: delete}}` | `<ipv4 nc:operation="delete"/>` | Delete the whole subtree, no children |
| **leaf** | `{... description: {_operation: delete}}` | `<description nc:operation="delete"/>` | Delete the leaf, no text |

`delete` and `remove` serialize identically; the difference is server-side
(`delete` on a missing node errors, `remove` is idempotent). A leaf delete
sentinel must **not** carry a value — `<leaf nc:operation="delete"/>` is the
whole instruction.

## Library API

All functions live under `yang_xml_gen.*`. A `Loader` is the entry point — it
loads every `.yang` in the models directory and builds identity/namespace
indexes.

```python
from yang_xml_gen.loader import Loader
loader = Loader()                       # auto-discovers repo models/, or set models_dir=...
loader = Loader(models_dir="/path/to/models")
```

### Builder (`xml_builder.py`)

```python
from yang_xml_gen.xml_builder import build, build_fragment

build(loader, module_name, root, data, operation=None) -> ET.Element
build_fragment(loader, module_name, root, data, operation=None) -> str
```

`build` returns an `ElementTree.Element`; `build_fragment` is the pretty-printed
string convenience.

### Wrappers (`wrappers.py`)

```python
from yang_xml_gen.wrappers import (
    bare_config, edit_config, rpc_call, get, get_config,
    subtree_filter, xpath_filter,
)

bare_config(loader, module_name, root, data, *, operation=None) -> str
edit_config(loader, module_name, root, data, *,
            target="running", operation=None, message_id=101) -> str
rpc_call(loader, module_name, rpc_name, data, *, message_id=101) -> str

# NOTE: get and get_config are KEYWORD-ONLY (no loader/module/root positional).
get_config(*, target="running", filter_element=None,
           with_defaults=None, message_id=102) -> str
get(*, filter_element=None, with_defaults=None, message_id=103) -> str

subtree_filter(loader, module_name, root, data) -> ET.Element
xpath_filter(select: str) -> ET.Element
```

Note `rpc_call`'s third positional is `rpc_name`, not `root`. `with_defaults` is
validated against `("report-all", "report-all-tagged", "trim", "explicit")` and
raises `ValueError` on a bad mode.

### Parser (`xml_parser.py`)

```python
from yang_xml_gen.xml_parser import parse_reply, parse_fragment, ParseError

parse_reply(xml, loader, *, data_only=False) -> Any
parse_fragment(xml, loader, *, module=None, root=None, data_only=True) -> Any
```

> **Gotcha**: `parse_reply` defaults to `data_only=False` (returns the
> `{module, root, data}` envelope), but `parse_fragment` defaults to
> `data_only=True` (returns just `data`). They are intentionally opposite.
> `parse_fragment` raises `ParseError` if the input is actually an `<rpc-reply>`
> (use `parse_reply` instead).

### Scaffold (`scaffold.py`)

```python
from yang_xml_gen.scaffold import generate_template, template_to_json

generate_template(loader, module, root, *, include_state=False) -> dict
template_to_json(loader, module, root, *, include_state=False, indent=2) -> str
```

List-key leaves become `"<keyname>"` placeholders, other leaves become `""`,
leaf-lists become `[""]`. State (`config false`) nodes are omitted unless
`include_state=True`.

### Validator (`validator.py`)

```python
import warnings
from yang_xml_gen.validator import (
    YangValidationWarning, validate_value, emit_warnings,
)

validate_value(node, value, loader) -> list[str]   # [] = valid; never raises
emit_warnings(node, value, loader) -> None         # validate_value + warnings.warn

# Strict mode: turn violations into hard errors
warnings.filterwarnings("error", category=YangValidationWarning)
```

`YangValidationWarning` is a `UserWarning` subclass — filter on it to silence or
escalate. See [Value validation](#value-validation).

### RPC worker (`rpc_worker.py`) — embedded mode

A long-lived JSON-RPC worker over stdin/stdout, designed to be embedded as a
"plugin" by an external process (in practice the
[netconfSub](https://github.com/jiahui-qin/netconfSub) Node.js backend). The
host spawns this module **once** and keeps it alive for the whole session,
sending one JSON request per line on stdin and reading one JSON response per
line on stdout.

Why a warm worker rather than invoking the CLI per call? `Loader()` parses and
cross-validates *every* `.yang` in the models directory (124 files here) — a
one-time cost of hundreds of ms to a few seconds. A per-call CLI invocation
would re-pay that on every single request; a warm worker amortises it across
the session, so individual `build` / `parse` calls stay in the millisecond
range.

**Launching** (the host normally does this via `child_process.spawn`):

```bash
# After `pip install -e .`, or from the repo root:
python -m yang_xml_gen.rpc_worker
# Optional: --models-dir /path/to/models (else YANG_XML_GEN_MODELS_DIR env, else bundled models/)
```

**Wire protocol:**

1. On startup the worker constructs one `Loader` and emits a greeting line:
   `{"ready": true, "models_dir": ..., "module_count": N}`
   (or `{"ready": false, "error": {"type", "message"}}` if the models dir is
   unusable — the host should surface a 503 with an install hint).
2. Each request is a line: `{"id": <opaque>, "method": "...", "params": {...}}`.
3. Each response is a line:
   `{"id": <same>, "ok": true, "result": ..., "warnings": [...]}` or
   `{"id": <same>, "ok": false, "error": {"type": "...", "message": "..."}}`.
4. `{"method": "shutdown"}` (no id required) exits cleanly; EOF on stdin does
   too.

`warnings` captures `YangValidationWarning` records emitted during the call
(YANG type-constraint violations are non-blocking, so a successful `result`
can still carry warnings — the device is the final authority). Errors are
framed, never tracebacks: the worker never crashes on a bad request.

**Methods** (thin adapters over the public library functions — no generation
logic lives here):

| method | params | returns `result` |
|---|---|---|
| `list_modules` | — | `["module1", ...]` |
| `roots` | `{module}` | `[{"name","kind"}, ...]` (`kind` includes `"rpc"`) |
| `template` | `{module, root, include_state?}` | `{module, root, data}` skeleton |
| `build` | `{module, root, data, wrap, operation?, target?, message_id?, filter?, filter_select?, with_defaults?}` | `{"xml": "<rpc>...</rpc>"}` |
| `parse_reply` | `{xml, data_only?}` | parsed reply as dict |
| `parse_fragment` | `{xml, module?, root?, data_only?}` | parsed fragment as dict |
| `validate` | `{module, root, data, ...build params}` | `{}` (warnings only — runs a build to collect `YangValidationWarning`) |

`wrap` is one of `bare` / `edit-config` / `rpc` / `get-config` / `get`. For
the read path (`get-config` / `get`) set `filter` (subtree data, requires
`module`+`root`) **or** `filter_select` (xpath string), not both; omit both
for full retrieval. See [Read path: get / get-config](#read-path-get--get-config)
for the filter semantics.

A minimal end-to-end conversation from a host:

```
→ {"id":1,"method":"list_modules"}
← {"id":1,"ok":true,"result":["example-toaster", ...],"warnings":[]}
→ {"id":2,"method":"roots","params":{"module":"example-toaster"}}
← {"id":2,"ok":true,"result":[{"name":"toaster","kind":"container"}],"warnings":[]}
→ {"id":3,"method":"build","params":{"module":"example-toaster","root":"toaster","data":{"toaster":{"darkness":3}},"wrap":"edit-config","target":"running"}}
← {"id":3,"ok":true,"result":{"xml":"<nc:rpc ...>...</nc:rpc>"},"warnings":[]}
→ {"method":"shutdown"}
```

> **Note on prefixes:** the generated `<rpc>` uses the NETCONF base namespace
> with an `nc:` prefix (`<nc:rpc xmlns:nc="urn:ietf:params:netconf:base:1.0">`).
> Hosts that re-parse the XML to forward it to a NETCONF client should strip
> this prefix (or handle namespaced roots) before handing the operation object
> to a library that adds its own `xmlns`.

## Value validation

Every leaf value is checked against its YANG type constraints and any violation
is emitted as a `YangValidationWarning` via `warnings.warn` — **non-blocking**.
`build()` still returns an Element, `parse_*` still returns a dict. The device
remains the final authority; validation just surfaces obvious typos locally.

**Covered constraints** (one warning per violation, naming the constraint):

| Constraint | Trigger | Example (against `example-toaster`) |
|---|---|---|
| `range` | numeric / decimal64 out of range | `darkness=99` (range `1..10`) |
| `length` | string length out of bounds | `label=""` (length `1..32`) |
| `pattern` | string fails XSD regex | an IPv4 leaf with `"999.999.999.999"` |
| `enumeration` | value not in enum set | `mode="nuclear"` (only `regular`/`defrost`/`reheat`) |
| `identityref` | identity missing, or not derived from the leaf's base | `toast-type="toaster:cold-pizza"` is valid; a non-existent identity warns |
| `decimal64` | fractional digits exceed `fraction-digits` | `"12.345"` when `fraction-digits=2` |
| `bits` | bit name not in the declared set | `access-operations="create,bogus"` |
| `union` | matches no member type | a date union leaf with `"not-a-date"` |

Validation runs on **both** directions: `build()` checks at `_format_value`,
`parse_reply()`/`parse_fragment()` check at `_coerce_value` — same
`emit_warnings` helper, same behaviour.

**Example** — write a spec with out-of-range `darkness` and a bogus `mode` to
`bad.json`:

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "wrap": "bare",
  "data": { "darkness": 99, "mode": "nuclear" }
}
```

```bash
$ python -m yang_xml_gen.cli bad.json
```

```
.../xml_builder.py:307: YangValidationWarning: leaf 'darkness': value '99' violates a 'uint8' constraint (range/length/pattern/enumeration)
.../xml_builder.py:307: YangValidationWarning: leaf 'mode': value 'nuclear' violates a 'enumeration' constraint (range/length/pattern/enumeration)
<?xml version='1.0' encoding='utf-8'?>
<toaster xmlns="urn:example:toaster">
  <darkness>99</darkness>
  <mode>nuclear</mode>
</toaster>
```

Two warnings, but the XML is still produced.

**Strict mode** — escalate to a hard error (CLI: `-W
error::yang_xml_gen.validator.YangValidationWarning`):

```python
import warnings
from yang_xml_gen.validator import YangValidationWarning
warnings.filterwarnings("error", category=YangValidationWarning)
# Now an invalid leaf raises YangValidationWarning instead of warning.
```

**Skipped** (no warning, no block): non-leaf nodes, `type` of `None`,
unresolved `type_spec`, and delete/remove sentinels (no value to check). The
validator swallows any unexpected pyang exception — it can never crash the
build/parse.

**identityref nuance**: the *namespace resolution* of an identityref (which
`xmlns` to emit) is a **hard error** in `build()` — an unknown identity can't
declare a namespace, so `build()` raises `BuildError`. Only the *type-constraint*
violation (identity exists but isn't derived from the leaf's base) is the
non-blocking warning.

## Reverse parsing

Two entry points, symmetric with the forward builder:

- `--from-xml` / `parse_reply()` — full `<rpc-reply>` envelope.
- `--from-fragment` / `parse_fragment()` — a bare data-tree element (e.g. the
  `<config>` content of an `<edit-config>`, or a subtree-filter reply with the
  `<rpc-reply><data>` wrapper stripped).

```bash
# Full <rpc-reply> -> JSON envelope (module/root inferred from xmlns)
python -m yang_xml_gen.cli reply.xml --from-xml

# Bare data fragment -> data-only JSON (default for --from-fragment)
python -m yang_xml_gen.cli config.xml --from-fragment
```

### Reply shapes → JSON (`parse_reply`, RFC 6241)

`parse_reply` dispatches on the first child of `<rpc-reply>`:

1. **`<data>`** (data-bearing reply) —
   single-root payload → `{"module", "root", "data"}` (or just `data` with
   `--data-only`); multi-root payload → only `--data-only` is supported (envelope
   is a single-root model; raises `ParseError` otherwise, hinting at
   `--data-only` or a subtree filter). Empty `<data/>` → `{}` (`--data-only`) or
   `ParseError` (envelope: nothing to infer).
2. **`<ok/>`** → `{"ok": true}`.
3. **`<rpc-error>`** → `{"rpc-error": [...]}` — each error is a structured dict
   of its children (`error-type`, `error-tag`, `error-severity`, `error-message`,
   `error-path`, `error-info`). These live in the NETCONF base namespace and
   aren't YANG-modelled, so they're parsed schema-less.

Unknown reply shape → `ParseError`.

### Type recovery

Reverse type recovery is symmetric with the forward `_to_str`:

| YANG leaf type | XML text | JSON value |
|---|---|---|
| `boolean` | `true` / `1` | `true` |
| `boolean` | `false` / `0` | `false` |
| `empty` | (no text; presence is the value) | `true` |
| `identityref` | `prefix:ident` | kept verbatim as a string |
| others (string / enumeration / decimal64 / integer / …) | text | text string |

The forward builder always emits identityref **with** a prefix
(`toaster:wheat-bread`); the reverse parser preserves it, so round-trip input
must use the prefixed form for exact equality (a bare ident round-trips back
*with* a prefix).

### `nc:operation` round-trip

Forward `nc:operation` (the `_operation: delete` sentinel) is preserved
symmetrically on the way back:

- `nc:operation` on a container / list entry → `"_operation"` key in the entry.
- `nc:operation="delete"` (or `remove`) on a leaf **with no text** → the
  sentinel `{"_operation": "delete"}`.
- A leaf with both a value and an operation (a shape the forward tool never
  produces) → only the value is kept; the operation is dropped.

`get`/`get-config` replies usually carry **no** `nc:operation`, so the main use
case is unaffected; this round-trip mainly serves "parse an edit-config-style
fragment back" edge cases.

## Read path: get / get-config

`wrap: get-config` reads a datastore's config; `wrap: get` reads running config
merged with operational state. Narrow the reply with `filter` (subtree) or
`filter-select` (xpath) — omit both for a full retrieval.

Key insight: a subtree filter's "select subtree" content is **the same shape**
`build()` already produces — a key-only list entry is a "content-match node"
(selects one entry), an empty container is a "selection node" (selects the whole
subtree). So filter content reuses `build()` with no new serialization logic.

```json
{
  "module": "example-toaster",
  "root": "toaster",
  "wrap": "get-config",
  "target": "running",
  "message-id": 201,
  "filter": { "toaster": {} }
}
```

### Filter semantics (RFC 6241 §6.2/§6.4)

| Filter form | Spec key | Selection semantics |
|---|---|---|
| subtree, key-only entry | `filter: {interface: [{name: eth0}]}` | content-match: select entries where `name=="eth0"` |
| subtree, empty container | `filter: {interface: [{name: eth0, ipv4: {}}]}` | selection: select the whole `ipv4` subtree under eth0 |
| subtree, multiple entries | `filter: {interface: [{name: eth0}, {name: eth1}]}` | multiple content-match nodes, one per entry |
| xpath | `filter-select: "/if:interfaces/..."` | XPath 1.0 expression, evaluated by the device |
| none | (omit both) | full retrieval |

### `<get>` vs `<get-config>`

- `<get-config>` (§7.5): reads a named datastore (`<target>`); config data only.
  Use the `target` key (default `running`).
- `<get>` (§7.7): no `<target>`; returns running config merged with operational
  state. Use this for operational state.

### `<with-defaults>` (RFC 6243)

By default NETCONF does not echo a node's schema default value (only explicitly
set nodes). RFC 6243's `<with-defaults>` parameter controls this; it's augmented
into `<get>`/`<get-config>` input by `ietf-netconf-with-defaults`, so the element
lives in *that* module's namespace (not the NETCONF base namespace). Set the
`with-defaults` spec key to one of:

| Mode | Semantics (RFC 6243 §3) |
|---|---|
| `report-all` | Echo every node, including unset defaults |
| `report-all-tagged` | Same, but default-valued nodes are tagged |
| `trim` | Don't echo nodes whose value equals the schema default |
| `explicit` | Only echo explicitly set nodes (the NETCONF default, made explicit) |

`<with-defaults>` is emitted as the **last** child of `<get>`/`<get-config>`
(after `<target>` and `<filter>`), matching its augment position. An invalid mode
raises `ValueError` in the wrapper — no XML is produced.

## Packaging

[`pyproject.toml`](pyproject.toml) uses the setuptools backend.

```bash
python -m pip install -e .     # editable (development)
python -m pip install .        # regular wheel install
```

After install, the `yang-xml-gen` console script (`[project.scripts]` →
`yang_xml_gen.cli:main`) is equivalent to `python -m yang_xml_gen.cli`.

| Field | Value |
|---|---|
| Project name | `yang-xml-gen` |
| Version | `0.7.0` |
| `requires-python` | `>=3.10` (uses PEP 604 `X \| Y` union syntax) |
| Dependencies | `pyang>=2.5`, `PyYAML>=6.0` |
| Entry point | `yang-xml-gen = "yang_xml_gen.cli:main"` |
| Type marker | `py.typed` (PEP 561) ships with the wheel |
| License | MIT, declared via `project.license = {file = "LICENSE"}` |

**Models are not bundled** — 123 `.yang` files are large upstream artifacts
(obtain them per [`models/README.md`](models/README.md)). The cost is the
post-install `RuntimeError` until you pass `--models-dir` or
`YANG_XML_GEN_MODELS_DIR`; this makes "missing models" a loud failure rather than
a silent empty run. Editable installs from the repo auto-discover `models/`.

## Testing

```bash
python -m pytest -q     # 172 tests
```

| Test module | Covers |
|---|---|
| `tests/test_generator.py` | Forward build: namespace, key order, identityref prefix, boolean, operation injection, error handling |
| `tests/test_delete.py` | `_operation: delete` on list entry / container / leaf; multi-entry; `delete` vs `remove` |
| `tests/test_scaffold.py` | `--template` skeleton: list placeholder, state filtering, leaf-list, empty/decimal64/union leaves |
| `tests/test_choice_rpc.py` | choice/case flattening, rpc input serialization, augment namespace |
| `tests/test_filter.py` | get / get-config, subtree + xpath filters, `<with-defaults>`, CLI end-to-end |
| `tests/test_parse.py` | `parse_reply` / `parse_fragment`, reply shapes, type recovery, `nc:operation` round-trip, CLI `--from-xml`/`--from-fragment`, input BOM/UTF-16 decoding |
| `tests/test_validator.py` | `validate_value` per constraint, `emit_warnings`, strict mode, builder/parser integration |
| `tests/test_packaging.py` | `pyproject.toml` fields, file presence, models not bundled, `cli.main` callable |

## Limitations

Out of scope (by design):

- **No schema-level validation**: `when` / `must` / `mandatory` / `min-elements`
  / choice mutual-exclusion are not checked. Validation only covers a single
  leaf's type constraints.
- **Validation never blocks**: use `filterwarnings("error",
  category=YangValidationWarning)` to escalate.
- **No `action` or `notification`** support (actions nest under containers/lists
  and need a target path; notifications are device-to-manager).
- **No rpc output** generation (only the rpc *call* / input direction).
- **Models not bundled** in the wheel; no `fetch_models.py` or CI to fetch them.
- **typedef chains** are not recursively resolved to the root type.
- **leaf-list single-value delete** (`<leaf nc:operation="delete">value</leaf>`)
  is unsupported — it conflicts with the leaf's value-less delete form. Delete
  the whole leaf-list via the parent container's `_operation: delete`.
- **Structured `anyxml`** is treated as a text leaf, not parsed as an XML subtree.

## Project structure

```
yang-xml-gen/
├── models/                 # YANG models (not bundled in wheel; see models/README.md)
│   ├── README.md           #   how to obtain the 123 upstream IETF/OpenConfig models
│   └── example-toaster@2026-07-17.yang   # committed demo model used in this README
├── scripts/
│   └── compile_models.py   # batch compile + consistency check (exit code for CI)
├── src/yang_xml_gen/
│   ├── loader.py           # load all models; identity/namespace indexes; models-dir resolution
│   ├── schema.py           # pyang statements -> SchemaNode tree (TypeInfo with type_spec)
│   ├── scaffold.py         # blank JSON template from schema
│   ├── xml_builder.py      # data + schema -> XML (forward; non-blocking validation)
│   ├── xml_parser.py       # <rpc-reply> / bare fragment -> JSON (reverse; non-blocking validation)
│   ├── validator.py        # YANG type-constraint checks (range/length/pattern/enum/...)
│   ├── wrappers.py         # bare / edit-config / rpc / get-config / get envelopes
│   ├── cli.py              # CLI entry point (--from-xml / --from-fragment / --template / ...)
│   ├── __main__.py         # enables `python -m yang_xml_gen`
│   └── py.typed            # PEP 561 type marker
├── tests/                  # test_generator / delete / scaffold / choice_rpc / filter / parse / validator / packaging
├── examples/               # YAML/JSON inputs + template/filled/rpc/get/delete + reply samples
├── pyproject.toml          # packaging (setuptools, entry point: yang-xml-gen)
├── LICENSE                 # MIT
├── README.md               # this file (English)
└── README_zh.md            # Chinese
```

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 qinjh.
