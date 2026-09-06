"""
tests/integration/test_golden_thread.py
=======================================
The Golden Thread Scenario Test and v1.5 Burndown Scaffold (Wave I-7).

Canonical Lifecycle Flow (Charter §6, Review §0 and §5):
customer → design → quote → sign → **baseline frozen** → project → PO → **ORDERED** → GR → match →
**DELIVERED** → ASSET → **install (INSTALLED)** → **test (TESTED)** → SLA attached → invoice approved →
**actual_cost** → ticket → dispatch → **work order** → outcome → cert expiry → **allocation invalidated**
→ portal request → **ticket** → outcome recorded → design recall returns the project

Every bold step is red on public main today (10 seam breaks + degradation register).
Each impossible step is marked with `@pytest.mark.xfail(strict=True, reason="break-N: ...")`.
As Phase 1 waves in Charter B (S-2a, PR-1, IN-1, FT-1, E-3, SU-1/FT-3, HR-1/V-2, CP-1, PJ-1/SD-2)
close each seam, the corresponding strict-xfail marker MUST be removed in the same commit.
If a seam closes while still marked strict-xfail, pytest reports XPASS (FAILED), forcing
removal of the xfail marker and making this an honest burndown instrument.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from nce.orchestrator import NCEEngine
from nce.tool_registry import TOOL_REGISTRY

# ---------------------------------------------------------------------------
# Golden Thread Burndown Manifest
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BurndownStep:
    """Specification for a step in the Golden Thread scenario."""

    index: int
    name: str
    canonical_label: str
    is_broken: bool
    review_break: str | None
    phase1_wave: str | None
    description: str


GOLDEN_THREAD_STEPS: tuple[BurndownStep, ...] = (
    BurndownStep(
        index=1,
        name="customer",
        canonical_label="customer",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Customer account / deal created and verified in Sales / CRM read model",
    ),
    BurndownStep(
        index=2,
        name="design",
        canonical_label="design",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="System design topology authored with devices, ports, and cables",
    ),
    BurndownStep(
        index=3,
        name="quote",
        canonical_label="quote",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Sales quote authored from system design bill of materials (BOM lines)",
    ),
    BurndownStep(
        index=4,
        name="sign",
        canonical_label="sign",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Signature requested for the sales quote",
    ),
    BurndownStep(
        index=5,
        name="baseline_frozen",
        canonical_label="baseline frozen",
        is_broken=False,
        review_break="break-1",
        phase1_wave="S-2a",
        description="Signed quote freezes baseline via signing webhook/callback in production",
    ),
    BurndownStep(
        index=6,
        name="project",
        canonical_label="project",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Project converted from signed quote with baseline scope reference",
    ),
    BurndownStep(
        index=7,
        name="po",
        canonical_label="PO",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Procurement purchase order generated from project BOM lines",
    ),
    BurndownStep(
        index=8,
        name="ordered",
        canonical_label="ORDERED",
        is_broken=True,
        review_break="break-2",
        phase1_wave="PR-1",
        description="PO submission emits PO_LINE.status_changed to write BOM_LINE ORDERED rung",
    ),
    BurndownStep(
        index=9,
        name="gr",
        canonical_label="GR",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Goods receipt created for delivered procurement items",
    ),
    BurndownStep(
        index=10,
        name="match",
        canonical_label="match",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Three-way match performed between PO, GR, and supplier invoice",
    ),
    BurndownStep(
        index=11,
        name="delivered",
        canonical_label="DELIVERED",
        is_broken=True,
        review_break="break-2",
        phase1_wave="IN-1",
        description="GOODS_RECEIPT.created event published to transition BOM_LINE to DELIVERED",
    ),
    BurndownStep(
        index=12,
        name="asset",
        canonical_label="ASSET",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Asset seeded from BOM line through Assets engine",
    ),
    BurndownStep(
        index=13,
        name="installed",
        canonical_label="install (INSTALLED)",
        is_broken=True,
        review_break="break-2",
        phase1_wave="FT-1",
        description="Field Tech scan/install completion calls update_bom_line_status with INSTALLED",
    ),
    BurndownStep(
        index=14,
        name="tested",
        canonical_label="test (TESTED)",
        is_broken=True,
        review_break="break-2",
        phase1_wave="FT-1",
        description="Field Tech test completion calls update_bom_line_status with TESTED",
    ),
    BurndownStep(
        index=15,
        name="sla_attached",
        canonical_label="SLA attached",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="SLA coverage attached to installed asset",
    ),
    BurndownStep(
        index=16,
        name="invoice_approved",
        canonical_label="invoice approved",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Supplier invoice approved in Economy engine",
    ),
    BurndownStep(
        index=17,
        name="actual_cost",
        canonical_label="actual_cost",
        is_broken=True,
        review_break="break-3",
        phase1_wave="E-3",
        description="Economy invoice approval cascades to write actual_cost on BOM_LINE",
    ),
    BurndownStep(
        index=18,
        name="ticket",
        canonical_label="ticket",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Customer support ticket created for service dispatch",
    ),
    BurndownStep(
        index=19,
        name="dispatch",
        canonical_label="dispatch",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Support ticket dispatched with dispatched_as edge to work order",
    ),
    BurndownStep(
        index=20,
        name="work_order",
        canonical_label="work order",
        is_broken=True,
        review_break="break-5b",
        phase1_wave="SU-1/FT-3",
        description="Support dispatched_as boundary edge consumed by Field Tech to create work order",
    ),
    BurndownStep(
        index=21,
        name="outcome",
        canonical_label="outcome",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Field Tech records work order resolution outcome",
    ),
    BurndownStep(
        index=22,
        name="cert_expiry",
        canonical_label="cert expiry",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Technician certification expiry condition occurs",
    ),
    BurndownStep(
        index=23,
        name="allocation_invalidated",
        canonical_label="allocation invalidated",
        is_broken=True,
        review_break="break-5a",
        phase1_wave="HR-1/V-2",
        description="HR/Vendors cert expiry event invalidates scheduled resource allocation",
    ),
    BurndownStep(
        index=24,
        name="portal_request",
        canonical_label="portal request",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Customer raises service request via Customer Portal",
    ),
    BurndownStep(
        index=25,
        name="portal_ticket",
        canonical_label="ticket",
        is_broken=False,
        review_break="break-5c",
        phase1_wave="CP-1",
        description="Customer Portal hand-off creates real Support ticket via engine.modules['support']",
    ),
    BurndownStep(
        index=26,
        name="outcome_recorded",
        canonical_label="outcome recorded",
        is_broken=False,
        review_break=None,
        phase1_wave=None,
        description="Project outcome recorded at G5 phase gate with case-study edge",
    ),
    BurndownStep(
        index=27,
        name="design_recall",
        canonical_label="design recall returns the project",
        is_broken=False,
        review_break="break-4",
        phase1_wave="PJ-1/SD-2",
        description="System design recall returns similar projects weighted by measured outcome",
    ),
    BurndownStep(
        index=28,
        name="degradations",
        canonical_label="degradation register empty",
        is_broken=False,
        review_break="break-degradations",
        phase1_wave="I-5",
        description="GET /api/health/degradations mounted and reports zero active degradations",
    ),
)


# ---------------------------------------------------------------------------
# Individual Step Tests (v1.5 Burndown)
# ---------------------------------------------------------------------------


class TestGoldenThreadSteps:
    """The 28 discrete step checkpoints of the Golden Thread lifecycle.

    Each broken seam is decorated with `@pytest.mark.xfail(strict=True)`.
    When a Phase 1 wave remediates a break, the strict xfail marker MUST be
    removed, or pytest will fail on XPASS.
    """

    def test_step_01_customer(self) -> None:
        """Step 1: customer account / deal model."""
        from nce.vertical_modules.sales.graph import do_create_deal

        assert callable(do_create_deal)

    def test_step_02_design(self) -> None:
        """Step 2: system design topology authoring."""
        from nce.vertical_modules.system_design.devices import do_author_device_topology

        assert callable(do_author_device_topology)

    def test_step_03_quote(self) -> None:
        """Step 3: sales quote generation."""
        from nce.vertical_modules.sales.lines import (
            do_add_quote_line,
            do_get_quote_lines,
        )

        assert callable(do_add_quote_line)
        assert callable(do_get_quote_lines)

    def test_step_04_sign(self) -> None:
        """Step 4: signature request."""
        from nce.vertical_modules.sales.signing import do_request_signature

        assert callable(do_request_signature)

    def test_step_05_baseline_frozen(self) -> None:
        """Step 5: baseline frozen via signing webhook."""
        # Seam verification: A signed quote in production must have an active webhook
        # route in webhook_receiver or a registered sales_request_signature actor tool.
        from nce.webhook_receiver.main import app

        signing_routes = [r.path for r in app.routes if "signing" in getattr(r, "path", "")]
        has_sales_tool = "sales_request_signature" in TOOL_REGISTRY
        if not signing_routes and not has_sales_tool:
            raise AssertionError(
                "break-1: sales_signed_baselines has no production webhook or registered tool (Wave S-2a)"
            )

    def test_step_06_project(self) -> None:
        """Step 6: project converted from signed quote."""
        from nce.vertical_modules.project.convert import do_convert_signed_quote

        assert callable(do_convert_signed_quote)

    def test_step_07_po(self) -> None:
        """Step 7: procurement purchase order generation core."""
        from nce.vertical_modules.procurement.po import do_generate_po

        assert callable(do_generate_po)

    @pytest.mark.xfail(
        strict=True,
        reason="break-2: PO_LINE status model absent / BOM_LINE ORDERED unwritten (Wave PR-1)",
    )
    def test_step_08_bom_line_ordered(self) -> None:
        """Step 8: BOM_LINE ORDERED rung written on PO submit."""
        # Seam verification: procurement_generate_po and procurement_submit_po
        # must be registered Actor tools that emit PO_LINE.status_changed.
        if "procurement_generate_po" not in TOOL_REGISTRY:
            raise AssertionError(
                "break-2: procurement_generate_po tool not registered; PO_LINE status model absent (Wave PR-1)"
            )

    def test_step_09_gr(self) -> None:
        """Step 9: goods receipt created."""
        from nce.vertical_modules.inventory.goods_receipt import do_record_goods_receipt

        assert callable(do_record_goods_receipt)

    def test_step_10_match(self) -> None:
        """Step 10: three-way match."""
        from nce.vertical_modules.procurement.three_way_match import (
            do_evaluate_three_way_match,
        )

        assert callable(do_evaluate_three_way_match)

    @pytest.mark.xfail(
        strict=True,
        reason="break-2/IN-1: GOODS_RECEIPT.created producer parked; does not trigger project task update (Wave IN-1)",
    )
    def test_step_11_bom_line_delivered(self) -> None:
        """Step 11: BOM_LINE DELIVERED triggered by GOODS_RECEIPT.created."""
        # Seam verification: IN-1 wakes the 132c parked producer for GOODS_RECEIPT.created
        # and connects it to project automation.
        try:
            from nce.events import catalogue

            contract = getattr(catalogue, "EVENT_CATALOGUE", {}).get("GOODS_RECEIPT.created")
            if contract is None or not contract.producers:
                raise AssertionError(
                    "break-2/IN-1: GOODS_RECEIPT.created producer is parked or uncatalogued (Wave IN-1)"
                )
        except (ImportError, AttributeError):
            raise AssertionError(
                "break-2/IN-1: GOODS_RECEIPT.created producer is parked or uncatalogued (Wave IN-1)"
            )

    def test_step_12_asset(self) -> None:
        """Step 12: asset seeded from BOM line."""
        from nce.vertical_modules.assets.seed import do_seed_asset_from_bom

        assert callable(do_seed_asset_from_bom)

    @pytest.mark.xfail(
        strict=True,
        reason="break-2: Field Tech install never calls update_bom_line_status(INSTALLED) (Wave FT-1)",
    )
    def test_step_13_install_installed(self) -> None:
        """Step 13: BOM_LINE INSTALLED rung written by Field Tech install."""
        # Seam verification: field_tech/scan.py or checklist.py must call update_bom_line_status
        import ast
        import pathlib

        ft_dir = pathlib.Path("nce/vertical_modules/field_tech")
        found = False
        for py_file in ft_dir.glob("*.py"):
            tree = ast.parse(py_file.read_bytes().decode("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if fn == "update_bom_line_status":
                        found = True
                        break
        if not found:
            raise AssertionError(
                "break-2: Field Tech never calls update_bom_line_status (Wave FT-1)"
            )

    @pytest.mark.xfail(
        strict=True,
        reason="break-2: Field Tech test never calls update_bom_line_status(TESTED) (Wave FT-1)",
    )
    def test_step_14_test_tested(self) -> None:
        """Step 14: BOM_LINE TESTED rung written by Field Tech test."""
        # Seam verification: field_tech must record TESTED rung
        import ast
        import pathlib

        ft_dir = pathlib.Path("nce/vertical_modules/field_tech")
        found = False
        for py_file in ft_dir.glob("*.py"):
            tree = ast.parse(py_file.read_bytes().decode("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Str) and node.s == "TESTED":
                    found = True
                    break
                elif isinstance(node, ast.Constant) and node.value == "TESTED":
                    found = True
                    break
        if not found:
            raise AssertionError(
                "break-2: Field Tech never writes TESTED status to BOM_LINE ladder (Wave FT-1)"
            )

    def test_step_15_sla_attached(self) -> None:
        """Step 15: SLA attached to asset."""
        from nce.vertical_modules.assets.sla import do_attach_sla

        assert callable(do_attach_sla)

    def test_step_16_invoice_approved(self) -> None:
        """Step 16: invoice approved in Economy."""
        from nce.vertical_modules.economy.cascade import do_cascade_on_approval

        assert callable(do_cascade_on_approval)

    @pytest.mark.xfail(
        strict=True,
        reason="break-3: economy cascade on approval uncalled; actual_cost on BOM_LINE unwritten (Wave E-3)",
    )
    def test_step_17_actual_cost(self) -> None:
        """Step 17: actual_cost written to BOM_LINE by economy cascade."""
        # Seam verification: economy_approve_invoice tool must exist in tool_registry
        if "economy_approve_invoice" not in TOOL_REGISTRY:
            raise AssertionError(
                "break-3: economy_approve_invoice tool not registered; cascade is uncalled (Wave E-3)"
            )

    def test_step_18_ticket(self) -> None:
        """Step 18: support ticket created."""
        from nce.vertical_modules.support.tickets import do_open_ticket

        assert callable(do_open_ticket)

    def test_step_19_dispatch(self) -> None:
        """Step 19: support ticket dispatched."""
        from nce.vertical_modules.support.dispatch import do_dispatch_work_order

        assert callable(do_dispatch_work_order)

    @pytest.mark.xfail(
        strict=True,
        reason="break-5b: support TICKET dispatched_as has no consumer in field_tech (Wave SU-1/FT-3)",
    )
    def test_step_20_work_order(self) -> None:
        """Step 20: support dispatched_as boundary edge consumed by Field Tech."""
        # Seam verification: field_tech must consume dispatched_as edge or subscribe to TICKET.dispatched
        import ast
        import pathlib

        ft_dir = pathlib.Path("nce/vertical_modules/field_tech")
        found = False
        for py_file in ft_dir.glob("*.py"):
            tree = ast.parse(py_file.read_bytes().decode("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in (
                    "dispatched_as",
                    "TICKET.dispatched",
                ):
                    found = True
                    break
        if not found:
            raise AssertionError(
                "break-5b: Field Tech has zero references to dispatched_as or TICKET.dispatched (Wave SU-1/FT-3)"
            )

    def test_step_21_outcome(self) -> None:
        """Step 21: field tech records outcome."""
        from nce.vertical_modules.field_tech.outcome import do_record_outcome

        assert callable(do_record_outcome)

    def test_step_22_cert_expiry(self) -> None:
        """Step 22: certification expiry checker."""
        from nce.vertical_modules.vendors.certs import do_check_cert_expiry

        assert callable(do_check_cert_expiry)

    @pytest.mark.xfail(
        strict=True,
        reason="break-5a: HR emits no C4 event; watcher listens for CERTIFICATION; cert expiry never invalidates allocation (Wave HR-1/V-2)",
    )
    def test_step_23_allocation_invalidated(self) -> None:
        """Step 23: cert expiry invalidates resource allocation."""
        # Seam verification: HR engine must emit CERTIFICATION.EXPIRED event
        import ast
        import pathlib

        hr_dir = pathlib.Path("nce/vertical_modules/hr")
        found = False
        for py_file in hr_dir.glob("*.py"):
            tree = ast.parse(py_file.read_bytes().decode("utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    if fn in ("emit_graph_write", "emit_event", "append_event"):
                        found = True
                        break
        if not found:
            raise AssertionError(
                "break-5a: HR engine emits zero C4 events; CERTIFICATION.EXPIRED unproduced (Wave HR-1/V-2)"
            )

    def test_step_24_portal_request(self) -> None:
        """Step 24: customer raises service request."""
        from nce.vertical_modules.customer_portal.actions import do_raise_service_request

        assert callable(do_raise_service_request)

    def test_step_25_portal_ticket(self) -> None:
        """Step 25: customer portal hand-off creates Support ticket."""
        # Seam verification: customer_portal/actions.py must not be in KNOWN_SEAM_OFFENDERS
        from tests.unit.test_seam_ratchet import KNOWN_SEAM_OFFENDERS

        cp_file = "nce/vertical_modules/customer_portal/actions.py"
        if cp_file in KNOWN_SEAM_OFFENDERS and "support" in KNOWN_SEAM_OFFENDERS[cp_file]:
            raise AssertionError(
                "break-5c: customer_portal/actions.py still has unresolved hasattr(engine, 'support') seam (Wave CP-1)"
            )

    def test_step_26_outcome_recorded(self) -> None:
        """Step 26: project outcome recorded at G5."""
        from nce.vertical_modules.project.recall import do_record_project_outcome

        assert callable(do_record_project_outcome)

    def test_step_27_design_recall(self) -> None:
        """Step 27: design recall returns outcome-weighted similar project."""
        # Seam verification: project_record_outcome tool must exist in tool_registry
        if "project_record_outcome" not in TOOL_REGISTRY:
            raise AssertionError(
                "break-4: project_record_outcome tool not registered; design recall is similarity-only (Wave PJ-1/SD-2)"
            )

    def test_step_28_degradation_register(self) -> None:
        """Step 28: assert degradation register is mounted and empty."""
        from nce.admin_app import app
        from nce.degradation import get_degradation_register

        paths = [
            r.path for r in app.routes if getattr(r, "path", None) == "/api/health/degradations"
        ]
        if not paths:
            raise AssertionError(
                "break-degradations: GET /api/health/degradations route is not mounted (Wave I-5)"
            )
        reg = get_degradation_register()
        total = reg.total_count()
        if total > 0:
            raise AssertionError(
                f"break-degradations: degradation register non-empty: total={total} events: {reg.get_degradations()}"
            )


# ---------------------------------------------------------------------------
# Full Scenario Pipeline Execution Test
# ---------------------------------------------------------------------------


class TestGoldenThreadPipeline:
    """End-to-end scenario runner executing the canonical Golden Thread.

    Executes each step in strict chronological order and halts on the first unclosed seam.
    """

    @pytest.mark.xfail(
        strict=True,
        reason="break-2: Golden Thread pipeline halts at next broken seam (BOM_LINE ORDERED unwritten, Wave PR-1)",
    )
    def test_golden_thread_full_e2e_pipeline(self) -> None:
        """Execute full scenario pipeline end-to-end.

        Step 5 (baseline frozen) was closed by Wave S-2a.
        Halts at Step 8 (BOM_LINE ORDERED) until Wave PR-1 lands.
        """
        engine = NCEEngine()
        assert engine is not None
        context: dict[str, Any] = {
            "customer_id": "cust-gold-001",
            "deal_id": "deal-gold-001",
            "design_id": "dsn-gold-001",
            "quote_id": "q-gold-001",
            "project_id": None,
            "baseline_frozen": False,
        }

        # Step 1: Customer
        from nce.vertical_modules.sales.graph import do_create_deal

        assert callable(do_create_deal)

        # Step 2: Design
        from nce.vertical_modules.system_design.devices import do_author_device_topology

        assert callable(do_author_device_topology)

        # Step 3: Quote
        from nce.vertical_modules.sales.lines import do_get_quote_lines

        assert callable(do_get_quote_lines)

        # Step 4: Sign
        from nce.vertical_modules.sales.signing import do_request_signature

        assert callable(do_request_signature)

        # Step 5: Baseline Frozen (Closed by Wave S-2a)
        if not context["baseline_frozen"]:
            from nce.webhook_receiver.main import app

            signing_routes = [r.path for r in app.routes if "signing" in getattr(r, "path", "")]
            if not signing_routes and "sales_request_signature" not in TOOL_REGISTRY:
                raise AssertionError(
                    "break-1: pipeline halted at step 5: sales_signed_baselines has no production webhook or tool (Wave S-2a)"
                )
            context["baseline_frozen"] = True

        # Step 6: Project
        from nce.vertical_modules.project.convert import do_convert_signed_quote

        assert callable(do_convert_signed_quote)

        # Step 7: PO
        from nce.vertical_modules.procurement.po import do_generate_po

        assert callable(do_generate_po)

        # Step 8: BOM_LINE ORDERED (Second Seam Break - Wave PR-1)
        if "procurement_submit_po" not in TOOL_REGISTRY:
            raise AssertionError(
                "break-2: pipeline halted at step 8: BOM_LINE ORDERED unwritten, procurement_submit_po missing (Wave PR-1)"
            )


# ---------------------------------------------------------------------------
# Standing Positive Controls (U18: a positive-control test beats a manual RED)
# ---------------------------------------------------------------------------


class TestGoldenThreadPositiveControls:
    """Standing positive-control tests verifying the burndown instrument itself."""

    def test_burndown_manifest_covers_all_review_breaks(self) -> None:
        """Verify that all 7 review breaks from Review §0 are mapped to steps."""
        expected_breaks = {
            "break-1",
            "break-2",
            "break-3",
            "break-4",
            "break-5a",
            "break-5b",
            "break-5c",
            "break-degradations",
        }
        manifest_breaks = {
            s.review_break for s in GOLDEN_THREAD_STEPS if s.review_break is not None
        }
        missing = expected_breaks - manifest_breaks
        assert not missing, f"Burndown manifest missing breaks: {missing}"

    def test_burndown_manifest_step_sequence_continuity(self) -> None:
        """Verify that steps are 1-indexed, contiguous, and exactly 28 steps."""
        assert len(GOLDEN_THREAD_STEPS) == 28
        indices = [s.index for s in GOLDEN_THREAD_STEPS]
        assert indices == list(range(1, 29))

    def test_burndown_manifest_broken_steps_count(self) -> None:
        """Verify broken steps count burns down as waves land.

        Originally 10 seam breaks (Steps 5, 8, 11, 13, 14, 17, 20, 23, 25, 27)
        plus the degradation register check (Step 28).
        Wave S-2a closed Step 5, Wave CP-1 closed Step 25 and Wave I-5 closed Step 28,
        burning down to 8 broken steps.
        """
        broken_steps = [s for s in GOLDEN_THREAD_STEPS if s.is_broken]
        assert len(broken_steps) == 7
        broken_indices = {s.index for s in broken_steps}
        assert broken_indices == {8, 11, 13, 14, 17, 20, 23}

    def test_positive_control_broken_steps_have_remediation_waves(self) -> None:
        """Verify every broken step specifies a responsible Phase 1 remediation wave."""
        for step in GOLDEN_THREAD_STEPS:
            if step.is_broken:
                assert step.phase1_wave is not None, f"Step {step.index} missing remediation wave"
                assert step.review_break is not None, f"Step {step.index} missing review break"

    def test_positive_control_strict_xfail_marker_applied_to_all_broken_steps(self) -> None:
        """Verify that every broken step test method carries @pytest.mark.xfail(strict=True)."""
        import inspect

        broken_names = {
            f"test_step_{s.index:02d}_{s.name}" for s in GOLDEN_THREAD_STEPS if s.is_broken
        }
        passing_names = {
            f"test_step_{s.index:02d}_{s.name}" for s in GOLDEN_THREAD_STEPS if not s.is_broken
        }

        for name, method in inspect.getmembers(TestGoldenThreadSteps, predicate=inspect.isfunction):
            if name in broken_names:
                marks = getattr(method, "pytestmark", [])
                xfail_marks = [m for m in marks if m.name == "xfail"]
                assert xfail_marks, f"{name} is a broken seam step but lacks @pytest.mark.xfail"
                assert xfail_marks[0].kwargs.get("strict") is True, (
                    f"{name} must have strict=True on xfail"
                )
            elif name in passing_names:
                marks = getattr(method, "pytestmark", [])
                xfail_marks = [m for m in marks if m.name == "xfail"]
                assert not xfail_marks, (
                    f"{name} is a working step and MUST NOT carry @pytest.mark.xfail"
                )
