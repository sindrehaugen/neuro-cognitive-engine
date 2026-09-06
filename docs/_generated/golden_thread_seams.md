> **Status:** shipped · **Verified-against:** 1e402e1 (main) · **Last-audited:** generated

# Golden Thread — Seam Burndown

> **Status:** shipped · **Verified-against:** 1e402e1 (main) · **Last-audited:** generated

**This page is generated from `tests/integration/test_golden_thread.py` — it cannot go stale.** A step is OPEN here if and only if its test carries `@pytest.mark.xfail(strict=True, reason="break-N: ...")` in that file; `strict=True` means the test SUITE fails (XPASS) the moment a seam closes while its marker is still on, forcing the marker's removal in the same commit. Regenerate with:
```
python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD --out docs/_generated/golden_thread_seams.md
```

## Summary — 8 of 28 lifecycle steps broken, 5 distinct seam(s): `break-2`, `break-3`, `break-4`, `break-5a`, `break-5b`

| Step | Seam | Status | Reason (from the test file) |
|---:|---|---|---|
| 1 | Step 1: customer account / deal model. (`test_step_01_customer` — `tests/integration/test_golden_thread.py:317`) | ✅ closed | — |
| 2 | Step 2: system design topology authoring. (`test_step_02_design` — `tests/integration/test_golden_thread.py:323`) | ✅ closed | — |
| 3 | Step 3: sales quote generation. (`test_step_03_quote` — `tests/integration/test_golden_thread.py:329`) | ✅ closed | — |
| 4 | Step 4: signature request. (`test_step_04_sign` — `tests/integration/test_golden_thread.py:339`) | ✅ closed | — |
| 5 | Step 5: baseline frozen via signing webhook. (`test_step_05_baseline_frozen` — `tests/integration/test_golden_thread.py:345`) | ✅ closed | — |
| 6 | Step 6: project converted from signed quote. (`test_step_06_project` — `tests/integration/test_golden_thread.py:358`) | ✅ closed | — |
| 7 | Step 7: procurement purchase order generation core. (`test_step_07_po` — `tests/integration/test_golden_thread.py:364`) | ✅ closed | — |
| 8 | Step 8: BOM_LINE ORDERED rung written on PO submit. (`test_step_08_bom_line_ordered` — `tests/integration/test_golden_thread.py:374`) | 🔴 OPEN | break-2: PO_LINE status model absent / BOM_LINE ORDERED unwritten (Wave PR-1) |
| 9 | Step 9: goods receipt created. (`test_step_09_gr` — `tests/integration/test_golden_thread.py:383`) | ✅ closed | — |
| 10 | Step 10: three-way match. (`test_step_10_match` — `tests/integration/test_golden_thread.py:389`) | ✅ closed | — |
| 11 | Step 11: BOM_LINE DELIVERED triggered by GOODS_RECEIPT.created. (`test_step_11_bom_line_delivered` — `tests/integration/test_golden_thread.py:401`) | 🔴 OPEN | break-2/IN-1: GOODS_RECEIPT.created producer parked; does not trigger project task update (Wave IN-1) |
| 12 | Step 12: asset seeded from BOM line. (`test_step_12_asset` — `tests/integration/test_golden_thread.py:418`) | ✅ closed | — |
| 13 | Step 13: BOM_LINE INSTALLED rung written by Field Tech install. (`test_step_13_install_installed` — `tests/integration/test_golden_thread.py:428`) | 🔴 OPEN | break-2: Field Tech install never calls update_bom_line_status(INSTALLED) (Wave FT-1) |
| 14 | Step 14: BOM_LINE TESTED rung written by Field Tech test. (`test_step_14_test_tested` — `tests/integration/test_golden_thread.py:453`) | 🔴 OPEN | break-2: Field Tech test never calls update_bom_line_status(TESTED) (Wave FT-1) |
| 15 | Step 15: SLA attached to asset. (`test_step_15_sla_attached` — `tests/integration/test_golden_thread.py:475`) | ✅ closed | — |
| 16 | Step 16: invoice approved in Economy. (`test_step_16_invoice_approved` — `tests/integration/test_golden_thread.py:481`) | ✅ closed | — |
| 17 | Step 17: actual_cost written to BOM_LINE by economy cascade. (`test_step_17_actual_cost` — `tests/integration/test_golden_thread.py:491`) | 🔴 OPEN | break-3: economy cascade on approval uncalled; actual_cost on BOM_LINE unwritten (Wave E-3) |
| 18 | Step 18: support ticket created. (`test_step_18_ticket` — `tests/integration/test_golden_thread.py:499`) | ✅ closed | — |
| 19 | Step 19: support ticket dispatched. (`test_step_19_dispatch` — `tests/integration/test_golden_thread.py:505`) | ✅ closed | — |
| 20 | Step 20: support dispatched_as boundary edge consumed by Field Tech. (`test_step_20_work_order` — `tests/integration/test_golden_thread.py:515`) | 🔴 OPEN | break-5b: support TICKET dispatched_as has no consumer in field_tech (Wave SU-1/FT-3) |
| 21 | Step 21: field tech records outcome. (`test_step_21_outcome` — `tests/integration/test_golden_thread.py:537`) | ✅ closed | — |
| 22 | Step 22: certification expiry checker. (`test_step_22_cert_expiry` — `tests/integration/test_golden_thread.py:543`) | ✅ closed | — |
| 23 | Step 23: cert expiry invalidates resource allocation. (`test_step_23_allocation_invalidated` — `tests/integration/test_golden_thread.py:553`) | 🔴 OPEN | break-5a: HR emits no C4 event; watcher listens for CERTIFICATION; cert expiry never invalidates allocation (Wave HR-1/V-2) |
| 24 | Step 24: customer raises service request. (`test_step_24_portal_request` — `tests/integration/test_golden_thread.py:574`) | ✅ closed | — |
| 25 | Step 25: customer portal hand-off creates Support ticket. (`test_step_25_portal_ticket` — `tests/integration/test_golden_thread.py:580`) | ✅ closed | — |
| 26 | Step 26: project outcome recorded at G5. (`test_step_26_outcome_recorded` — `tests/integration/test_golden_thread.py:591`) | ✅ closed | — |
| 27 | Step 27: design recall returns outcome-weighted similar project. (`test_step_27_design_recall` — `tests/integration/test_golden_thread.py:601`) | 🔴 OPEN | break-4: project outcome attribution and design recall similarity-only / unwired (Wave PJ-1/SD-2) |
| 28 | Step 28: assert degradation register is mounted and empty. (`test_step_28_degradation_register` — `tests/integration/test_golden_thread.py:609`) | ✅ closed | — |

**20 steps carry no `xfail` marker at all — their seam is proven live** by the test asserting the real production call path, not a mock.
