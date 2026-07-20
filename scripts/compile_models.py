#!/usr/bin/env python
"""Batch-compile all YANG models in ``models/``.

Goal of step 1: prove the model set is internally consistent by loading
every module through pyang with the model directory on the search path,
so all ``import`` chains resolve.

The repo keeps multiple revisions of several OpenConfig modules side by
side. Loading every revision at once makes pyang reject duplicate
``augment`` targets (two revisions of the same module cannot both augment
the same node). That is not a real dependency problem -- it is an artifact
of putting several versions in one directory. We therefore compile each
module name only once, using its latest revision.

Usage::

    python scripts/compile_models.py            # latest revision per module
    python scripts/compile_models.py --all      # every file, every revision
    python scripts/compile_models.py --module openconfig-interfaces
    python scripts/compile_models.py --json report.json

Exit code is non-zero if any error is found, so this doubles as a CI check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Resolve pyang whether it is installed as a package or run from a checkout.
try:
    from pyang import error as pyang_error
    from pyang.context import Context
    from pyang.repository import FileRepository
except ImportError:  # pragma: no cover - environment hint
    sys.stderr.write(
        "pyang is not installed. Install it with:\n"
        "    python -m pip install pyang\n"
    )
    raise

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"

# module-name -> (revision, path)
_FILE_RE = re.compile(r"^(?P<name>.+?)@(?P<rev>\d{4}-\d{2}-\d{2})\.yang$")


def discover_models(models_dir: Path) -> dict[str, list[tuple[str, Path]]]:
    """Group model files by module name, preserving every revision found."""
    by_name: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for path in sorted(models_dir.glob("*.yang")):
        m = _FILE_RE.match(path.name)
        if m:
            name, rev = m.group("name"), m.group("rev")
        else:
            # Files without a revision date in the name. Fall back to the
            # revision statement inside the module at compile time; group by
            # bare filename so they are still compiled.
            name, rev = path.stem, ""
        by_name[name].append((rev, path))
    return by_name


def pick_latest(by_name: dict[str, list[tuple[str, Path]]]) -> list[Path]:
    """Return one file per module name, the latest revision."""
    picked: list[Path] = []
    for name, revs in by_name.items():
        # Sort by revision descending; empty revision sorts last so a dated
        # file always wins over an undated one with the same stem.
        revs_sorted = sorted(revs, key=lambda r: r[0], reverse=True)
        picked.append(revs_sorted[0][1])
    return sorted(picked)


def compile_files(targets: list[Path]) -> list[dict]:
    """Compile ``targets`` together in one pyang context.

    Returning a list of issue dicts keeps the caller free of pyang types.

    Each pyang error is a 3-tuple ``(Position, code, args)``. ``code`` is a
    string such as ``DUPLICATE_CHILD_NAME``; its severity lives in
    ``pyang.error.error_codes[code]`` as ``(level_int, format_str)``. We map
    that to a stable ``"error"``/``"warning"`` label and render the message
    with pyang's own format string so the wording matches ``pyang`` CLI.
    """
    repo = FileRepository(str(MODELS_DIR), use_env=False)
    ctx = Context(repo)
    for path in targets:
        text = path.read_text(encoding="utf-8")
        ctx.add_module(str(path), text)
    ctx.validate()

    issues: list[dict] = []
    for pos, code, args in ctx.errors:
        level = "error"
        message = code
        entry = pyang_error.error_codes.get(code)
        if entry is not None:
            level_int, fmt = entry[0], entry[1]
            if pyang_error.is_warning(level_int):
                level = "warning"
            try:
                message = fmt % (args if isinstance(args, tuple) else (args,))
            except Exception:
                # Fall back to the raw args if formatting fails; never crash
                # the report because of a malformed error tuple.
                message = f"{code}: {args}"
        issues.append(
            {
                "file": getattr(pos, "ref", None) or str(pos),
                "line": getattr(pos, "line", None),
                "level": level,
                "code": code,
                "message": message,
            }
        )
    return issues


def render_report(targets: list[Path], issues: list[dict]) -> str:
    errors = [i for i in issues if i["level"] == "error"]
    warnings = [i for i in issues if i["level"] == "warning"]

    lines = []
    lines.append(f"Compiled {len(targets)} module(s) from {MODELS_DIR}")
    lines.append(f"  errors:   {len(errors)}")
    lines.append(f"  warnings: {len(warnings)}")
    lines.append("")
    if errors:
        lines.append("=== errors ===")
        for i in errors:
            lines.append(_fmt_issue(i))
        lines.append("")
    if warnings:
        lines.append("=== warnings ===")
        for i in warnings:
            lines.append(_fmt_issue(i))
        lines.append("")
    if not issues:
        lines.append("All modules compiled cleanly.")
    return "\n".join(lines)


def _fmt_issue(i: dict) -> str:
    loc = i["file"]
    if i["line"]:
        loc += f":{i['line']}"
    return f"[{i['level']}] {loc}: {i['message']}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--models-dir",
        type=Path,
        default=MODELS_DIR,
        help="directory containing .yang files (default: ../models)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="compile every revision, not just the latest per module",
    )
    p.add_argument(
        "--module",
        action="append",
        help="compile only this module name (repeatable); implies latest revision",
    )
    p.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        help="write a machine-readable report to this path",
    )
    args = p.parse_args(argv)

    by_name = discover_models(args.models_dir)

    if args.module:
        missing = [m for m in args.module if m not in by_name]
        if missing:
            sys.stderr.write("Unknown module(s): " + ", ".join(missing) + "\n")
            return 2
        targets = sorted(
            sorted(by_name[m], key=lambda r: r[0], reverse=True)[0][1]
            for m in args.module
        )
    elif args.all:
        targets = sorted(p for revs in by_name.values() for _, p in revs)
    else:
        targets = pick_latest(by_name)

    issues = compile_files(targets)
    # Keep output stable regardless of pyang's internal ordering.
    issues.sort(key=lambda i: (i["level"], i["file"], i["line"] or 0))

    print(render_report(targets, issues))

    if args.json_path:
        args.json_path.write_text(
            json.dumps(
                {
                    "models_dir": str(args.models_dir),
                    "compiled": [str(p) for p in targets],
                    "issues": issues,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return 1 if any(i["level"] == "error" for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
