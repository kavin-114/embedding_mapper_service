# Embedding Mapper Service

## Purpose
Receives a canonical invoice JSON (extracted by vLLM document parser with per-field confidence scores) and maps it to an ERP-specific payload by resolving foreign-key fields via vector similarity search against ChromaDB.

## Tech Stack
- Python 3.11+ / FastAPI / Uvicorn
- ChromaDB (vector store)
- sentence-transformers (all-MiniLM-L6-v2)
- Pydantic v2 / PyYAML
- pytest + httpx for testing
- Docker + docker-compose

## Collection Naming Convention
`{entity}__{tenant_id}__{erp_system}`
Examples: `vendors__alfarsi__erpnext`, `items__tenant_b__odoo`

Each collection stores one ERP native ID in metadata field `erp_id`. No cross-ERP IDs in the same record.

### Collections
| Collection       | Purpose                                    |
|------------------|--------------------------------------------|
| vendors          | ERP vendor/supplier master data             |
| items            | ERP item/product master data                |
| tax_codes        | ERP item tax templates                      |
| uoms             | Units of measurement                        |
| companies        | ERP company records                         |
| addresses        | Supplier/company addresses                  |
| cost_centers     | ERP cost centers                            |
| warehouses       | ERP warehouses                              |
| tax_templates    | Purchase taxes and charges templates        |
| vendor_context   | Learned vendor→item mappings from feedback  |

## Resolution Strategies
| Strategy           | Description                              |
|-------------------|------------------------------------------|
| HARD_KEY          | Exact metadata match (tax_id, item_code, item_name) |
| CONTEXT_HARD_KEY  | Match via vendor_context (feedback history) |
| FILTERED_SEMANTIC | Semantic search with metadata filters (HSN, UOM) |
| PURE_SEMANTIC     | Semantic search without filters            |
| NOT_FOUND         | No match found                             |

## ScoredField Confidence → Strategy Selection
| Confidence   | Strategy           |
|-------------|--------------------|
| >= 0.90     | HARD_KEY           |
| >= 0.70     | FILTERED_SEMANTIC  |
| >= 0.50     | PURE_SEMANTIC      |
| < 0.50      | Skip, pure semantic|

## Pipeline Stages
1. **Load ERP Schema** — Read YAML config for target ERP (includes `tax_scope_map`, `line_item_map`).
2. **Vendor Resolution** — Hard-match on `tax_id` or semantic search with region_code boost/penalty.
3. **Unknown Vendor Handler** — Check sync freshness, try partial match, or declare unknown.
4. **Context Enrichment** — Build InvoiceContext from vendor metadata + vendor purchase history. Derives generic `tax_scope` from country+region comparison, maps to ERP-specific `tax_component` via YAML `tax_scope_map`. Resolves company, addresses, cost center, warehouse, tax template.
5. **Line Item Resolution** — Multi-step item, UOM, and tax resolution per line item (see details below).
6. **Transform Payload** — Rename keys and reshape to ERP-native format using YAML field_map and line_item_map.

## Vendor Resolution Details
- `vendor_tax_id` confidence >= 0.90 → hard match on `tax_id` metadata field
- Else → semantic search on vendor_name, with region_code boost (+0.08 if same as company) or penalty (-0.15 if different)
- Score >= 0.88 → FOUND, 0.50–0.87 → SUGGEST, < 0.50 → NOT_FOUND

## Item Resolution Details (Stage 5)
Multi-step cascade for each line item:

| Step | Strategy | Condition |
|------|----------|-----------|
| 1a | HARD_KEY | `item_code` confidence >= 0.90 → exact metadata match |
| 1b | HARD_KEY | `description` confidence >= 0.90 → exact match on `item_name` metadata |
| 2 | FILTERED_SEMANTIC | Semantic search filtered by HSN code, UOM (with alias expansion), item_group |
| 3 | PURE_SEMANTIC | Semantic search on description only (no filters) |
| 4 | Score boost | +0.10 for items in vendor's preferred_items history |
| 5 | CONTEXT_HARD_KEY | Fallback: if top score < 0.88 and vendor is known, search vendor_context filtered by vendor_erp_id |

### UOM Alias Expansion
When filtering by UOM, equivalent aliases are included via `$in` query to prevent false exclusions:
- Nos / Each / Pcs / Numbers / Pieces
- Kg / Kgs / Kilogram
- Mtr / Meter / Metre
- Ltr / Litre / Liter
- Box / Boxes, Set / Sets, Pair / Pairs

## Unknown Vendor Handler
- Check sync freshness (> 6 hours → STALE_DATA)
- Partial match above 0.35 → POSSIBLE_MATCH with top 3
- Otherwise → VENDOR_NOT_FOUND, action CREATE_VENDOR

