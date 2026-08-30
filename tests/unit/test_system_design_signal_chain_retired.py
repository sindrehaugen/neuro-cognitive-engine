"""Ratchet: the ``SIGNAL_CHAIN`` node type stays retired (Batch 067i).

``SIGNAL_CHAIN`` was declared in ``devices.py`` and registered in
``node-ownership.json``, but no module ever wrote a node of that type and its
label helper ``signal_chain_label`` had zero call sites repo-wide. A signal
chain is a ``connected_to`` walk over ``PORT`` nodes -- which is what
``validation_queries.py`` already traverses. Batch 067i removed the declaration
and the dead helper; see ``nce/vertical_modules/system_design/README.md`` for
the full decision record and for why the ownership row was left inert.

These tests exist so the phantom cannot silently return. They assert absence, so
they fail the moment anything re-introduces the symbol -- re-adding
``signal_chain_label`` or ``NODE_TYPE_SIGNAL_CHAIN`` to ``devices.py`` turns
every one of them red. A future wave that genuinely needs materialised chain
nodes must delete this file deliberately and record why.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nce.vertical_modules.system_design import devices

# The exact names Batch 067i removed.
_RETIRED_ATTRIBUTES = (
    "signal_chain_label",
    "NODE_TYPE_SIGNAL_CHAIN",
    "_NODE_TYPE_SIGNAL_CHAIN",
)

_README = Path(devices.__file__).resolve().parent / "README.md"


@pytest.mark.parametrize("attribute", _RETIRED_ATTRIBUTES)
def test_retired_attribute_is_absent(attribute: str) -> None:
    """Neither the label helper nor the node-type constants may come back."""
    assert not hasattr(devices, attribute), (
        f"{attribute!r} is back on devices.py. SIGNAL_CHAIN was retired in "
        "Batch 067i: a signal chain is a connected_to walk over PORT nodes, "
        "not a node type. See the module README before re-adding it."
    )


def test_no_exported_name_mentions_signal_chain() -> None:
    """No attribute of any kind may carry the retired type in its name."""
    offenders = sorted(n for n in vars(devices) if "SIGNAL_CHAIN" in n.upper())
    assert offenders == [], f"devices.py exports SIGNAL_CHAIN-named attributes again: {offenders}"


def test_no_node_type_constant_holds_the_signal_chain_string() -> None:
    """Catch a re-introduction that renames the constant but keeps the value."""
    offenders = sorted(
        name
        for name, value in vars(devices).items()
        if isinstance(value, str) and not name.startswith("__") and value == "SIGNAL_CHAIN"
    )
    assert offenders == [], f"devices.py declares the SIGNAL_CHAIN node type again via {offenders}"


def test_no_label_helper_emits_a_signal_chain_label() -> None:
    """Catch a re-introduction that hides behind a differently named helper."""
    helpers = {
        name: fn
        for name, fn in vars(devices).items()
        if name.endswith("_label") and inspect.isfunction(fn)
    }
    assert helpers, "expected devices.py to still expose its label helpers"

    for name, fn in helpers.items():
        arity = len(inspect.signature(fn).parameters)
        label = fn(*["X"] * arity)
        assert not label.startswith("SIGNAL_CHAIN"), (
            f"{name}() emits a SIGNAL_CHAIN label ({label!r}); the type is retired"
        )


def test_module_readme_records_the_retirement_decision() -> None:
    """The decision record is the deliverable -- it must not be deleted."""
    assert _README.is_file(), f"missing module decision record: {_README}"
    text = _README.read_text(encoding="utf-8")
    assert "SIGNAL_CHAIN" in text and "connected_to" in text, (
        "README.md no longer records why SIGNAL_CHAIN was retired"
    )
