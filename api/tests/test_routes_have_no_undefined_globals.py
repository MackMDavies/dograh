"""Catch names a route uses but never imports.

`get_campaign_defaults` called `get_org_concurrency_limit` without importing
it. Python resolves globals at call time, so the module imported cleanly,
every existing test passed, and the endpoint 500'd on every request in
production until someone read the traceback. The browser could not even show
the status: a FastAPI 500 escapes before CORSMiddleware attaches headers, so
it surfaced only as "Failed to fetch".

Import-time checks cannot catch this. This walks each route function's
bytecode for LOAD_GLOBAL and asserts the name resolves against the module's
globals or builtins — exactly the lookup the interpreter performs at runtime.

LOAD_GLOBAL specifically, not co_names: co_names also holds attribute names
(the "get" in `x.get(...)`), which would fire on every dict access in the
codebase.
"""

import builtins
import dis
import importlib
import pkgutil
import types

import pytest

import api.routes


def _iter_code(module):
    """Every code object defined in this module, with its qualified name.

    Code objects are walked directly rather than rebuilt into functions:
    a nested code object that closes over a variable cannot be turned back
    into a FunctionType without its closure tuple, and comprehensions and
    inner helpers carry LOAD_GLOBALs worth checking.
    """
    seen: set = set()

    module_file = getattr(module, "__file__", None)

    def walk_code(code, prefix):
        if code in seen:
            return
        seen.add(code)
        # Only code compiled from this module's own file. Libraries that build
        # methods with exec — dataclasses' _recursive_repr wrapper is the one
        # that bit here — stamp the generated function with the defining
        # module's name while its code still points at the stdlib, and its
        # internals (`_thread.get_ident`) are not ours to vouch for.
        if module_file and code.co_filename == module_file:
            yield f"{prefix}.{code.co_name}" if prefix else code.co_name, code
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                yield from walk_code(const, prefix)

    def walk_obj(obj):
        if isinstance(obj, types.FunctionType):
            # Only functions this module owns — an imported helper is its own
            # module's responsibility, and would be checked there.
            if obj.__module__ == module.__name__:
                yield from walk_code(obj.__code__, obj.__qualname__.rsplit(".", 1)[0]
                                     if "." in obj.__qualname__ else "")
        elif isinstance(obj, (staticmethod, classmethod)):
            yield from walk_obj(obj.__func__)
        elif isinstance(obj, type) and obj.__module__ == module.__name__:
            for attr in vars(obj).values():
                yield from walk_obj(attr)

    for value in vars(module).values():
        yield from walk_obj(value)


def _route_modules():
    """Import what we can. Some modules need runtime deps this environment
    stubs out; those are skipped rather than failing the sweep, and the
    coverage assertions below stop that becoming a silent no-op."""
    mods, skipped = [], []
    for info in pkgutil.iter_modules(api.routes.__path__):
        try:
            mods.append(importlib.import_module(f"api.routes.{info.name}"))
        except Exception as exc:
            skipped.append(f"{info.name}: {exc}")
    return mods, skipped


ROUTE_MODULES, SKIPPED_MODULES = _route_modules()

# The modules this guard must never silently stop covering. `organization` is
# the one that shipped the NameError; the others carry the dialling paths.
REQUIRED = {"organization", "campaign", "telephony"}


def test_route_modules_were_discovered():
    """A guard on the guard: an empty sweep would pass silently."""
    assert len(ROUTE_MODULES) > 5, f"expected route modules, skipped: {SKIPPED_MODULES}"


def test_critical_modules_are_covered():
    scanned = {m.__name__.rsplit(".", 1)[-1] for m in ROUTE_MODULES}
    missing = REQUIRED - scanned
    assert not missing, (
        f"these modules must be scanned but could not be imported: {missing}. "
        f"Skipped: {SKIPPED_MODULES}"
    )


@pytest.mark.parametrize("module", ROUTE_MODULES, ids=lambda m: m.__name__)
def test_no_undefined_global_names(module):
    undefined = []

    for qualname, code in _iter_code(module):
        for instruction in dis.get_instructions(code):
            if instruction.opname != "LOAD_GLOBAL":
                continue
            name = instruction.argval
            if name in module.__dict__ or hasattr(builtins, name):
                continue
            undefined.append(f"{qualname} uses undefined name {name!r}")

    assert not undefined, (
        f"{module.__name__} references names it never imports or defines. "
        "These raise NameError at request time, not import time:\n  "
        + "\n  ".join(sorted(set(undefined)))
    )
