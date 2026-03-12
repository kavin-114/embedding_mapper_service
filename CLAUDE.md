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

## ScoredField Confidence → Strategy
| Confidence   | Strategy           |
|-------------|--------------------|
| >= 0.90     | HARD_KEY           |
| >= 0.70     | FILTERED_SEMANTIC  |
| >= 0.50     | PURE_SEMANTIC      |
| < 0.50      | Skip, pure semantic|

## Pipeline Stages
1. **Load ERP Schema** — Read YAML config for target ERP (includes `tax_scope_map`).
2. **Vendor Resolution** — Hard-match on `tax_id` or semantic search with region_code boost/penalty.
3. **Unknown Vendor Handler** — Check sync freshness, try partial match, or declare unknown.
4. **Context Enrichment** — Build InvoiceContext from vendor metadata + vendor purchase history. Derives generic `tax_scope` (INTRA_REGION/INTER_REGION/IMPORT) from country+region comparison, then maps to ERP-specific `tax_component` via YAML `tax_scope_map`.
5. **Line Item Resolution** — Resolve item, UOM, and tax FK fields per line item (with history boost).
6. **Transform Payload** — Rename keys and reshape to ERP-native format using YAML field_map.

## Vendor Resolution Details
- `vendor_tax_id` confidence >= 0.90 → hard match on `tax_id` metadata field
- Else → semantic search on vendor_name, with region_code boost (+0.08 if same as company) or penalty (-0.15 if different)
- Score >= 0.88 → FOUND, 0.50–0.87 → SUGGEST, < 0.50 → NOT_FOUND

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
6. Verified `tax_id` from feedback is carried in InvoiceContext for downstream use.

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

## Review UI (`/review`)
Single-page HTML app for e2e testing and feedback submission. Four collapsible panels:
1. **Original Document** — PDF/image viewer (collapsed by default)
2. **Extracted** — Extractor output fields with confidence color coding (collapsed by default)
3. **Mapped** — Pipeline results with status badges, inline-editable erp_ids (expanded by default)
4. **Ground Truth** — Auto-fetched from ERPNext by bill_no or paste frm.doc JSON (expanded by default)

Color coding: green (>=0.88 auto_map / >=90 confidence), amber (0.70-0.87 suggest / 70-89), orange (0.50-0.69 review / 50-69), red (<0.50 no_match / <50).

"Approve & Submit Feedback" button calls POST /api/v1/feedback to close the learning loop.

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

# Open review UI
open http://localhost:8080/review

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
- `app/routers/` — FastAPI route handlers (map, extractor_map, sync, pull_sync, feedback, review, health)
- `app/static/` — Review UI (review.html)
- `app/config.py` — Settings from environment
- `scripts/backtest/` — Backtest CLI (run.py, evaluator.py, extractor.py, report.py, erpnext_client.py)
- `tests/` — pytest test suite (164 tests)
