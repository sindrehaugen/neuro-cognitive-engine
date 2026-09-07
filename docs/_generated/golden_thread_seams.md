> **Status:** shipped · **Verified-against:** 749cda5 (main) · **Last-audited:** generated

# Golden Thread — Seam Burndown

> **Status:** shipped · **Verified-against:** 749cda5 (main) · **Last-audited:** generated

**This page is generated from `tests/integration/test_golden_thread.py` — it cannot go stale.** A step is OPEN here if and only if its test carries `@pytest.mark.xfail(strict=True, reason="break-N: ...")` in that file; `strict=True` means the test SUITE fails (XPASS) the moment a seam closes while its marker is still on, forcing the marker's removal in the same commit. Regenerate with:
```
python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD --out docs/_generated/golden_thread_seams.md
```

## Summary — 0 of 28 lifecycle steps broken, 0 distinct seam(s): 

| Step | Seam | Status | Reason (from the test file) |
|---:|---|---|---|
| 1 | Step 1: customer account / deal model. (`test_step_01_customer` — `tests/integration/test_golden_thread.py:603`) | ✅ closed | — |
| 2 | Step 2: system design topology authoring. (`test_step_02_design` — `tests/integration/test_golden_thread.py:628`) | ✅ closed | — |
| 3 | Step 3: sales quote generation. (`test_step_03_quote` — `tests/integration/test_golden_thread.py:668`) | ✅ closed | — |
| 4 | Step 4: signature request. (`test_step_04_sign` — `tests/integration/test_golden_thread.py:692`) | ✅ closed | — |
| 5 | Step 5: baseline frozen via signing webhook. (`test_step_05_baseline_frozen` — `tests/integration/test_golden_thread.py:745`) | ✅ closed | — |
| 6 | Step 6: project converted from signed quote. (`test_step_06_project` — `tests/integration/test_golden_thread.py:765`) | ✅ closed | — |
| 7 | Step 7: purchase order generated from BOM. (`test_step_07_po` — `tests/integration/test_golden_thread.py:792`) | ✅ closed | — |
| 8 | Step 8: BOM_LINE ORDERED rung written. (`test_step_08_ordered` — `tests/integration/test_golden_thread.py:820`) | ✅ closed | — |
| 9 | Step 9: goods receipt created. (`test_step_09_gr` — `tests/integration/test_golden_thread.py:843`) | ✅ closed | — |
| 10 | Step 10: three-way match executed. (`test_step_10_match` — `tests/integration/test_golden_thread.py:872`) | ✅ closed | — |
| 11 | Step 11: BOM_LINE DELIVERED rung written. (`test_step_11_delivered` — `tests/integration/test_golden_thread.py:890`) | ✅ closed | — |
| 12 | Step 12: asset seeded from BOM line. (`test_step_12_asset` — `tests/integration/test_golden_thread.py:913`) | ✅ closed | — |
| 13 | Step 13: BOM_LINE INSTALLED rung written. (`test_step_13_installed` — `tests/integration/test_golden_thread.py:935`) | ✅ closed | — |
| 14 | Step 14: BOM_LINE TESTED rung written. (`test_step_14_tested` — `tests/integration/test_golden_thread.py:958`) | ✅ closed | — |
| 15 | Step 15: SLA coverage attached to asset. (`test_step_15_sla_attached` — `tests/integration/test_golden_thread.py:981`) | ✅ closed | — |
| 16 | Step 16: supplier invoice approved. (`test_step_16_invoice_approved` — `tests/integration/test_golden_thread.py:1012`) | ✅ closed | — |
| 17 | Step 17: actual_cost written to BOM_LINE. (`test_step_17_actual_cost` — `tests/integration/test_golden_thread.py:1035`) | ✅ closed | — |
| 18 | Step 18: support ticket created. (`test_step_18_ticket` — `tests/integration/test_golden_thread.py:1049`) | ✅ closed | — |
| 19 | Step 19: work order dispatched. (`test_step_19_dispatch` — `tests/integration/test_golden_thread.py:1072`) | ✅ closed | — |
| 20 | Step 20: field tech work order created. (`test_step_20_work_order` — `tests/integration/test_golden_thread.py:1095`) | ✅ closed | — |
| 21 | Step 21: work order outcome recorded. (`test_step_21_outcome` — `tests/integration/test_golden_thread.py:1118`) | ✅ closed | — |
| 22 | Step 22: technician certification expiry evaluated. (`test_step_22_cert_expiry` — `tests/integration/test_golden_thread.py:1150`) | ✅ closed | — |
| 23 | Step 23: certification expiry invalidates scheduled allocation. (`test_step_23_allocation_invalidated` — `tests/integration/test_golden_thread.py:1186`) | ✅ closed | — |
| 24 | Step 24: customer raises portal request. (`test_step_24_portal_request` — `tests/integration/test_golden_thread.py:1241`) | ✅ closed | — |
| 25 | Step 25: customer portal creates support ticket. (`test_step_25_portal_ticket` — `tests/integration/test_golden_thread.py:1256`) | ✅ closed | — |
| 26 | Step 26: project outcome recorded at G5 phase gate. (`test_step_26_outcome_recorded` — `tests/integration/test_golden_thread.py:1269`) | ✅ closed | — |
| 27 | Step 27: design recall returns outcome-weighted similar project. (`test_step_27_design_recall` — `tests/integration/test_golden_thread.py:1290`) | ✅ closed | — |
| 28 | Step 28: degradation register reports zero active degradations. (`test_step_28_degradations` — `tests/integration/test_golden_thread.py:1315`) | ✅ closed | — |

**28 steps carry no `xfail` marker at all — their seam is proven live** by the test asserting the real production call path, not a mock.
