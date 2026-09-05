> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

# Economy Engine User Guide (Doc 75)

> **Status:** shipped · **Verified-against:** 7304330 (main) · **Last-audited:** 2026-08-17

The **Economy Engine** (`nce/vertical_modules/economy/`) is the financial intelligence and accounting engine of the Neuro-Cognitive Engine (NCE). It lifts the core financial mechanisms from PFT (~17.3k LOC, 80+ test files) onto the NCE knowledge graph spine: the **130-point contextual invoice matcher**, the **7-effect approval cascade** (the sole writer of `BOM_LINE.actual_cost`), **Norwegian GAAP (regnskapsloven §4-1) periodisering**, the **strict zero-drift balance guarantee**, the **margin-trinity** actuals tracking, and the **recurring-revenue** stack.

> [!IMPORTANT]
> **Surface summary (Main @ 7304330):**
> - **3 exposed MCP tools:** `economy_match_invoice`, `economy_compute_periodisering`, `economy_emit_event` (`nce/tool_registry.py:666-684`). All three are read-only Advisor tools (`cacheable: true`, `admin_only: false`, `mutation: false`).
> - **3 mounted REST routes:** `POST /api/economy/match-invoice`, `POST /api/economy/periodisering`, `POST /api/economy/emit-event` (`nce/admin_handlers/economy.py`).
> - **9 internal domain cores (`do_*`):** `do_compute_bucket_targets`, `do_compute_dunning`, `do_compute_recognition_schedule`, `do_emit_financial_event`, `do_forecast_cashflow`, `do_generate_kid`, `do_match_invoice`, `do_snapshot_mrr_arr_churn`, `do_validate_kid`.
> - **Core accounting invariant:** **NCE mirrors and periodises internally; Finago remains the legal General Ledger (GL) system-of-record.** NCE computes the internal numbers (matching, cascade, accruals, projections, balanced double-entry postings) and mirrors the legal book, but does not commit postings directly to Finago's GL in Normal mode.

---

## 1. 130-Point Contextual Invoice Match (`matching.py`)

