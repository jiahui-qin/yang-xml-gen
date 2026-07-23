"""Long-lived JSON-RPC worker over stdin/stdout.

This is the integration seam between :mod:`yang_xml_gen` (Python) and an
external process that wants to drive it as a "plugin" -- in practice the
netconfSub Node.js backend. The Node side spawns this module once at
startup, holds the process for the plugin's lifetime, and sends one JSON
request per line on stdin; we answer with one JSON response per line on
stdout.

Why a long-lived worker rather than spawning the CLI per call?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``Loader()`` parses and cross-validates *every* ``.yang`` file in the
models directory (124 files here) -- a one-time cost of hundreds of
milliseconds to a few seconds, dominated by pyang's ``ctx.validate()``.
After that, individual operations (``build``, ``parse_reply``,
``template_to_json``) only walk one module's ``i_children`` and are
cheap. A per-call CLI invocation would re-pay the full load cost on every
single request; a warm worker amortises it across the whole session.

Wire protocol
~~~~~~~~~~~~~

* On startup we construct a single ``Loader(models_dir)`` and emit one
  greeting line: ``{"ready": true, "models_dir": ..., "module_count": N}``
  (or ``{"ready": false, "error": ...}`` if the models dir is unusable).
* Each request is a line: ``{"id": <opaque>, "method": "...", "params": {...}}``.
* Each response is a line: ``{"id": <same>, "ok": true, "result": ...,
  "warnings": [...]}`` or ``{"id": <same>, "ok": false, "error":
  {"type": "...", "message": "..."}}``.
* ``{"method": "shutdown"}`` (no id required) exits cleanly. EOF on stdin
  does too.

``warnings`` captures :class:`YangValidationWarning` records emitted
during the call (YANG type-constraint violations are non-blocking, so a
successful ``result`` can still carry warnings -- the device is the final
authority). We use ``warnings.catch_warnings(record=True)`` so the
warnings reach the caller as structured data instead of leaking to
stderr.

Errors are framed, never tracebacks: every handler runs inside
``try/except``, so ``BuildError`` / ``ParseError`` / ``KeyError`` /
``ValueError`` / ``RuntimeError`` become ``{"ok": false, "error": {...}}``
responses. The worker never crashes on a bad request.
"""

from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from typing import Any, Callable

from .loader import Loader, MODELS_DIR_ENV
from .schema import build_schema
from .scaffold import generate_template
from .validator import YangValidationWarning
from .wrappers import (
    bare_config,
    edit_config,
    get,
    get_config,
    rpc_call,
    subtree_filter,
    xpath_filter,
)
from .xml_builder import BuildError
from .xml_parser import ParseError, parse_fragment, parse_reply

# Methods that take a ``params`` mapping. Kept explicit (rather than reflected
# off function names) so the wire contract is grep-able in one place.
_METHODS: dict[str, Callable[[Loader, dict], Any]]


def _make_loader(models_dir: str | None) -> Loader:
    """Construct the single shared ``Loader``.

    Resolution priority matches the library: explicit arg →
    ``YANG_XML_GEN_MODELS_DIR`` → bundled ``models/``. We re-read the env
    here (rather than passing ``models_dir`` straight through) so a caller
    that sets the env but sends no init param still works.
    """
    resolved = models_dir or os.environ.get(MODELS_DIR_ENV) or None
    return Loader(resolved)


# -- RPC method handlers -------------------------------------------------
#
# Each handler takes the shared ``loader`` and the ``params`` dict, and
# returns the ``result`` payload (the worker wraps it with ok/warnings).
# They are thin adapters over the public library functions -- no new
# generation logic lives here.


def _h_list_modules(loader: Loader, _params: dict) -> list[str]:
    return loader.list_modules()


def _h_roots(loader: Loader, params: dict) -> list[dict]:
    module = params["module"]
    tree = build_schema(loader, module)
    # ``tree`` is the synthetic module root; its children are the top-level
    # data nodes and rpcs. We surface name+kind so the caller can render a
    # picker and auto-detect rpc roots (kind == "rpc").
    return [
        {"name": name, "kind": child.kind}
        for name, child in tree.children.items()
    ]


def _h_template(loader: Loader, params: dict) -> dict:
    return generate_template(
        loader,
        params["module"],
        params["root"],
        include_state=bool(params.get("include_state", False)),
    )


