"""Load YANG models from ``models/`` into a validated pyang context.

This is the single entry point that the rest of the package uses to get at
the parsed schema. It reuses the "latest revision per module" rule from
``scripts/compile_models.py`` so that the generator sees exactly the same
set of modules that passed step-1 validation.
"""

from __future__ import annotations

import re
from pathlib import Path

from pyang.context import Context
from pyang.repository import FileRepository

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_MODELS_DIR = REPO_ROOT / "models"

_FILE_RE = re.compile(r"^(?P<name>.+?)@(?P<rev>\d{4}-\d{2}-\d{2})\.yang$")


class Loader:
    """A validated pyang context over a model directory.

    Holding one context for the lifetime of a generation job lets every
    SchemaNode share the same identity tables, so an identityref leaf can
    resolve an identity defined in any loaded module.
    """

    def __init__(self, models_dir: Path | str | None = None):
        self.models_dir = Path(models_dir or DEFAULT_MODELS_DIR)
        self.ctx: Context = self._load()
        # identity-name -> defining module name. Used by the XML builder to
        # prefix identityref values correctly (the prefix must point at the
        # module that defines the *value* identity, not the leaf's module).
        self.identities: dict[str, str] = self._index_identities()
        # namespace-URI -> defining module name. Used by the reverse parser
        # to map a payload element's xmlns back to its YANG module, so the
        # schema can be located without the caller naming the module.
        self.namespaces: dict[str, str] = self._index_namespaces()

    # -- internal ------------------------------------------------------

    def _load(self) -> Context:
        repo = FileRepository(str(self.models_dir), use_env=False)
        ctx = Context(repo)
        targets = _pick_latest(self.models_dir)
        for path in targets:
            ctx.add_module(str(path), path.read_text(encoding="utf-8"))
        ctx.validate()
        return ctx

    def _index_identities(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for name, _rev in self.ctx.modules:
            mod = self.ctx.modules[(name, _rev)]
            for ident in mod.search("identity"):
                # Last write wins is fine: identity names are unique across
                # the loaded set (duplicate definitions would be a step-1
                # compile error).
                index[ident.arg] = ident.i_module.arg
        return index

    def _index_namespaces(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for name, _rev in self.ctx.modules:
            mod = self.ctx.modules[(name, _rev)]
            ns_stmt = mod.search_one("namespace")
            if ns_stmt is None:
                # Submodules have no namespace; they share their including
                # module's namespace, which is already indexed.
                continue
            index[ns_stmt.arg] = name
        return index

    # -- public --------------------------------------------------------

    def get_module(self, name: str):
        """Return the pyang module object for ``name`` (latest revision).

        Raises ``KeyError`` if the module is not loaded.
        """
        mod = self.ctx.get_module(name)
        if mod is None:
            raise KeyError(f"module not loaded: {name}")
        return mod

    def module_by_namespace(self, ns: str) -> str:
        """Return the module name whose YANG ``namespace`` is ``ns``.

        Used by the reverse parser to infer a payload's module from its
        xmlns. Raises ``KeyError`` if no loaded module declares ``ns``.
        """
        try:
            return self.namespaces[ns]
        except KeyError:
            raise KeyError(
                f"no loaded module declares namespace {ns!r}"
            ) from None

    def list_modules(self) -> list[str]:
        """Names of all loaded modules (latest revision only)."""
        # ctx.modules is keyed by (name, revision); de-dup on name.
        seen: list[str] = []
        for name, _rev in self.ctx.modules:
            if name not in seen:
                seen.append(name)
        return sorted(seen)


def _pick_latest(models_dir: Path) -> list[Path]:
    """One file per module name, the latest revision (see step 1)."""
    by_name: dict[str, list[tuple[str, Path]]] = {}
    for path in sorted(models_dir.glob("*.yang")):
        m = _FILE_RE.match(path.name)
        if m:
            name, rev = m.group("name"), m.group("rev")
        else:
            name, rev = path.stem, ""
        by_name.setdefault(name, []).append((rev, path))

    picked: list[Path] = []
    for name, revs in by_name.items():
        revs.sort(key=lambda r: r[0], reverse=True)
        picked.append(revs[0][1])
    return sorted(picked)
