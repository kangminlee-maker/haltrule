#!/usr/bin/env python3
"""What the modules under py/haltrule may name: what three short allowlists hold, and nothing else.

Python has no compiler setting that takes the host away, as "types": [] does for
the TypeScript modules, and its mainstream linters ban names from a list (ruff
cannot ban a bare builtin at all; none of them offers an import allowlist). A
ban list grows with every route someone thinks of. So this states the opposite,
and it is closed: a module may import only the names below, use only the
builtins below, and reach behind an object only by the two attributes below. Everything else - open, print, hash, time, os, a name nobody
has thought of yet - is refused because it is not listed.

    py_purity.py <file.py>...     exit 1 and one line per problem, if any

Scopes come from the standard library's symtable, so a parameter called `id`
is a parameter and a bare `id` is the builtin.
"""

from __future__ import annotations

import ast
import symtable
import sys
from pathlib import Path

ALLOWED_IMPORTS = {
    "__future__": {"annotations"},
    "dataclasses": {"dataclass", "field"},
    "hashlib": {"sha256"},
    "math": {"ceil", "floor", "trunc", "isfinite", "isinf", "isnan", "inf", "nan"},
    "typing": {
        "Any",
        "Final",
        "Iterable",
        "Literal",
        "Mapping",
        "Optional",
        "Sequence",
        "Tuple",
        "Union",
    },
}
SIBLINGS = "haltrule"  # `from haltrule.<module> import ...`, where that module is a file beside this one

ALLOWED_BUILTINS = {
    # values and their types
    "bool", "int", "float", "str", "bytes", "bytearray", "list", "tuple", "dict", "set", "frozenset", "type",
    # pure functions of their arguments
    "abs", "all", "any", "divmod", "enumerate", "isinstance", "len", "max", "min", "ord", "range",
    "reversed", "sorted", "sum", "super", "zip",
    # what a refusal is raised with
    "ArithmeticError", "AssertionError", "OverflowError", "TypeError", "ValueError",
}  # fmt: skip


# Attributes spelled with two leading underscores are how Python reaches behind an object - its class, its
# globals, the builtins. Two are needed; the rest are not listed.
ALLOWED_DUNDERS = {"__init__", "__name__"}


def _builtin_references(
    table: symtable.SymbolTable, module_names: set[str]
) -> set[str]:
    """Names a scope reads that neither it, an enclosing function, nor the module binds."""
    found = set()
    for symbol in table.get_symbols():
        if not symbol.is_referenced() or symbol.get_name() in module_names:
            continue
        if table.get_type() == "module" or symbol.is_global():
            found.add(symbol.get_name())
    for child in table.get_children():
        found |= _builtin_references(child, module_names)
    return found


def problems(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    found: list[tuple[int, str]] = []
    modules: dict[str, str] = {}  # the local name of an imported module -> the module

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    found.append(
                        (
                            node.lineno,
                            f"imports {alias.name}, which is not on the allowlist",
                        )
                    )
                modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                found.append(
                    (node.lineno, "a relative import; name the module in full")
                )
            elif module.startswith(SIBLINGS + "."):
                if not (path.parent / (module.split(".", 1)[1] + ".py")).is_file():
                    found.append(
                        (
                            node.lineno,
                            f"imports from {module}, which is not a file beside this one",
                        )
                    )
            elif module not in ALLOWED_IMPORTS:
                found.append(
                    (
                        node.lineno,
                        f"imports from {module}, which is not on the allowlist",
                    )
                )
            else:
                for alias in node.names:
                    if alias.name not in ALLOWED_IMPORTS[module]:
                        found.append(
                            (
                                node.lineno,
                                f"imports {module}.{alias.name}, which is not on the allowlist",
                            )
                        )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("__")
            and node.attr not in ALLOWED_DUNDERS
        ):
            found.append(
                (
                    node.lineno,
                    f"uses the attribute {node.attr}, which is not on the allowlist",
                )
            )

    # An imported module is used as module.name, the name being listed; never as a value.
    module_bases = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            module_bases.add(id(node.value))
            module = modules[node.value.id]
            if node.attr not in ALLOWED_IMPORTS.get(module, set()):
                found.append(
                    (
                        node.lineno,
                        f"uses {module}.{node.attr}, which is not on the allowlist",
                    )
                )
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and node.id in modules
            and id(node) not in module_bases
        ):
            found.append(
                (node.lineno, f"uses the module {modules[node.id]} as a value")
            )

    table = symtable.symtable(source, str(path), "exec")
    module_names = {
        s.get_name()
        for s in table.get_symbols()
        if s.is_assigned() or s.is_imported() or s.is_namespace()
    }
    # Only names the source spells: the compiler adds symbols of its own (3.14: __conditional_annotations__).
    lines: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            lines[node.id] = min(node.lineno, lines.get(node.id, node.lineno))
    for name in sorted(
        (_builtin_references(table, module_names) & lines.keys()) - ALLOWED_BUILTINS
    ):
        found.append(
            (lines[name], f"uses the builtin {name}, which is not on the allowlist")
        )

    return [f"{path}:{line}: {text}" for line, text in sorted(found)]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    found = [problem for arg in argv for problem in problems(Path(arg))]
    for problem in found:
        print(problem)
    if not found:
        print(f"OK: {len(argv)} file(s) name only what the allowlists hold")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
