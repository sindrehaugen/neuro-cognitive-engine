> **Status:** shipped · **Verified-against:** 453268f (main) · **Last-audited:** generated

# Golden Thread — Seam Burndown

> **Status:** shipped · **Verified-against:** 453268f (main) · **Last-audited:** generated

**This page is generated from `tests/integration/test_golden_thread.py` — it cannot go stale.** A step is OPEN here if and only if its test carries `@pytest.mark.xfail(strict=True, reason="break-N: ...")` in that file; `strict=True` means the test SUITE fails (XPASS) the moment a seam closes while its marker is still on, forcing the marker's removal in the same commit. Regenerate with:
```
python scripts/gen_golden_thread_seams.py --repo . --baseline HEAD --out docs/_generated/golden_thread_seams.md
```

## Summary — 5 of 37 lifecycle steps broken, 5 distinct seam(s): `break-h9a`, `break-h9b`, `break-h9c`, `break-h9d`, `break-h9e`

| Step | Seam | Status | Reason (from the test file) |
|---:|---|---|---|
| 1 | Step 1: customer account / deal model. (`test_step_01_customer` — `tests/integration/test_golden_thread.py:923`) | ✅ closed | — |
| 2 | Step 2: system design topology authoring. (`test_step_02_design` — `tests/integration/test_golden_thread.py:948`) | ✅ closed | — |
| 3 | Step 3: sales quote generation. (`test_step_03_quote` — `tests/integration/test_golden_thread.py:988`) | ✅ closed | — |
| 4 | Step 4: signature request. (`test_step_04_sign` — `tests/integration/test_golden_thread.py:1012`) | ✅ closed | — |
| 5 | Step 5: baseline frozen via signing webhook. (`test_step_05_baseline_frozen` — `tests/integration/test_golden_thread.py:1065`) | ✅ closed | — |
| 6 | Step 6: project converted from signed quote. (`test_step_06_project` — `tests/integration/test_golden_thread.py:1085`) | ✅ closed | — |
| 7 | Step 7: purchase order generated from BOM. (`test_step_07_po` — `tests/integration/test_golden_thread.py:1112`) | ✅ closed | — |
| 8 | Step 8: BOM_LINE ORDERED rung written. (`test_step_08_ordered` — `tests/integration/test_golden_thread.py:1140`) | ✅ closed | — |
| 9 | Step 9: goods receipt created. (`test_step_09_gr` — `tests/integration/test_golden_thread.py:1163`) | ✅ closed | — |
| 10 | Step 10: three-way match executed. (`test_step_10_match` — `tests/integration/test_golden_thread.py:1192`) | ✅ closed | — |
| 11 | Step 11: BOM_LINE DELIVERED rung written. (`test_step_11_delivered` — `tests/integration/test_golden_thread.py:1210`) | ✅ closed | — |
| 12 | Step 12: asset seeded from BOM line. (`test_step_12_asset` — `tests/integration/test_golden_thread.py:1233`) | ✅ closed | — |
| 13 | Step 13: BOM_LINE INSTALLED rung written. (`test_step_13_installed` — `tests/integration/test_golden_thread.py:1255`) | ✅ closed | — |
| 14 | Step 14: BOM_LINE TESTED rung written. (`test_step_14_tested` — `tests/integration/test_golden_thread.py:1278`) | ✅ closed | — |
| 15 | Step 15: SLA coverage attached to asset. (`test_step_15_sla_attached` — `tests/integration/test_golden_thread.py:1301`) | ✅ closed | — |
| 16 | Step 16: supplier invoice approved. (`test_step_16_invoice_approved` — `tests/integration/test_golden_thread.py:1332`) | ✅ closed | — |
| 17 | Step 17: actual_cost written to BOM_LINE. (`test_step_17_actual_cost` — `tests/integration/test_golden_thread.py:1355`) | ✅ closed | — |
| 18 | Step 18: support ticket created. (`test_step_18_ticket` — `tests/integration/test_golden_thread.py:1369`) | ✅ closed | — |
| 19 | Step 19: work order dispatched. (`test_step_19_dispatch` — `tests/integration/test_golden_thread.py:1392`) | ✅ closed | — |
| 20 | Step 20: field tech work order created. (`test_step_20_work_order` — `tests/integration/test_golden_thread.py:1415`) | ✅ closed | — |
| 21 | Step 21: work order outcome recorded. (`test_step_21_outcome` — `tests/integration/test_golden_thread.py:1438`) | ✅ closed | — |
| 22 | Step 22: technician certification expiry evaluated. (`test_step_22_cert_expiry` — `tests/integration/test_golden_thread.py:1470`) | ✅ closed | — |
| 23 | Step 23: certification expiry invalidates scheduled allocation. (`test_step_23_allocation_invalidated` — `tests/integration/test_golden_thread.py:1506`) | ✅ closed | — |
| 24 | Step 24: customer raises portal request. (`test_step_24_portal_request` — `tests/integration/test_golden_thread.py:1561`) | ✅ closed | — |
| 25 | Step 25: customer portal creates support ticket. (`test_step_25_portal_ticket` — `tests/integration/test_golden_thread.py:1576`) | ✅ closed | — |
| 26 | Step 26: project outcome recorded at G5 phase gate. (`test_step_26_outcome_recorded` — `tests/integration/test_golden_thread.py:1589`) | ✅ closed | — |
| 27 | Step 27: design recall returns outcome-weighted similar project. (`test_step_27_design_recall` — `tests/integration/test_golden_thread.py:1628`) | ✅ closed | — |
| 28 | Step 28: degradation register reports zero active degradations. (`test_step_28_degradations` — `tests/integration/test_golden_thread.py:1653`) | ✅ closed | — |
| 29 | Step 29: C17 SITE enriched with address/coordinates (F-9). (`test_step_29_site_address_enriched` — `tests/integration/test_golden_thread.py:1666`) | ✅ closed | — |
| 30 | Step 30: C-1 FL tree -- read children/ancestors, then move a node. (`test_step_30_fl_tree_children_ancestors_move` — `tests/integration/test_golden_thread.py:1726`) | ✅ closed | — |
| 31 | Step 31: C-1 FL tree -- merge two duplicate room records. (`test_step_31_fl_tree_merge` — `tests/integration/test_golden_thread.py:1786`) | 🔴 OPEN | break-h9a: merge_fl_nodes sets change_origin='merged' on the absorbed node (fl_tree.py:608 real-Postgres path, :635 in-memory), which is not in the real kg_nodes_change_origin_chk constraint ('sync','webhook','agent','operator','consolidation','replay','unknown'). Every real-Postgres merge fails with CheckViolationError. Ruling (ML-orch, 2026-09-19): the correct value is 'consolidation', not 'operator' -- 'operator' would silently mark the absorbed node as_built (fl_tree.py:102), which is true for a human-moved location but not for two duplicate records being consolidated. Fix dispatched to Lane C; this seam stays xfail until it lands on main, not removed on the strength of the ruling alone. |
| 32 | Step 32: C-2 room category set, an employee assigned responsible. (`test_step_32_responsible_assigned` — `tests/integration/test_golden_thread.py:1809`) | ✅ closed | — |
| 33 | Step 33: deal with participants (not built -- see xfail reason). (`test_step_33_deal_participants` — `tests/integration/test_golden_thread.py:1872`) | 🔴 OPEN | break-h9b: no DEAL_PARTICIPANT concept exists anywhere in nce.vertical_modules.sales (confirmed by reading resources.py/graph.py and migration 088_sales_resource_tables.sql) -- do_create_deal takes no participants/attendees argument at all. Aspirational per the original H-9 charter text, not yet built by any landed wave (tracked as B-1-followup). |
| 34 | Step 34: agreement with parties, via the C12 resource-surface tools. (`test_step_34_agreement_parties` — `tests/integration/test_golden_thread.py:1898`) | 🔴 OPEN | break-h9c (re-diagnosed): the resource_surface datetime/version_field bug this was originally blocked on merged to main in #284 -- confirmed fixed by live re-run. That re-run surfaced the real, distinct blocker: asyncpg.exceptions.ForeignKeyViolationError on agreements_customer_id_fkey. ctx.customer_id is a synthetic UUID that only ever exists as a kg_nodes graph node (entity_type='CUSTOMER', written by do_create_deal in step 1) -- no step anywhere in this file, and no code path anywhere in nce/, ever creates a matching row in the C12-relational sales_customers table. 'agreements' hard-FKs to that table, so any real customer_id must exist there first. This is an architecture split (graph-modeled customer identity vs. C12-relational customer identity) with no bridge between them, not a test bug and not fixed here. |
| 35 | Step 35: billing run (not built -- see xfail reason). (`test_step_35_billing_run` — `tests/integration/test_golden_thread.py:1937`) | 🔴 OPEN | break-h9d: no billing-run capability exists anywhere in the tree -- git ls-tree origin/main -- nce/migrations/ has no billing_run migration, and no vertical_modules package implements one. Tracked charter-wide as Wave B-12, not yet started as of this wave. |
| 36 | Step 36: customer invoice (not built -- see xfail reason). (`test_step_36_customer_invoice` — `tests/integration/test_golden_thread.py:1952`) | 🔴 OPEN | break-h9e: no customer-invoice capability exists anywhere in the tree (distinct from the existing supplier-invoice approval path economy_approve_invoice already covers). Depends on billing_run (step 35) existing first; tracked under the same Wave B-12. |
| 37 | Step 37: C13 notification delivered to the responsible employee. (`test_step_37_notification_received` — `tests/integration/test_golden_thread.py:1958`) | ✅ closed | — |

**32 steps carry no `xfail` marker at all — their seam is proven live** by the test asserting the real production call path, not a mock.
