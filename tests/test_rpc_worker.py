"""End-to-end tests for the long-lived RPC worker (:mod:`rpc_worker`).

These spawn the worker as a real subprocess (exactly how the netconfSub
Node backend consumes it) and drive it over stdin/stdout with the
newline-delimited JSON-RPC protocol. This validates the *wire contract*
on top of the library functions the other suites already cover
unit-style:

  * the ``ready`` greeting (and its absence when models can't load);
  * every method (``list_modules`` / ``roots`` / ``template`` / ``build``
    / ``parse_reply`` / ``parse_fragment`` / ``validate``) round-trips;
  * ``build`` covers all five wrap forms (bare / edit-config / rpc /
    get-config / get), including subtree + xpath filters;
  * non-blocking ``YangValidationWarning``s are captured into the
    ``warnings`` array on a successful response;
  * errors are framed (``ok: false`` + ``error.type``), never tracebacks;
  * ``shutdown`` and EOF both exit cleanly with code 0.

They depend on the real ``models/`` directory (like the other suites),
so a missing example-toaster model would surface here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
MODELS = ROOT / "models"


class _Worker:
    """A spawned worker process with a line-based request/response helper.

    Used as a context manager so the process is always cleaned up (even on
    assertion failure) -- a leaked worker would hang the test runner on
    Windows where the stdin pipe keeps it alive.
    """

    def __init__(self, *, models_dir: Path | None = None,
                 env_override: dict | None = None):
        env = os.environ.copy()
        # Give the worker a clean import path: just our src/. This mirrors
        # how a host process would spawn us (it doesn't know about our
        # repo layout) and avoids accidentally picking up an editable
        # install with a different models dir.
        env["PYTHONPATH"] = str(ROOT / "src")
        if models_dir is not None:
            env["YANG_XML_GEN_MODELS_DIR"] = str(models_dir)
        else:
            # Default: let the worker find the bundled models/ via __file__
            # resolution (editable-style). Clear any inherited override so
            # the test is deterministic across machines.
            env.pop("YANG_XML_GEN_MODELS_DIR", None)
        if env_override:
            env.update(env_override)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "yang_xml_gen.rpc_worker"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1, env=env,
        )
        self._stderr_buf: list[str] = []

    def __enter__(self) -> "_Worker":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def readline(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(
                "worker stdout closed unexpectedly; stderr="
                + (self.proc.stderr.read() or "<empty>")
            )
        return json.loads(line)

    def call(self, method: str, params: dict | None = None,
             req_id: int | str = 1) -> dict:
        """Send one request and return its response dict."""
        req = {"id": req_id, "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        return self.readline()

    def send_raw(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def close(self, *, expect_exit: int = 0) -> None:
        if self.proc.poll() is not None:
            return  # already exited
        try:
            self.send_raw({"method": "shutdown"})
        except (BrokenPipeError, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        # Drain stderr for diagnostics; don't assert on it (warnings may
        # legitimately appear if a future test exercises them).

    def stderr(self) -> str:
        return self.proc.stderr.read()


# ----------------------------------------------------------------------


class GreetingTests(unittest.TestCase):
    """The first stdout line tells the host whether the worker is usable."""

    def test_ready_greeting_with_bundled_models(self):
        # Default resolution finds the repo's models/ via __file__.
        with _Worker() as w:
            greeting = w.readline()
            self.assertTrue(greeting["ready"])
            self.assertIn("models_dir", greeting)
            self.assertIsInstance(greeting["module_count"], int)
            self.assertGreater(greeting["module_count"], 0)

    def test_ready_greeting_reports_models_dir(self):
        with _Worker(models_dir=MODELS) as w:
            greeting = w.readline()
            self.assertTrue(greeting["ready"])
            # The reported path is the one we asked for (absolute form may
            # differ on Windows; compare resolved).
            self.assertEqual(
                Path(greeting["models_dir"]).resolve(),
                MODELS.resolve(),
            )

    def test_empty_models_dir_loads_zero_modules(self):
        # An env var pointing at a non-existent path does NOT raise --
        # FileRepository tolerates it and _pick_latest globs to an empty
        # list, so the worker is "ready" but with zero modules. (The
        # hard-fail RuntimeError only happens when ALL three resolution
        # sources are None, which can't occur from a source checkout since
        # the bundled models/ always resolves.) The host treats
        # module_count==0 as "misconfigured" and surfaces an install hint.
        with _Worker(models_dir=Path("/no/such/models/dir/here")) as w:
            greeting = w.readline()
            self.assertTrue(greeting["ready"])
            self.assertEqual(greeting["module_count"], 0)
            # And list_modules agrees.
            resp = w.call("list_modules", req_id="empty")
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["result"], [])


class MethodTests(unittest.TestCase):
    """Each RPC method returns the right shape over the wire."""

    @classmethod
    def setUpClass(cls):
        cls.worker = _Worker()
        cls.greeting = cls.worker.readline()
        if not cls.greeting["ready"]:
            raise unittest.SkipTest(
                f"worker not ready: {cls.greeting.get('error')}"
            )

    @classmethod
    def tearDownClass(cls):
        cls.worker.close()

    def test_list_modules_includes_example_toaster(self):
        resp = self.worker.call("list_modules", req_id="lm")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["id"], "lm")
        self.assertIn("example-toaster", resp["result"])

    def test_roots_reports_container_and_rpc_kinds(self):
        resp = self.worker.call("roots",
                                {"module": "example-toaster"}, req_id="rt")
        self.assertTrue(resp["ok"], resp)
        names = {item["name"]: item["kind"] for item in resp["result"]}
        # The toaster model has a `toaster` container and two rpcs.
        self.assertEqual(names.get("toaster"), "container")
        self.assertEqual(names.get("make-toast"), "rpc")
        self.assertEqual(names.get("cancel-toast"), "rpc")

    def test_template_returns_spec_skeleton(self):
        resp = self.worker.call("template",
                                {"module": "example-toaster",
                                 "root": "toaster"}, req_id="tp")
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertEqual(result["module"], "example-toaster")
        self.assertEqual(result["root"], "toaster")
        # Every writable leaf is present and empty (scaffold invariant).
        self.assertEqual(result["data"], {
            "darkness": "", "toast-type": "", "mode": "", "label": "",
        })
        self.assertEqual(resp["warnings"], [])

    def test_build_bare(self):
        resp = self.worker.call("build", {
            "module": "example-toaster", "root": "toaster", "wrap": "bare",
            "data": {"darkness": 7, "mode": "regular"},
        }, req_id="bb")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn("<toaster", xml)
        self.assertIn("<darkness>7</darkness>", xml)
        self.assertNotIn("<rpc", xml)  # bare = no envelope

    def test_build_edit_config(self):
        resp = self.worker.call("build", {
            "module": "example-toaster", "root": "toaster",
            "wrap": "edit-config", "operation": "merge",
            "data": {"darkness": 7, "toast-type": "wheat-bread",
                     "mode": "defrost", "label": "Kitchen"},
        }, req_id="ec")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn("<nc:rpc", xml)
        self.assertIn("<nc:edit-config", xml)
        self.assertIn('nc:operation="merge"', xml)
        self.assertIn("<darkness>7</darkness>", xml)
        # identityref auto-prefix: toaster:wheat-bread + its namespace decl.
        self.assertIn("toaster:wheat-bread", xml)

    def test_build_rpc_call(self):
        # make-toast is an rpc; its input params sit directly under the rpc
        # element (no <config> wrapper).
        resp = self.worker.call("build", {
            "module": "example-toaster", "root": "make-toast",
            "wrap": "rpc", "message_id": 42,
            "data": {"toastDoneness": 8, "toastType": "wheat-bread"},
        }, req_id="rpc")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn('message-id="42"', xml)
        self.assertIn("<make-toast", xml)
        self.assertIn("<toastDoneness>8</toastDoneness>", xml)

    def test_build_get_config_no_filter(self):
        # Full retrieval: no filter element at all.
        resp = self.worker.call("build", {
            "wrap": "get-config", "target": "running", "message_id": 201,
        }, req_id="gc")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn("<nc:get-config", xml)
        self.assertIn("<nc:running", xml)
        self.assertNotIn("<nc:filter", xml)

    def test_build_get_config_subtree_filter(self):
        # A subtree filter is the *content* of the root node (same shape
        # `build` takes for data), not the root wrapped again. An empty
        # mapping selects the whole subtree (RFC 6241 selection node).
        resp = self.worker.call("build", {
            "module": "example-toaster", "root": "toaster",
            "wrap": "get-config", "filter": {},
        }, req_id="gcs")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn('<nc:filter type="subtree">', xml)
        self.assertIn("<toaster", xml)

    def test_build_get_config_xpath_filter(self):
        resp = self.worker.call("build", {
            "wrap": "get-config", "filter_select": "/t:toaster",
        }, req_id="gcx")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn('<nc:filter type="xpath"', xml)
        self.assertIn('select="/t:toaster"', xml)

    def test_build_get_with_with_defaults(self):
        resp = self.worker.call("build", {
            "wrap": "get", "with_defaults": "report-all", "message_id": 303,
        }, req_id="gw")
        self.assertTrue(resp["ok"], resp)
        xml = resp["result"]["xml"]
        self.assertIn("<nc:get>", xml)
        self.assertIn("<with-defaults", xml)
        self.assertIn("report-all", xml)

    def test_build_rejects_both_filters(self):
        # Mirrors the CLI's mutual-exclusion check.
        resp = self.worker.call("build", {
            "module": "example-toaster", "root": "toaster",
            "wrap": "get-config",
            "filter": {"toaster": {}}, "filter_select": "/t:toaster",
        }, req_id="bf")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "ValueError")

    def test_build_unknown_wrap_errors(self):
        resp = self.worker.call("build", {"wrap": "bogus"}, req_id="bw")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "ValueError")


class ParseTests(unittest.TestCase):
    """Reverse parsing over the wire, including round-trip."""

    @classmethod
    def setUpClass(cls):
        cls.worker = _Worker()
        cls.greeting = cls.worker.readline()
        if not cls.greeting["ready"]:
            raise unittest.SkipTest(
                f"worker not ready: {cls.greeting.get('error')}"
            )

    @classmethod
    def tearDownClass(cls):
        cls.worker.close()

    def test_parse_reply_toaster(self):
        reply = (EXAMPLES / "toaster-reply.xml").read_text(encoding="utf-8")
        resp = self.worker.call("parse_reply", {"xml": reply},
                                req_id="pr")
        self.assertTrue(resp["ok"], resp)
        result = resp["result"]
        self.assertEqual(result["module"], "example-toaster")
        self.assertEqual(result["root"], "toaster")
        # The reply carries the values we expect (round-trip target).
        self.assertEqual(result["data"]["darkness"], "7")
        self.assertEqual(result["data"]["mode"], "defrost")
        self.assertIn("wheat-bread", result["data"]["toast-type"])

    def test_parse_reply_data_only(self):
        reply = (EXAMPLES / "toaster-reply.xml").read_text(encoding="utf-8")
        resp = self.worker.call("parse_reply",
                                {"xml": reply, "data_only": True},
                                req_id="prd")
        self.assertTrue(resp["ok"], resp)
        # data_only strips the {module, root, data} envelope.
        self.assertNotIn("module", resp["result"])
        self.assertNotIn("root", resp["result"])
        self.assertIn("darkness", resp["result"])

    def test_parse_fragment_default_data_only(self):
        # parse_fragment defaults to data_only=True (opposite of parse_reply);
        # confirm the asymmetry survives the wire.
        reply = (EXAMPLES / "toaster-reply.xml").read_text(encoding="utf-8")
        # Strip the <rpc-reply><data> wrapper to get a bare fragment.
        from xml.etree import ElementTree as ET
        root = ET.fromstring(reply)
        data_el = root.find("{urn:ietf:params:netconf:base:1.0}data")
        fragment = ET.tostring(data_el[0], encoding="unicode")
        resp = self.worker.call("parse_fragment", {"xml": fragment},
                                req_id="pf")
        self.assertTrue(resp["ok"], resp)
        # Default data_only=True -> just the data mapping.
        self.assertIn("darkness", resp["result"])
        self.assertNotIn("module", resp["result"])

    def test_round_trip_build_then_parse(self):
        # Build XML from a spec, then parse it back. Per the README, the
        # reverse parser returns leaf text as strings (booleans/empty are
        # the only type-coercing cases), so an integer 7 round-trips as
        # "7". identityref keeps its prefix on both sides. We compare the
        # parsed data against the string-coerced spec.
        spec_data = {"darkness": 7, "toast-type": "toaster:wheat-bread",
                     "mode": "defrost", "label": "Counter"}
        expected_after_parse = {
            k: (str(v) if not isinstance(v, str) else v)
            for k, v in spec_data.items()
        }
        build_resp = self.worker.call("build", {
            "module": "example-toaster", "root": "toaster",
            "wrap": "bare", "data": spec_data,
        }, req_id="rt-build")
        self.assertTrue(build_resp["ok"], build_resp)
        fragment = build_resp["result"]["xml"]

        parse_resp = self.worker.call("parse_fragment", {
            "xml": fragment, "module": "example-toaster",
            "root": "toaster", "data_only": True,
        }, req_id="rt-parse")
        self.assertTrue(parse_resp["ok"], parse_resp)
        self.assertEqual(parse_resp["result"], expected_after_parse)


class ValidateTests(unittest.TestCase):
    """``validate`` runs a build purely to collect type-constraint warnings."""

    @classmethod
    def setUpClass(cls):
        cls.worker = _Worker()
        cls.greeting = cls.worker.readline()
        if not cls.greeting["ready"]:
            raise unittest.SkipTest(
                f"worker not ready: {cls.greeting.get('error')}"
            )

    @classmethod
    def tearDownClass(cls):
        cls.worker.close()

    def test_valid_data_has_no_warnings(self):
        resp = self.worker.call("validate", {
            "module": "example-toaster", "root": "toaster", "wrap": "bare",
            "data": {"darkness": 5, "mode": "regular"},
        }, req_id="vok")
        self.assertTrue(resp["ok"], resp)
        self.assertEqual(resp["warnings"], [])

    def test_out_of_range_emits_warning(self):
        # darkness is uint8 with range 1..10; 99 violates it.
        resp = self.worker.call("validate", {
            "module": "example-toaster", "root": "toaster", "wrap": "bare",
            "data": {"darkness": 99},
        }, req_id="vr")
        self.assertTrue(resp["ok"], resp)  # non-blocking: still ok
        kinds = " ".join(w["message"] for w in resp["warnings"])
        self.assertIn("darkness", kinds)
        self.assertIn("range", kinds.lower() + "uint8")

    def test_bad_enum_emits_warning(self):
        # mode only allows regular/defrost/reheat.
        resp = self.worker.call("validate", {
            "module": "example-toaster", "root": "toaster", "wrap": "bare",
            "data": {"mode": "nuclear"},
        }, req_id="ve")
        self.assertTrue(resp["ok"], resp)
        kinds = " ".join(w["message"] for w in resp["warnings"])
        self.assertIn("mode", kinds)
        self.assertIn("enumeration", kinds)

    def test_build_also_carries_warnings(self):
        # The warning trap applies to build too, not just validate.
        resp = self.worker.call("build", {
            "module": "example-toaster", "root": "toaster", "wrap": "bare",
            "data": {"darkness": 99, "mode": "nuclear"},
        }, req_id="bw")
        self.assertTrue(resp["ok"], resp)
        self.assertTrue(len(resp["warnings"]) >= 2, resp["warnings"])
        # XML is still produced despite the warnings.
        self.assertIn("<darkness>99</darkness>", resp["result"]["xml"])


class ErrorFramingTests(unittest.TestCase):
    """Exceptions become {ok: false, error: {...}}, never tracebacks."""

    @classmethod
    def setUpClass(cls):
        cls.worker = _Worker()
        cls.greeting = cls.worker.readline()
        if not cls.greeting["ready"]:
            raise unittest.SkipTest(
                f"worker not ready: {cls.greeting.get('error')}"
            )

    @classmethod
    def tearDownClass(cls):
        cls.worker.close()

    def test_unknown_module_keyerror(self):
        resp = self.worker.call("roots", {"module": "no-such-module"},
                                req_id="e1")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "KeyError")
        self.assertEqual(resp["id"], "e1")

    def test_unknown_root_keyerror(self):
        resp = self.worker.call("template", {
            "module": "example-toaster", "root": "no-such-root",
        }, req_id="e2")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "KeyError")

    def test_malformed_xml_parse_error(self):
        resp = self.worker.call("parse_reply", {"xml": "not xml at all"},
                                req_id="e3")
        self.assertFalse(resp["ok"])
        self.assertIn(resp["error"]["type"],
                      ("ParseError", "ET.ParseError", "ParseError",
                       "ExpatError", "ValueError"))

    def test_unknown_method(self):
        resp = self.worker.call("bogus_method", {}, req_id="e4")
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["type"], "MethodError")

    def test_id_echoed_on_error(self):
        # The id correlation must survive error paths so the host can
        # reject the right pending promise.
        resp = self.worker.call("roots", {"module": "bad"}, req_id="correl-42")
        self.assertEqual(resp["id"], "correl-42")
        self.assertFalse(resp["ok"])


class LifecycleTests(unittest.TestCase):
    """shutdown and EOF both exit cleanly."""

    def test_shutdown_exits_zero(self):
        with _Worker() as w:
            w.readline()  # consume greeting
            w.send_raw({"method": "shutdown"})
            w.proc.wait(timeout=10)
            self.assertEqual(w.proc.returncode, 0)

    def test_eof_exits_zero(self):
        # Closing stdin (EOF) is the implicit shutdown a host gets if it
        # dies without sending shutdown.
        w = _Worker()
        w.readline()
        w.proc.stdin.close()
        w.proc.wait(timeout=10)
        self.assertEqual(w.proc.returncode, 0)

    def test_malformed_json_line_is_framed(self):
        # A garbage line on stdin must not crash the worker; it answers
        # with a null-id error so the host can log and continue.
        with _Worker() as w:
            w.readline()
            w.proc.stdin.write("this is not json\n")
            w.proc.stdin.flush()
            resp = w.readline()
            self.assertFalse(resp["ok"])
            self.assertIsNone(resp["id"])
            self.assertEqual(resp["error"]["type"], "JSONDecodeError")
            # Worker should still serve a subsequent valid request.
            ok = w.call("list_modules", req_id="after")
            self.assertTrue(ok["ok"])

    def test_serves_multiple_requests_on_one_process(self):
        # The whole point of the warm worker: many calls, one Loader().
        with _Worker() as w:
            w.readline()
            for i in range(5):
                resp = w.call("list_modules", req_id=f"multi-{i}")
                self.assertTrue(resp["ok"])
                self.assertIn("example-toaster", resp["result"])


if __name__ == "__main__":
    unittest.main()
