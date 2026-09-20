> **Status:** shipped · **Verified-against:** 80cb82c (main) · **Last-audited:** generated

# Golden Thread — Seam Burndown

> **Status:** shipped · **Verified-against:** 80cb82c (main) · **Last-audited:** generated

**This page is generated from `tests/integration/test_golden_thread.py` — it cannot go stale.** A step is OPEN here if and only if its test carries `@pytest.mark.xfail(strict=True, reason="break-N: ...")` in that file; `strict=True` means the test SUITE fails (XPASS) the moment a seam closes while its marker is still on, forcing the marker's removal in the same commit. Regenerate with:
```
python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD --out docs/_generated/golden_thread_seams.md
```

## Summary — 4 of 37 lifecycle steps broken, 4 distinct seam(s): `break-h9b`, `break-h9c`, `break-h9d`, `break-h9e`

| Step | Seam | Status | Reason (from the test file) |
|---:|---|---|---|
| 1 | Step 1: customer account / deal model. (`test_step_01_customer` — `tests/integration/test_golden_thread.py:973`) | ✅ closed | — |
| 2 | Step 2: system design topology authoring. (`test_step_02_design` — `tests/integration/test_golden_thread.py:998`) | ✅ closed | — |
| 3 | Step 3: sales quote generation. (`test_step_03_quote` — `tests/integration/test_golden_thread.py:1038`) | ✅ closed | — |
| 4 | Step 4: signature request. (`test_step_04_sign` — `tests/integration/test_golden_thread.py:1062`) | ✅ closed | — |
| 5 | Step 5: baseline frozen via signing webhook. (`test_step_05_baseline_frozen` — `tests/integration/test_golden_thread.py:1115`) | ✅ closed | — |
| 6 | Step 6: project converted from signed quote. (`test_step_06_project` — `tests/integration/test_golden_thread.py:1135`) | ✅ closed | — |
| 7 | Step 7: purchase order generated from BOM. (`test_step_07_po` — `tests/integration/test_golden_thread.py:1162`) | ✅ closed | — |
| 8 | Step 8: BOM_LINE ORDERED rung written. (`test_step_08_ordered` — `tests/integration/test_golden_thread.py:1190`) | ✅ closed | — |
| 9 | Step 9: goods receipt created. (`test_step_09_gr` — `tests/integration/test_golden_thread.py:1213`) | ✅ closed | — |
| 10 | Step 10: three-way match executed. (`test_step_10_match` — `tests/integration/test_golden_thread.py:1242`) | ✅ closed | — |
| 11 | Step 11: BOM_LINE DELIVERED rung written. (`test_step_11_delivered` — `tests/integration/test_golden_thread.py:1260`) | ✅ closed | — |
| 12 | Step 12: asset seeded from BOM line. (`test_step_12_asset` — `tests/integration/test_golden_thread.py:1283`) | ✅ closed | — |
| 13 | Step 13: BOM_LINE INSTALLED rung written. (`test_step_13_installed` — `tests/integration/test_golden_thread.py:1305`) | ✅ closed | — |
| 14 | Step 14: BOM_LINE TESTED rung written. (`test_step_14_tested` — `tests/integration/test_golden_thread.py:1328`) | ✅ closed | — |
| 15 | Step 15: SLA coverage attached to asset. (`test_step_15_sla_attached` — `tests/integration/test_golden_thread.py:1351`) | ✅ closed | — |
| 16 | Step 16: supplier invoice approved. (`test_step_16_invoice_approved` — `tests/integration/test_golden_thread.py:1382`) | ✅ closed | — |
| 17 | Step 17: actual_cost written to BOM_LINE. (`test_step_17_actual_cost` — `tests/integration/test_golden_thread.py:1405`) | ✅ closed | — |
| 18 | Step 18: support ticket created. (`test_step_18_ticket` — `tests/integration/test_golden_thread.py:1419`) | ✅ closed | — |
| 19 | Step 19: work order dispatched. (`test_step_19_dispatch` — `tests/integration/test_golden_thread.py:1442`) | ✅ closed | — |
| 20 | Step 20: field tech work order created. (`test_step_20_work_order` — `tests/integration/test_golden_thread.py:1465`) | ✅ closed | — |
| 21 | Step 21: work order outcome recorded. (`test_step_21_outcome` — `tests/integration/test_golden_thread.py:1488`) | ✅ closed | — |
| 22 | Step 22: technician certification expiry evaluated. (`test_step_22_cert_expiry` — `tests/integration/test_golden_thread.py:1520`) | ✅ closed | — |
| 23 | Step 23: certification expiry invalidates scheduled allocation. (`test_step_23_allocation_invalidated` — `tests/integration/test_golden_thread.py:1556`) | ✅ closed | — |
| 24 | Step 24: customer raises portal request. (`test_step_24_portal_request` — `tests/integration/test_golden_thread.py:1611`) | ✅ closed | — |
| 25 | Step 25: customer portal creates support ticket. (`test_step_25_portal_ticket` — `tests/integration/test_golden_thread.py:1626`) | ✅ closed | — |
| 26 | Step 26: project outcome recorded at G5 phase gate. (`test_step_26_outcome_recorded` — `tests/integration/test_golden_thread.py:1639`) | ✅ closed | — |
| 27 | Step 27: design recall returns outcome-weighted similar project. (`test_step_27_design_recall` — `tests/integration/test_golden_thread.py:1678`) | ✅ closed | — |
| 28 | Step 28: degradation register reports zero active degradations. (`test_step_28_degradations` — `tests/integration/test_golden_thread.py:1703`) | ✅ closed | — |
| 29 | Step 29: C17 SITE enriched with address/coordinates (F-9). (`test_step_29_site_address_enriched` — `tests/integration/test_golden_thread.py:1716`) | ✅ closed | — |
| 30 | Step 30: C-1 FL tree -- read children/ancestors, then move a node. (`test_step_30_fl_tree_children_ancestors_move` — `tests/integration/test_golden_thread.py:1776`) | ✅ closed | — |
| 31 | Step 31: C-1 FL tree -- merge two duplicate room records. (`test_step_31_fl_tree_merge` — `tests/integration/test_golden_thread.py:1821`) | ✅ closed | — |
| 32 | Step 32: C-2 room category set, an employee assigned responsible. (`test_step_32_responsible_assigned` — `tests/integration/test_golden_thread.py:1866`) | ✅ closed | — |
| 33 | Step 33: deal with participants (not built -- see xfail reason). (`test_step_33_deal_participants` — `tests/integration/test_golden_thread.py:1929`) | 🔴 OPEN | break-h9b: no DEAL_PARTICIPANT concept exists anywhere in nce.vertical_modules.sales (confirmed by reading resources.py/graph.py and migration 088_sales_resource_tables.sql) -- do_create_deal takes no participants/attendees argument at all. Aspirational per the original H-9 charter text, not yet built by any landed wave (tracked as B-1-followup). |
| 34 | Step 34: agreement with parties, via the C12 resource-surface tools. (`test_step_34_agreement_parties` — `tests/integration/test_golden_thread.py:1955`) | 🔴 OPEN | break-h9c (re-diagnosed): the resource_surface datetime/version_field bug this was originally blocked on merged to main in #284 -- confirmed fixed by live re-run. That re-run surfaced the real, distinct blocker: asyncpg.exceptions.ForeignKeyViolationError on agreements_customer_id_fkey. ctx.customer_id is a synthetic UUID that only ever exists as a kg_nodes graph node (entity_type='CUSTOMER', written by do_create_deal in step 1) -- no step anywhere in this file, and no code path anywhere in nce/, ever creates a matching row in the C12-relational sales_customers table. 'agreements' hard-FKs to that table, so any real customer_id must exist there first. This is an architecture split (graph-modeled customer identity vs. C12-relational customer identity) with no bridge between them, not a test bug and not fixed here. |
| 35 | Step 35: billing run (not built -- see xfail reason). (`test_step_35_billing_run` — `tests/integration/test_golden_thread.py:1994`) | 🔴 OPEN | break-h9d: no billing-run capability exists anywhere in the tree -- git ls-tree origin/main -- nce/migrations/ has no billing_run migration, and no vertical_modules package implements one. Tracked charter-wide as Wave B-12, not yet started as of this wave. |
| 36 | Step 36: customer invoice (not built -- see xfail reason). (`test_step_36_customer_invoice` — `tests/integration/test_golden_thread.py:2009`) | 🔴 OPEN | break-h9e: no customer-invoice capability exists anywhere in the tree (distinct from the existing supplier-invoice approval path economy_approve_invoice already covers). Depends on billing_run (step 35) existing first; tracked under the same Wave B-12. |
| 37 | Step 37: C13 notification delivered to the responsible employee. (`test_step_37_notification_received` — `tests/integration/test_golden_thread.py:2015`) | ✅ closed | — |

**33 steps carry no `xfail` marker at all — their seam is proven live** by the test asserting the real production call path, not a mock.
