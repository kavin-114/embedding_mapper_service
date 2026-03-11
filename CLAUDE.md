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
Examples: `vendors__tenant_a__erpnext`, `items__tenant_b__odoo`

Each collection stores one ERP native ID in metadata field `erp_id`. No cross-ERP IDs in the same record.

### Collections
| Collection       | Purpose                                    |
|------------------|--------------------------------------------|
| vendors          | ERP vendor/supplier master data             |
| items            | ERP item/product master data                |
| tax_codes        | ERP tax templates                           |
| uoms             | Units of measurement                        |
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
- `POST /api/v1/sync` — Seed/update a collection from ERP master data
- `POST /api/v1/feedback` — Store human-approved vendor→item mappings for learning
- `GET  /api/v1/health` — Liveness probe
- `GET  /api/v1/health/ready` — Readiness probe (checks ChromaDB)

## Running
```bash
# Start ChromaDB
chroma run --host localhost --port 8000 --path ./chroma_data

# Seed master data
python scripts/seed.py

# Start API
uvicorn app.main:app --reload --port 8080

# Tests
pytest
```

## Key Files
- `app/models/` — Pydantic models (canonical.py, resolution.py, response.py, feedback.py)
- `app/schemas/` — ERP YAML configs (erpnext.yaml, odoo.yaml, zoho.yaml)
- `app/services/` — Business logic (resolver, mapper, transformer, context_builder, etc.)
- `app/routers/` — FastAPI route handlers (map, sync, feedback, health)
- `app/config.py` — Settings from environment
- `scripts/` — Seed scripts and master data fixtures
