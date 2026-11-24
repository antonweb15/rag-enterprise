"""Compile the protected core modules into native ``.so`` extensions (Stage M).

`core/licensing_and_distribution.md` §2 / `core/eval_and_ops.md` §4 mandate that the critical core
(the licensing modules, the retrieval/RRF pipeline, context budgeting and chunking) ship in the
On-Premise Box as **compiled C extensions** (Cython) with no readable ``.py`` sources, published to
a private registry.

This script is invoked inside the multi-stage ``Dockerfile`` builder:

    python scripts/compile_core.py --build

It cythonizes every ``.py`` file under the protected package dirs into a ``.so`` (in place) and then
**removes the original ``.py`` sources** (and the ``.c`` intermediates), so the resulting image
contains only binaries for these modules. The honest security note (`core/eval_and_ops.md` §4):
compilation only *raises the reverse-engineering cost*; real protection is the watermark/legal
contour - see :mod:`rag_core.watermark` and §4a.

Run with ``--check`` (no build deps needed) to just print the module inventory - this is what the
static release-pipeline test asserts against.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root = parent of this scripts/ dir.
REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES_ROOT = REPO_ROOT / "packages"

# The protected packages compiled into .so (Box IP). Keep in sync with the Dockerfile and
# IMPLEMENTATION_PLAN.md Stage M / core/eval_and_ops.md §4 / core/licensing_and_distribution.md §2,
# which name the critical core as: the licensing modules, the RRF retrieval pipeline, context
# budgeting and chunking.
PROTECTED_PACKAGES = (
    "rag_core/licensing",
    "rag_core/retrieval",
)

# Individual critical modules (compiled too) that live inside otherwise-open packages: the chunking
# engine and the context-budgeting / prompt-assembly code (core/licensing_and_distribution.md §2).
PROTECTED_MODULES = (
    "rag_core/ingestion/splitter.py",
    "rag_core/generation/prompt.py",
    "rag_core/generation/pipeline.py",
)

# These are never compiled away: package markers / typing markers must stay importable as Python so
# the package still imports after the .py siblings are stripped.
KEEP_SOURCES = ("__init__.py",)


def iter_protected_sources() -> list[Path]:
    """All ``.py`` files (packages + individual modules) that will be cythonized (sorted)."""
    sources: list[Path] = []
    for rel in PROTECTED_PACKAGES:
        pkg_dir = PACKAGES_ROOT / rel
        for py_file in sorted(pkg_dir.rglob("*.py")):
            if py_file.name in KEEP_SOURCES:
                continue
            sources.append(py_file)
    for rel in PROTECTED_MODULES:
        sources.append(PACKAGES_ROOT / rel)
    return sorted(set(sources))


def _print_inventory() -> int:
    """Prints the protected package dirs and the sources that would be compiled."""
    print("Protected packages (compiled to .so, Stage M):")
    for rel in PROTECTED_PACKAGES:
        pkg_dir = PACKAGES_ROOT / rel
        exists = "ok" if pkg_dir.is_dir() else "MISSING"
        print(f"  - {rel} [{exists}]")
    print("Protected modules (compiled to .so, Stage M):")
    for rel in PROTECTED_MODULES:
        mod = PACKAGES_ROOT / rel
        exists = "ok" if mod.is_file() else "MISSING"
        print(f"  - {rel} [{exists}]")
    print("Sources to compile:")
    for src in iter_protected_sources():
        print(f"  - {src.relative_to(REPO_ROOT)}")
    missing_pkgs = [rel for rel in PROTECTED_PACKAGES if not (PACKAGES_ROOT / rel).is_dir()]
    missing_mods = [rel for rel in PROTECTED_MODULES if not (PACKAGES_ROOT / rel).is_file()]
    return 1 if (missing_pkgs or missing_mods) else 0


def _build() -> int:
    """Cythonizes the protected sources in place and strips the original ``.py`` / ``.c`` files."""
    try:
        from Cython.Build import cythonize
        from setuptools import Extension
        from setuptools.dist import Distribution
    except ImportError as exc:  # pragma: no cover - only hit without the build extra
        print(f"error: Cython/setuptools required for --build ({exc})", file=sys.stderr)
        return 2

    sources = iter_protected_sources()
    if not sources:
        print("error: no protected sources found", file=sys.stderr)
        return 1

    extensions = []
    for src in sources:
        # Dotted module name from the path under packages/ (e.g. rag_core.retrieval.pipeline).
        module = ".".join(src.relative_to(PACKAGES_ROOT).with_suffix("").parts)
        extensions.append(Extension(module, [str(src)]))

    ext_modules = cythonize(
        extensions,
        compiler_directives={"language_level": "3"},
        quiet=True,
    )

    dist = Distribution({"ext_modules": ext_modules})
    cmd = dist.get_command_obj("build_ext")
    cmd.inplace = 1
    cmd.build_lib = str(PACKAGES_ROOT)
    cmd.ensure_finalized()
    cmd.run()

    # Strip the readable sources + the generated .c intermediates (keep only the .so binaries).
    for src in sources:
        c_file = src.with_suffix(".c")
        if c_file.exists():
            c_file.unlink()
        src.unlink()
        print(f"compiled + stripped: {src.relative_to(REPO_ROOT)}")

    return 0


def _assert_stripped(install_root: Path) -> int:
    """Fails if any protected module still ships a readable ``.py`` (or misses its ``.so``).

    Run against the INSTALLED ``rag_core`` dir inside the image (Dockerfile builder) and by the
    static release test against a simulated tree. This is the machine-checkable form of the plan's
    "no sources in the image" guarantee (red/yellow of the Stage-M review): compilation that
    silently leaves the .py behind is worse than none (false sense of protection).
    """
    if not install_root.is_dir():
        print(f"error: install root not found: {install_root}", file=sys.stderr)
        return 1
    # install_root IS the rag_core package dir; protected paths are relative to packages/rag_core.
    errors: list[str] = []
    for src in iter_protected_sources():
        rel = src.relative_to(PACKAGES_ROOT / "rag_core")
        installed_py = install_root / rel
        if installed_py.exists():
            errors.append(f"readable source NOT stripped: rag_core/{rel}")
        stem_dir = (install_root / rel).parent
        stem = rel.stem
        # Accept both `pipeline.so` and the versioned `pipeline.cpython-311-....so`.
        has_so = stem_dir.is_dir() and any(
            p.name == f"{stem}.so" or p.name.startswith(f"{stem}.")
            for p in stem_dir.glob(f"{stem}*.so")
        )
        if not has_so:
            errors.append(f"compiled .so MISSING for: rag_core/{rel}")
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"assert-stripped OK: {len(iter_protected_sources())} protected modules compiled, no .py")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile the protected core into .so (Stage M).")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--build", action="store_true", help="cythonize and strip the .py sources in place"
    )
    group.add_argument(
        "--check",
        action="store_true",
        help="only print the module inventory (no build deps needed)",
    )
    group.add_argument(
        "--assert-stripped",
        metavar="RAG_CORE_DIR",
        help="verify an installed rag_core dir has no readable protected .py (and has the .so)",
    )
    args = parser.parse_args(argv)

    if args.build:
        return _build()
    if args.assert_stripped:
        return _assert_stripped(Path(args.assert_stripped))
    return _print_inventory()


if __name__ == "__main__":
    raise SystemExit(main())