def _h_build(loader: Loader, params: dict) -> dict:
    """Build XML for any ``wrap`` form.

    The wrap dispatch mirrors ``cli._build_get_message``: subtree filters
    reuse the XML builder via :func:`subtree_filter`, xpath filters via
    :func:`xpath_filter`, and ``get``/``get-config`` take a pre-built
    filter element (or ``None`` for full retrieval). The data-bearing wraps
    (``bare``/``edit-config``/``rpc``) go straight to the wrapper funcs.
    """
    wrap = params.get("wrap", "bare")
    module = params.get("module")
    root = params.get("root")
    data = params.get("data")

    if wrap == "bare":
        return {"xml": bare_config(loader, module, root, data,
                                   operation=params.get("operation"))}
    if wrap == "edit-config":
        return {"xml": edit_config(
            loader, module, root, data,
            target=params.get("target", "running"),
            operation=params.get("operation"),
            message_id=int(params.get("message_id", 101)),
        )}
    if wrap == "rpc":
        # rpc_call's third positional is the rpc *name*, not a data root.
        return {"xml": rpc_call(
            loader, module, root, data,
            message_id=int(params.get("message_id", 101)),
        )}

    # Read path: get-config / get. Neither takes a data tree -- they take a
    # filter (subtree or xpath) or nothing for full retrieval.
    filter_data = params.get("filter")
    filter_select = params.get("filter_select")
    if filter_data is not None and filter_select is not None:
        raise ValueError(
            "spec may set `filter` (subtree) or `filter_select` (xpath), "
            "not both"
        )

    filter_element = None
    if filter_select is not None:
        filter_element = xpath_filter(str(filter_select))
    elif filter_data is not None:
        if not module or not root:
            raise ValueError(
                "a subtree `filter` requires `module` and `root`"
            )
        filter_element = subtree_filter(loader, module, root, filter_data)

    with_defaults = params.get("with_defaults")
    message_id = int(params.get("message_id", 102 if wrap == "get-config" else 103))

    if wrap == "get-config":
        return {"xml": get_config(
            target=params.get("target", "running"),
            filter_element=filter_element,
            with_defaults=with_defaults,
            message_id=message_id,
        )}
    if wrap == "get":
        return {"xml": get(
            filter_element=filter_element,
            with_defaults=with_defaults,
            message_id=message_id,
        )}

    raise ValueError(
        f"unknown wrap {wrap!r}; expected one of "
        "bare/edit-config/rpc/get-config/get"
    )


def _h_parse_reply(loader: Loader, params: dict) -> Any:
    return parse_reply(
        params["xml"], loader,
        data_only=bool(params.get("data_only", False)),
    )


def _h_parse_fragment(loader: Loader, params: dict) -> Any:
    return parse_fragment(
        params["xml"], loader,
        module=params.get("module"),
        root=params.get("root"),
        data_only=bool(params.get("data_only", True)),
    )


def _h_validate(loader: Loader, params: dict) -> dict:
    """Run a build purely to collect YANG type-constraint warnings.

    We don't return the generated XML -- the caller only asked "is this
    data valid?". The build itself emits ``YangValidationWarning`` via
    ``warnings.warn`` for each leaf-value violation; those are captured by
    the worker's warning trap and returned alongside an empty result.
    """
    _h_build(loader, params)
    return {}


_METHODS = {
    "list_modules": _h_list_modules,
    "roots": _h_roots,
    "template": _h_template,
    "build": _h_build,
    "parse_reply": _h_parse_reply,
    "parse_fragment": _h_parse_fragment,
    "validate": _h_validate,
}


# -- main loop -----------------------------------------------------------


def _emit(obj: dict) -> None:
    """Write one JSON object as a line on stdout and flush.

    stdout is our protocol channel, so every write must be flushed
    immediately -- the Node side reads line-buffered and would block
    forever on a partial line sitting in our buffer.
    """
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_request(loader: Loader, req: dict) -> dict | None:
    """Dispatch one request dict to its handler, returning the response.

    Returns ``None`` for ``shutdown`` (a signal to stop the loop rather
    than a request to answer). Every other path produces a response dict;
    exceptions never escape -- they're framed as ``ok: false``.
    """
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "shutdown":
        return None

    handler = _METHODS.get(method)
    if handler is None:
        return {
            "id": req_id,
            "ok": False,
            "error": {
                "type": "MethodError",
                "message": f"unknown method {method!r}; "
                           f"expected one of {sorted(_METHODS)}",
            },
        }

    # Trap YangValidationWarning (non-blocking) into a structured list.
    # ``simplefilter("always")`` ensures our handler fires even if Python's
    # default "once per location" filter would have suppressed repeats.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            result = handler(loader, params)
        except (BuildError, ParseError, KeyError, ValueError,
                RuntimeError, Exception) as exc:
            return {
                "id": req_id,
                "ok": False,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            }
        warn_list = [
            {"message": str(w.message)}
            for w in caught
            if issubclass(w.category, YangValidationWarning)
        ]

    return {"id": req_id, "ok": True, "result": result, "warnings": warn_list}


def _run(models_dir: str | None = None) -> int:
    """Construct the loader, greet, then serve requests until EOF/shutdown."""
    try:
        loader = _make_loader(models_dir)
    except Exception as exc:
        # Loader failure (typically: models dir missing after a pip install).
        # Tell the caller we're not usable; they can surface a 503 with the
        # installation hint. Exit non-zero so the Node side's exit handler
        # also sees a failure.
        _emit({
            "ready": False,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })
        return 1

    _emit({
        "ready": True,
        "models_dir": str(loader.models_dir),
        "module_count": len(loader.list_modules()),
    })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            # Malformed JSON on the wire: we have no id to echo back, so
            # answer with a null id so the caller can still correlate.
            _emit({
                "id": None,
                "ok": False,
                "error": {"type": "JSONDecodeError", "message": str(exc)},
            })
            continue

        resp = _handle_request(loader, req)
        if resp is None:
            # ``shutdown`` -- stop serving.
            break
        _emit(resp)

    return 0


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point (``yang-xml-gen-rpc``).

    An optional ``--models-dir`` is accepted for parity with the CLI; the
    ``YANG_XML_GEN_MODELS_DIR`` env var is the more common way for the
    Node plugin to configure us (set on the spawned env, no args needed).
    """
    models_dir: str | None = None
    args = list(sys.argv[1:] if argv is None else argv)
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--models-dir",):
            if i + 1 < len(args):
                models_dir = args[i + 1]
                i += 2
                continue
        elif a.startswith("--models-dir="):
            models_dir = a.split("=", 1)[1]
            i += 1
            continue
        i += 1

    return _run(models_dir)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