Implemented in [`matching.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/economy/matching.py). This is a pure domain core (zero DB, zero HTTP, zero web imports) that scores incoming supplier invoices against purchase order (PO) candidate commitments.

### 1.1 Score Scale & Component Breakdown
The scoring algorithm computes a composite score per invoice line against candidate commitments. The historical "130-point" name refers to the classical 100-point match plus the 30-point PFT context bonus. With the addition of the 50-point additive PO-number match bonus (Plan-D), the **true theoretical maximum score is 180 points** (the score is **deliberately NOT clamped** to 130):

| Component | Points | Trigger Condition / Scoring Rule |
|---|:---:|---|
| **PO-Number Exact Match** | `50` | `context["po_number_exact_match"] == True` (additive bonus) |
| **Supplier Identity** | `40` | Exact 9-digit Norwegian organisation number match (`supplier_orgnr`) |
| | `20` | Fuzzy supplier name match (token overlap) |
| | `0` | Unmatched / unknown supplier |
| **Amount Delta** | `30` | Line total within **≤ 2%** of expected candidate amount (`expected_amount <= 0` guarded first -> `0`) |
| | `15` | Line total within **≤ 10%** of expected candidate amount |
| | `0` | Line total delta **> 10%**, or non-positive / missing `expected_amount` |
| **Article Match** | `20` | Exact `article_no` match with BOM line |
| | `10` | Description token overlap ≥ 2 significant words (stopwords filtered) |
| | `0` | No BOM line attached or token overlap < 2 |
| **Project Dimension** | `10` | Explicit `project_id` dimension present on invoice |
| | `5` | Inferred project dimension |
| | `0` | No project dimension |
| **BOM-Line Context** | `15` | BOM line attached, article numbers match, and `bom_line["project_id"]` present |
| | `0` | Otherwise |
| **PO Expected Window** | `10` | `context["po_expected"] == True` (delivery expected in current window) |
| **Supplier Pattern Seen** | `5` | `context["supplier_pattern_seen"] == True` (historical billing frequency pattern) |

### 1.2 Aggregation Rules
An invoice typically carries multiple lines and is evaluated against a pool of candidate commitments:
1. **Per-line Best Match:** Each invoice line is scored against every candidate in the pool. The line takes the score of the highest-scoring candidate (ties preserve the first candidate encountered).
2. **Line Tier Resolution:** Evaluated against resolved thresholds:
   - `score >= green` (default `115`) -> **`GREEN`** (auto-eligible for approval cascade).
   - `score >= yellow` (default `70`) -> **`YELLOW`** (requires manual triage / human review).
   - `score < yellow` -> **`RED`** (failed match / manual dispute).
3. **Conservative Invoice-Level Triage:** The overall invoice tier is the **worst line tier** (`RED > YELLOW > GREEN`). A single RED line turns the entire invoice RED.
4. **Conservative Invoice Score:** The overall invoice score is the **minimum score among lines holding the worst tier**.
5. **Empty Invoice / Empty Pool Edge Cases:** An invoice with no lines returns `{"score": 0, "tier": "RED", "breakdown": []}`. A line with an empty candidate pool is scored against a synthetic empty candidate, allowing header-level signals (supplier, PO number, project) to still earn points.

### 1.3 Thresholds & Config-as-IP
Thresholds are loaded from `nce/config_data/economy-match-thresholds.json`:
- Global defaults: `green = 115`, `yellow = 70`.
- Per-supplier overrides: Keyed by `supplier_orgnr`. Overrides can tighten or loosen the thresholds for specific suppliers without modifying engine code.
- Incoherent configurations (e.g. `green < yellow`, `yellow <= 0`, or non-numeric types) are rejected with `ValueError`.

### 1.4 Procurement Boundary Contract
Each candidate in the pool may carry a `three_way_result` field from Procurement's Receiving 3-way match (`PO × Goods Receipt × Invoice`). The Economy engine **echoes this verdict unchanged** into the breakdown entry; it never recomputes or alters Procurement's goods-receipt verdict. Procurement owns the receiving match (goods vs order); Economy owns the financial match (invoice vs commitment) and is authoritative for posting.

### 1.5 Exposed MCP Tool: `economy_match_invoice`
Registered in `nce/tool_registry.py:666-671`.
- **Properties:** `cacheable: true`, `admin_only: false`, `mutation: false`
- **Request Arguments:**
  - `namespace_id` (`str`, UUID, required)
  - `invoice` (`dict`, required): `{"supplier_orgnr": "...", "project_dimension_present": bool, "lines": [{"article_no": "...", "description": "...", "line_total": float|Decimal}]}`
  - `candidates` (`list[dict]`, optional): `[{"candidate_id": "...", "bom_line": {...}, "context": {...}, "three_way_result": "..."}]`
- **Response Shape:**
```json
{
  "score": 125,
  "tier": "GREEN",
  "breakdown": [
    {
      "line_index": 0,
      "candidate_id": "po-item-9981",
      "candidate_index": 0,
      "total": 125,
      "tier": "GREEN",
      "three_way_result": "MATCHED",
      "components": {
        "scorePoNumberMatch": 0,
        "scoreSupplier": 40,
        "scoreAmount": 30,
        "scoreArticle": 20,
        "scoreProject": 10,
        "scoreBomLine": 15,
        "scoreExpectedFromPo": 10,
        "scoreSupplierPattern": 0
      }
    }
  ]
}
```

### 1.6 Mounted REST Route: `/api/economy/match-invoice`
- **Method & Path:** `POST /api/economy/match-invoice` (`nce/admin_handlers/economy.py:103-145`)
- **Headers:** `Content-Type: application/json`, `Authorization: Bearer <token>`
- **Request Body:** `{"namespace_id": "...", "invoice": {...}, "candidates": [...]}`
- **Response:** `{...result, "status": "ok"}` on 200; `422` on validation failure; `503` if engine disconnected.

---

## 2. NGAAP Periodisering Engine (`ngaap.py`)

Implemented in [`ngaap.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/economy/ngaap.py). This is a pure mathematical accrual engine implementing Norwegian GAAP (regnskapsloven §4-1) for project accounting across period boundaries.

### 2.1 Accounting Principles & Scope
The engine enforces statutory Norwegian accounting principles:
1. **Opptjeningsprinsippet (§4-1 nr. 2):** Revenue is earned and recognised when the service/good is delivered, not when invoiced.
2. **Sammenstillingsprinsippet (§4-1 nr. 3):** Cost of Goods Sold (COGS) is matched against the corresponding recognised revenue in the same period.
3. **Kongruensprinsippet (§4-1 nr. 5):** All income and expenditure changes must flow through the period result.

