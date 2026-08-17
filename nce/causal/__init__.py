"""
nce.causal — Causal Inference Layer (BATCH-P2-004)

Public API surface:

    from nce.causal import CausalGraph, DoCalculusEngine, InterventionResult
"""

from nce.causal.correlation import (
    _FORWARD_FAILURE_TYPES,
    _REVERSE_FAILURE_TYPES,
    CausalEdge,
    CausalGraph,
    CausalNode,
    ConfoundingPath,
    DoCalculusEngine,
    ImpactScore,
    InterventionResult,
    evaluate_intervention,
)
from nce.causal.synthesis import (
    PREDICTIVE_NODE_SCHEMA,
    PredictiveSynthesisEngine,
)

__all__ = [
    "CausalEdge",
    "CausalGraph",
    "CausalNode",
    "ConfoundingPath",
    "DoCalculusEngine",
    "ImpactScore",
    "InterventionResult",
    "evaluate_intervention",
    "PredictiveSynthesisEngine",
    "PREDICTIVE_NODE_SCHEMA",
    "_FORWARD_FAILURE_TYPES",
    "_REVERSE_FAILURE_TYPES",
]