## Feedback Learning Loop
When an invoice is flagged SUGGEST/REVIEW, a human reviews and approves the correct mappings. The external system then calls `POST /api/v1/feedback` with the verified vendor→item pairs.

### How it works:
1. Approved mappings are stored in `vendor_context__{tenant}__{erp}` collection.
2. Each record is a unique vendor+item pair with a frequency counter.
3. On repeat approvals, frequency increments (more frequent = higher trust).
4. During Stage 4 (Context Enrichment), the context_builder queries vendor history.
5. During Stage 5 (Item Resolution), items found in vendor history get a +0.10 score boost.
6. If items search score < 0.88 and vendor is known, vendor_context is searched as fallback (CONTEXT_HARD_KEY).
7. Verified `tax_id` from feedback is carried in InvoiceContext for downstream use.

### Collection schema (vendor_context):
- **ID**: `{vendor_erp_id}__{item_erp_id}`
- **Embed**: `"{vendor_name} {item_description}"`
- **Metadata**: vendor_erp_id, vendor_tax_id, item_erp_id, item_code, hsn_code, uom, description, frequency

## Vendor Collection Metadata Schema
| Field | Type | Description |
|-------|------|-------------|
| erp_id | str | ERP-native primary key |
| tax_id | str | Tax identifier (GSTIN, VAT, EIN, TIN, etc.) |
| tax_id_type | str | Type of tax ID: GSTIN, VAT, EIN, TIN |
| pan | str | PAN number (India-specific, optional) |
| trade_name | str | Trade/brand name (embedded for semantic matching) |
| category | str | Vendor category (Raw Material, Electrical, etc.) |
| country | str | ISO country code (IN, US, GB, etc.) |
| region_code | str | State/region code within the country |
| city | str | City name (embedded for semantic matching) |
| pincode | str | Postal/ZIP code |
| supplier_type | str | Company or Individual |
| currency | str | Default currency (INR, USD, EUR) |
| active | bool | Whether the vendor is active |

## Items Collection Metadata Schema
| Field | Type | Description |
|-------|------|-------------|
| erp_id | str | ERP-native primary key (ERPNext `name`) |
| item_code | str | Item code (hard key match) |
| item_name | str | Item display name (hard key match on description) |
| description | str | Long description (optional, enriches semantic embedding) |
| item_group | str | Category group (filter in semantic search) |
| uom | str | Default UOM (filter with alias expansion) |
| hsn_code | str | HSN/SAC code (filter in semantic search) |

**Embedded text**: `"{item_name} {description}"` — concatenated for richer semantic matching.

## ERPNext Line Item Mapping
The `line_item_map` in `erpnext.yaml` maps canonical fields to ERPNext PI item fields:
| Canonical Field | ERPNext Field | Source |
|----------------|---------------|--------|
| description | item_code | Resolved erp_id (Link to Item doctype) |
| item_name | item_name | Raw description text from invoice |
| quantity | qty | Raw numeric value |
| unit_price | rate | Raw numeric value |
| uom | uom | Resolved erp_id |
| tax_rate | item_tax_template | Resolved erp_id |
| warehouse | warehouse | Context default |
| expense_account | expense_account | Context default |
| cost_center | cost_center | Resolved erp_id |

## Tax Scope (Generic)
Tax scope is determined by comparing vendor's country/region with company's:
| Comparison | Tax Scope |
|-----------|-----------|
| Different country | IMPORT |
| Same country, different region | INTER_REGION |
| Same country, same region | INTRA_REGION |

ERP YAML `tax_scope_map` converts these to ERP-specific values (e.g., INTER_REGION → IGST).

## Confidence Decision Thresholds
| Score Range | Status    |
|------------|-----------|
| >= 0.88    | AUTO_MAP  |
| 0.70–0.87  | SUGGEST   |
| 0.50–0.69  | REVIEW    |
| < 0.50     | NO_MATCH  |

## API Endpoints
- `POST /api/v1/map` — Map canonical invoice to ERP payload (headers: X-ERP-System, X-Tenant-ID)
- `POST /api/v1/map/extractor` — Map extractor-format invoice (auto-adapts to canonical first)
- `POST /api/v1/sync` — Seed/update a collection from ERP master data
- `POST /api/v1/pull-sync` — Pull-sync master data from ERPNext directly
- `POST /api/v1/feedback` — Store human-approved vendor→item mappings for learning
- `GET  /api/v1/health` — Liveness probe
- `GET  /api/v1/health/ready` — Readiness probe (checks ChromaDB)
- `GET  /review` — Invoice review UI (single invoice, 4-panel view)
- `GET  /api/v1/review/files` — List extractor JSON files from server directory
- `GET  /api/v1/review/file` — Read an extractor JSON file (path-traversal protected)
- `GET  /api/v1/review/ground-truth` — Fetch ground truth from ERPNext by bill_no
- `GET  /backtest` — Backtest UI (seed + run with SSE streaming)
- `GET  /api/v1/backtest/config` — Backtest config defaults (masked credentials)
- `POST /api/v1/backtest/seed` — Seed ChromaDB from ERPNext (SSE streaming)
- `POST /api/v1/backtest/run` — Run backtest against ERPNext PIs (SSE streaming)