> [!NOTE]
> **Jurisdiction scope:** This engine implements **Norwegian GAAP only**. International standards (IFRS 15 five-step revenue model, US GAAP ASC 606) have materially different accrual rules; multi-jurisdiction support is an engine-extension milestone, not a simple config swap.

### 2.2 The 7 Canonical Buckets
Project scope is strictly partitioned into 7 canonical buckets, iterated in deterministic order:
- **Hardware Buckets (HW):** `hardware`, `materials`, `freight`
- **Soft Buckets:** `pm` (project management), `tek` (technician/installation), `programming`, `travel`

### 2.3 Per-Bucket Periodisation Algorithm
The boundary cut is defined by `delivery_pct` (the delivered scope ratio, `0 <= delivery_pct <= 10`; not capped at 1.0 to support legitimate over-delivery):

$$\text{gated\_base} = \begin{cases} \text{expected\_revenue} - \text{expected\_revenue\_from\_co} & \text{if } \text{co\_recognition\_gated} \\ \text{expected\_revenue} & \text{otherwise} \end{cases}$$

$$\text{earned\_revenue} = \max(0, \text{gated\_base} \times \text{delivery\_pct})$$

$$\text{revenue\_gap} = \text{earned\_revenue} - \text{actual\_invoiced}$$

$$\text{target\_accrued} = \max(0, \text{revenue\_gap}) \quad \text{(Asset: unbilled earned revenue, Account 1531)}$$

$$\text{target\_deferred} = \max(0, -\text{revenue\_gap}) \quad \text{(Liability: invoiced unearned revenue, Account 2901)}$$

$$\text{target\_recognized\_cogs} = \text{expected\_cost} \times \text{delivery\_pct} \quad \text{(COGS Expense, Account 4300...)}$$

$$\text{target\_wip} = \text{actual\_cost} - \text{target\_recognized\_cogs} \quad \text{(Work in Progress, Account 1771; SIGNED!)}$$

$$\text{target\_unrecognized} = \begin{cases} \text{expected\_revenue\_from\_co} \times \text{delivery\_pct} & \text{if } \text{co\_recognition\_gated} \\ 0 & \text{otherwise} \end{cases}$$

### 2.4 Invariants & The Signed WIP Guarantee
- **Signed WIP (1771):** Positive WIP represents capitalized cost incurred ahead of delivery (carried over to future periods). Negative WIP represents delivery ahead of supplier billing (accrued unbilled cost).
- **Exact Decimal Arithmetic:** All money amounts are handled as `Decimal` quantised to øre (2 decimal places, `ROUND_HALF_UP`). Residual amounts are calculated by exact subtraction, ensuring zero rounding drift.
- **Dual Structural Identities (checked per bucket and across total):**
  - **Cost Identity:** $\text{target\_recognized\_cogs} + \text{target\_wip} \equiv \text{actual\_cost}$
  - **Revenue Identity:** $\text{actual\_invoiced} + \text{target\_accrued} - \text{target\_deferred} \equiv \text{earned\_revenue}$

### 2.5 Exposed MCP Tool: `economy_compute_periodisering`
Registered in `nce/tool_registry.py:672-677`.
- **Properties:** `cacheable: true`, `admin_only: false`, `mutation: false`
- **Request Arguments:**
  - `namespace_id` (`str`, UUID, required)
  - `params` (`dict`, required):
    - `project_id` (`str`, optional)
    - `period_end` (`str`, optional ISO date / period string)
    - `buckets` (`dict`, optional): map of bucket names (`hardware`, `pm`, etc.) to `{expected_revenue, expected_cost, actual_cost, actual_invoiced, delivery_pct, co_recognition_gated}`.
- **Response Shape:**
```json
{
  "project_id": "PROJECT:Q1001",
  "period_end": "2026-06-30",
  "gaap": "NGAAP (regnskapsloven §4-1)",
  "country": "NO",
  "buckets": [
    {
      "bucket": "hardware",
      "earned_revenue": "50000.00",
      "target_accrued": "0.00",
      "target_deferred": "10000.00",
      "target_recognized_cogs": "35000.00",
      "target_wip": "5000.00",
      "target_unrecognized": "0.00",
      "actual_cost": "40000.00",
      "actual_invoiced": "60000.00",
      "recognition_basis_pct": 50.0,
      "accounts": {
        "cogs": {"account": "4300", "account_name": "Innkjøp varer for videresalg", "mva_code": 0, "balance_side": "debit"},
        "revenue": {"account": "3000", "account_name": "Salgsinntekt handelsvarer, avgiftspliktig", "mva_code": 3, "balance_side": "credit"},
        "accrued": {"account": "1531", "account_name": "Påløpt, ikke fakturert driftsinntekt", "mva_code": 0, "balance_side": "debit"},
        "deferred": {"account": "2901", "account_name": "Forskuddsbetaling fra kunder", "mva_code": 0, "balance_side": "credit"},
        "wip": {"account": "1771", "account_name": "Varer under tilvirkning / prosjekt i arbeid", "mva_code": 0, "balance_side": "debit"}
      }
    }
  ],
  "totals": {
    "earned_revenue": "50000.00",
    "actual_cost": "40000.00",
    "actual_invoiced": "60000.00",
    "accrued": "0.00",
    "deferred": "10000.00",
    "recognized_cogs": "35000.00",
    "wip": "5000.00",
    "unrecognized": "0.00"
  }
}
```

### 2.6 Mounted REST Route: `/api/economy/periodisering`
- **Method & Path:** `POST /api/economy/periodisering` (`nce/admin_handlers/economy.py:148-193`)
- **Headers:** `Content-Type: application/json`, `Authorization: Bearer <token>`
- **Request Body:** `{"namespace_id": "...", "params": {"buckets": {...}, "project_id": "..."}}`
- **Response:** `{...result, "status": "ok"}` with every amount serialised as an exact decimal string.

---

## 3. Financial Events & The Balance Guarantee (`events.py`)