## Review UI (`/review`)
Single-page HTML app for e2e testing and feedback submission. Four collapsible panels:
1. **Original Document** — PDF/image viewer (collapsed by default)
2. **Extracted** — Extractor output fields with confidence color coding (collapsed by default)
3. **Mapped** — Pipeline results with status badges, inline-editable erp_ids (expanded by default)
4. **Ground Truth** — Auto-fetched from ERPNext by bill_no or paste frm.doc JSON (expanded by default)

Color coding: green (>=0.88 auto_map / >=90 confidence), amber (0.70-0.87 suggest / 70-89), orange (0.50-0.69 review / 50-69), red (<0.50 no_match / <50).

"Approve & Submit Feedback" button calls POST /api/v1/feedback to close the learning loop.

Config (tenant, ERP) shared with backtest page via localStorage.

## Backtest UI (`/backtest`)
Single-page HTML app for batch testing mapper accuracy with SSE streaming progress.

**Config panel**: tenant, ERP system, ERPNext credentials (masked from .env), invoices directory, format, invoice map, limit. Persists to localStorage (credentials excluded).

**Seed ChromaDB**: Streams entity-by-entity progress (vendors, items, tax_codes, etc.) with status icons and counts.

**Run Backtest**: Streams per-invoice results (accuracy, PASS/FAIL) with live progress bar. Summary panel shows stat cards (overall accuracy, by field type), breakdown tables (by strategy, by status), and failures table.

## Extractor Adapter
`app/services/extractor_adapter.py` converts ExtractorInvoice (vLLM format, confidence 0-100) to CanonicalInvoice (confidence 0.0-1.0). Handles field renaming, tax ID priority (GSTIN > VAT > tax_id > PAN), date parsing, and document-level tax distribution to line items.

## Backtest CLI
`python -m scripts.backtest.run` — batch-test mapper accuracy against ERPNext Purchase Invoices.

```bash
python -m scripts.backtest.run \
    --tenant-id alfarsi --erp-system erpnext \
    --invoices-dir ./extractor_outputs/ \
    --format extractor \
    --invoice-map data/file_to_invoice_map.json \
    --seed \
    --output reports/backtest_results
```

Steps: seed ChromaDB from ERPNext → load invoices → map each → fetch ground truth PIs → evaluate accuracy → generate JSON+CSV reports.

## ERPNext Connector
`app/services/connectors/erpnext.py` — Frappe REST API client with concurrent fetching (ThreadPoolExecutor). Used by pull-sync, backtest, and review ground-truth endpoints. Auth: `token api_key:api_secret`.

## Running
```bash
# Start ChromaDB
chroma run --host localhost --port 8000 --path ./chroma_data

# Start API
uvicorn app.main:app --reload --port 8080

# Open UIs
open http://localhost:8080/review
open http://localhost:8080/backtest

# Tests
pytest
```

## Configuration (`.env`)
```
ERPNEXT_URL=https://site.erpnext.com
ERPNEXT_API_KEY=<key>
ERPNEXT_API_SECRET=<secret>
CHROMA_HOST=localhost
CHROMA_PORT=8000
COMPANY_COUNTRY=OM
COMPANY_REGION_CODE=
REVIEW_FILES_DIR=/path/to/extractor/outputs
LOG_FORMAT=console
LOG_LEVEL=INFO
```

## Key Files
- `app/models/` — Pydantic models (canonical.py, resolution.py, response.py, feedback.py, extractor.py)
- `app/schemas/` — ERP YAML configs (erpnext.yaml, odoo.yaml, zoho.yaml)
- `app/services/` — Business logic (resolver, mapper, transformer, context_builder, extractor_adapter, vector_service, embedding_service)
- `app/services/connectors/` — ERPNext client + extractors (seed format converters)
- `app/routers/` — FastAPI route handlers (map, extractor_map, sync, pull_sync, feedback, review, backtest, health)
- `app/static/` — UI pages (review.html, backtest.html)
- `app/config.py` — Settings from environment
- `scripts/backtest/` — Backtest CLI (run.py, evaluator.py, extractor.py, report.py)
- `tests/` — pytest test suite (169 tests)