Implemented in [`events.py`](https://github.com/sindrehaugen/NCE/blob/main/nce/vertical_modules/economy/events.py). This module enforces double-entry ledger integrity across all financial operations.

### 3.1 Core Invariants
1. **Sum-to-Zero Balance Invariant:** A journal voucher must balance such that $|\sum \text{amount}| \le \text{epsilon}$ (default `0.01` NOK / øre).
2. **Never Auto-Balances:** The engine **never inserts balancing legs or alters amounts**. If a voucher is unbalanced by even 0.02 NOK, it is rejected outright with `UnbalancedPostingsError`.
3. **Signed Amount Convention:** Postings use a single signed `amount` column (`debit > 0`, `credit < 0`). Separate debit/credit column pairs are forbidden to eliminate sign-inversion bugs.
4. **Deterministic Canonical Hashing:** Generates a 64-character SHA-256 hash over canonical key-sorted JSON (`_content_hash`). All datetime values are forced to UTC.
5. **Dry-Run MCP/REST Surface:** The exposed MCP tool and REST route validate, normalise, and hash the event in memory without committing it to the database (persistence is executed via the internal `persist_financial_event` graph core).

### 3.2 Exposed MCP Tool: `economy_emit_event`
Registered in `nce/tool_registry.py:678-683`.
- **Properties:** `cacheable: true`, `admin_only: false`, `mutation: false`
- **Request Arguments:**
  - `namespace_id` (`str`, UUID, required)
  - `event` (`dict`, required): `{"type": "SUPPLIER_INVOICE_APPROVED", "postings": [{"account": "4300", "amount": 1000.00}, {"account": "2400", "amount": -1000.00}]}`
- **Security Check:** The `event` payload cannot contain the top-level keys `"status"` or `"error"`.
- **Response Shape (Success):**
```json
{
  "type": "SUPPLIER_INVOICE_APPROVED",
  "postings": [
    {"account": "4300", "amount": "1000.00"},
    {"account": "2400", "amount": "-1000.00"}
  ],
  "hash": "a5d89f20e4b819f7c04e287413d78216ef35293816a1b2c4e5f67890abcdef12"
}
```
- **Response Shape (Unbalanced Error):**
```json
{
  "error": "event SUPPLIER_INVOICE_APPROVED does not balance to zero: sum=50.00 (tolerance=0.01)",
  "event_type": "SUPPLIER_INVOICE_APPROVED",
  "diff": "50.00",
  "tolerance": "0.01"
}
```

### 3.3 Mounted REST Route: `/api/economy/emit-event`
- **Method & Path:** `POST /api/economy/emit-event` (`nce/admin_handlers/economy.py:182-260`)
- **Headers:** `Content-Type: application/json`, `Authorization: Bearer <token>`
- **Request Body:** `{"namespace_id": "...", "event": {...}}`
- **Response:** `{...normalised_event, "status": "ok"}` on 200; `422` on unbalanced postings or validation failure.

---

## 4. The 9 Unwired Internal Domain Cores (`do_*`)

The table below catalogs the 9 internal domain cores in `nce/vertical_modules/economy/` identified in `surface.md`. These functions provide core financial algorithms and are directly callable in Python or via vertical pipelines:

| Function | Module | Description | Input Parameters | Return Value |
|---|---|---|---|---|
| `do_match_invoice` | `matching.py:532` | 130-pt contextual invoice match against PO candidate pool | `thresholds` (dict), `invoice` (dict), `candidates` (list) | `{"score": int, "tier": "GREEN"\|"YELLOW"\|"RED", "breakdown": list}` |
| `do_compute_bucket_targets` | `ngaap.py:719` | NGAAP 7-bucket periodisation under regnskapsloven §4-1 | `chart` (dict), `mapping` (dict), `params` (dict) | `{"project_id", "period_end", "buckets": list, "totals": dict}` |
| `do_emit_financial_event` | `events.py:441` | Balance guarantee validation and canonical SHA-256 hashing | `epsilon` (float), `event` (dict with `type`, `postings`) | Shallow copy of `event` with normalized postings and `hash` |
| `do_compute_dunning` | `dunning.py:280` | Maps credit risk score (0–100) to Norwegian dunning aggression tier | `customer` (dict with `credit_risk_score`, `customer_id`) | `{"tier", "reminder_schedule_days", "hw_signing_required", "lindorff_handoff"}` |
| `do_compute_recognition_schedule` | `recurring.py:255` | 12-month ratable 1/12 revenue recognition schedule | `params` (`contract_id`, `annual_amount`, `start_period`) | `{"contract_id", "annual_amount", "periods": list[12], "total_recognized"}` |
| `do_snapshot_mrr_arr_churn` | `recurring.py:368` | Computes MRR, ARR, churned MRR, and churn rate across contracts | `params` (`contracts`: list of `{annual_amount, status}`) | `{"mrr", "arr", "churned_mrr", "churn_rate", "active_count", "churned_count"}` |
| `do_forecast_cashflow` | `forecast.py:408` | Deterministic Monte Carlo cashflow simulation | `seed` (int), `params` (`periods`, `iterations`, `opening_balance`) | `{"summary": {P10, P50, P90}, "periods": list[{P10, P50, P90}]}` |
| `do_generate_kid` | `peppol.py:211` | Generates Norwegian KID payment reference with check digit | `base_number` (str 1–24 digits), `variant` (default `"MOD10"`) | `{"base_number", "check_digit", "kid", "variant": "MOD10"}` |
| `do_validate_kid` | `peppol.py:259` | Validates complete Norwegian KID reference | `kid` (str 2–25 digits), `variant` (default `"MOD10"`) | `{"kid", "valid": bool, "variant": "MOD10"}` |

### Detailed Specifications of Cores 4–9:

#### Core 4: `do_compute_dunning` (`dunning.py`)
- **Policy Tiers:**
  - `risk_score < 20` -> **`LOW`**: Friendly reminder schedule (`+10, +21` days).
  - `20 <= risk_score < 40` -> **`STANDARD`**: Full schedule (`-3, +3, +10, +21` days).
  - `40 <= risk_score <= 60` -> **`ELEVATED`**: Full schedule (`-3, +3, +10, +21` days).
  - `60 < risk_score <= 100` -> **`CRITICAL`**: Full schedule + **`hw_signing_required: true`** (100% hardware signing on new POs) + **`lindorff_handoff: true`** (debt collection escalation).
- **Invariants:** Risk score `60.0` exactly is `ELEVATED` (not escalated); `60.0001` is `CRITICAL`. Non-numeric, boolean, negative, or `> 100` values raise `ValueError`.

#### Core 5 & 6: Recurring Revenue Cores (`recurring.py`)
- **`do_compute_recognition_schedule`:** Divides `annual_amount` into 12 periods (`YYYY-MM`). The first 11 periods receive `quantise(annual_amount / 12)`. The 12th period absorbs the entire rounding residual by exact subtraction: $\text{period}_{12} = \text{annual\_amount} - \sum_{i=1}^{11} \text{period}_i$. If $\text{period}_{12} < 0$, the function raises `ValueError` rather than distributing negative revenue.
- **`do_snapshot_mrr_arr_churn`:** Computes steady-state metrics:
  - $\text{MRR} = \sum_{\text{active}} \text{quantise}(\text{annual\_amount} / 12)$
  - $\text{ARR} = \text{MRR} \times 12$
  - $\text{Churned MRR} = \sum_{\text{churned}} \text{quantise}(\text{annual\_amount} / 12)$
  - $\text{Churn Rate} = \frac{\text{Churned MRR}}{\text{MRR} + \text{Churned MRR}}$

#### Core 7: `do_forecast_cashflow` (`forecast.py`)
- **Simulation Logic:** For each iteration, perturbs cashflow: $\text{net}_t = \text{expected\_net}_t \times (1 + Z \times \text{uncertainty\_pct}_t)$ where $Z \sim \mathcal{N}(0, 1)$.
- **Determinism Guarantee:** Takes an explicit `seed` integer and instantiates an isolated `random.Random(seed)` object. It never touches Python's global RNG, guaranteeing identical output across concurrent threads and test runs.
- **Percentiles:** Calculates nearest-rank P10, P50, and P90 percentiles over iteration outcomes.

#### Cores 8 & 9: KID Generation & Validation (`peppol.py`)
- **Algorithm:** Implements the **MOD10 (Luhn)** check-digit algorithm over ASCII digit strings. Preserves meaningful leading zeros (e.g. `"0001234"`).
- **MOD11 Status:** `variant="MOD11"` raises `NotImplementedError` pending bank-arrangement policy decisions.

---

## 5. Additional Internal Domain Cores

Beyond the 9 domain cores listed in `surface.md`, the Economy module houses several foundational execution engines:

### 5.1 The 7-Effect Approval Cascade (`cascade.py`)
`do_cascade_on_approval(engine, params)` (`nce/vertical_modules/economy/cascade.py:27-180`) is the **single and exclusive write path for `BOM_LINE.actual_cost`**. It executes when a supplier invoice is approved (Stage-2 approval):
1. **Idempotent Ingestion:** Records approval run in `action_idempotency`.
2. **BOM Cost Update:** Writes individual approval cost rows into `economy_bom_actual_costs` (Migration 047).
3. **Balanced Postings:** Emits balanced double-entry ledger lines into `economy_postings` (Migration 048).
4. **Margin Snapshot Update:** Recomputes `actual_margin_pct` on the `MARGIN` graph node (`signed_margin_pct` from Sales remains immutable).
5. **Graph Ingestion:** Upserts `INVOICE`, `POSTING`, and `PERIOD` nodes and edges.
6. **Cashflow Realisation:** Updates cash projections in `memories`.
7. **Cognitive Ledger Event:** Appends an auditable record to `v3_cognitive_ledger`.

### 5.2 Contract Master Store & Renewal Scanner (`contracts.py`)
- `do_upsert_contract`: The sole writer to `economy_contracts` (Migration 049).
- `do_validate_contract`: Enforces statutory and contractual rules (e.g. CPI uplift capped at ≤ 5.0%, contract downgrade notice ≥ 30 days).
- `do_scan_renewals`: 90-day forward renewal scanner for active contracts.

### 5.3 Continuous GL Reconciliation (`finago.py`)
- `do_reconcile_gl(engine, params)`: Connects to Finago via `FinagoClient`, pulls legal GL balances, diffs them against NCE internal periodised postings, and logs discrepancies to `divergence_log`.

---

## 6. Worked Examples

### 6.1 Triage an Invoice via Python Core
```python
from nce.vertical_modules.economy.matching import do_match_invoice, load_economy_thresholds

thresholds = load_economy_thresholds()
invoice = {
    "supplier_orgnr": "987654321",
    "project_dimension_present": True,
    "lines": [
        {"article_no": "SONY-VPL-5000", "description": "Sony 4K Laser Projector", "line_total": 45000.00}
    ]
}
candidates = [
    {
        "candidate_id": "po-item-101",
        "bom_line": {"article_no": "SONY-VPL-5000", "project_id": "PROJECT:Q100"},
        "context": {
            "supplier_exact": True,
            "expected_amount": 45000.00,
            "po_expected": True,
            "supplier_pattern_seen": True,
            "po_number_exact_match": True
        },
        "three_way_result": "MATCHED"
    }
]

result = do_match_invoice(thresholds, invoice, candidates)
# result == {"score": 180, "tier": "GREEN", "breakdown": [...]}
```

### 6.2 Calculate NGAAP Accruals for a Project
```python
from nce.vertical_modules.economy.ngaap import (
    do_compute_bucket_targets,
    load_finago_chart_of_accounts,
    load_finago_account_mapping
)

chart = load_finago_chart_of_accounts()
mapping = load_finago_account_mapping()
params = {
    "project_id": "PROJECT:Q100",
    "period_end": "2026-06-30",
    "buckets": {
        "hardware": {
            "expected_revenue": 100000.00,
            "expected_cost": 70000.00,
            "actual_cost": 35000.00,
            "actual_invoiced": 50000.00,
            "delivery_pct": 0.50
        }
    }
}

periodisering = do_compute_bucket_targets(chart, mapping, params)
# periodisering["buckets"][0]["target_recognized_cogs"] == Decimal('35000.00')
# periodisering["buckets"][0]["target_wip"] == Decimal('0.00')
```

---

## Appendix: Spec vs. Shipped Matrix (Delta from `docs/vertical_engines/08-economy-engine.md`)

| Feature / Capability | Spec Proposal | Shipped State (Main @ 7304330) | Notes |
|---|---|---|---|
| **MCP: `economy_match_invoice`** | Advisor | **Shipped** (cacheable) | Read-only invoice match triage |
| **MCP: `economy_compute_periodisering`** | Advisor | **Shipped** (cacheable) | Read-only NGAAP bucket targets |
| **MCP: `economy_emit_event`** | Advisor / Dry-run | **Shipped** (cacheable) | Balance validator & hasher (dry-run) |
| **MCP: `economy_forecast_cashflow`** | Watcher | *Internal `do_*` only* | Implemented in `forecast.py`, not in MCP registry |
| **MCP: `economy_compute_dunning`** | Watcher | *Internal `do_*` only* | Implemented in `dunning.py`, not in MCP registry |
| **MCP: `economy_mrr_snapshot`** | Advisor | *Internal `do_*` only* | Implemented in `recurring.py`, not in MCP registry |
| **MCP: `economy_reconcile_gl`** | Admin Advisor | *Internal `do_*` only* | Implemented in `finago.py`, not in MCP registry |
| **MCP: `economy_cascade_on_approval`** | Admin Actor | *Internal `do_*` only* | Implemented in `cascade.py`, not in MCP registry |
| **MCP: `economy_recognize_recurring`** | Autonomous (cron) | *Internal `do_*` + cron* | Implemented in `recurring.py` + `nce/cron.py` |
| **MCP: `economy_generate_ehf`** | Admin Actor | *Internal `do_*` only* | Format-only in `peppol.py` (PEPPOL transport stubbed) |
| **REST: `/api/economy/match-invoice`** | POST | **Shipped** | Mounted in `nce/admin_handlers/economy.py` |
| **REST: `/api/economy/periodisering`** | POST | **Shipped** | Mounted in `nce/admin_handlers/economy.py` |
| **REST: `/api/economy/emit-event`** | POST | **Shipped** | Mounted in `nce/admin_handlers/economy.py` |
| **REST: `/api/economy/cascade`** | POST | *Planned* | Not mounted in admin handlers |
| **REST: `/api/economy/mrr` / `/cashflow`** | GET | *Planned* | Not mounted in admin handlers |
| **REST: `/api/economy/reconcile`** | GET | *Planned* | Not mounted in admin handlers |
| **SQL: `economy_bom_actual_costs`** | Table (047) | **Shipped** | Natural key `(ns, bom_line, approval_id)` |
| **SQL: `economy_postings`** | Table (048) | **Shipped** | WORM grant, sum=0 trigger backstop |
| **SQL: `economy_contracts`** | Table (049) | **Shipped** | Replaces Wave 9 metadata shim |
