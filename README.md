# Embedding Mapper Service

Maps canonical invoice JSON (from vLLM document parser) to ERP-specific payloads by resolving foreign-key fields via vector similarity search.

## Supported ERPs
- ERPNext
- Odoo
- Zoho Books

## Quick Start

### Local Development
```bash
pip install -e ".[dev]"
cp .env.example .env
uvicorn app.main:app --reload --port 8080
```

### Docker
```bash
docker compose up --build
```
This starts both the API (port 8080) and ChromaDB (port 8000).

### Running Tests
```bash
pytest
```

## API Usage

### Map an Invoice
```bash
curl -X POST http://localhost:8080/api/v1/map \
  -H "Content-Type: application/json" \
  -H "X-ERP-System: erpnext" \
  -H "X-Tenant-ID: tenant_a" \
  -d '{
    "invoice": {
      "invoice_number": "INV-001",
      "invoice_date": "2025-06-15",
      "vendor_name": {"value": "Acme Supplies", "confidence": 0.95},
      "vendor_gstin": {"value": "29ABCDE1234F1Z5", "confidence": 0.92},
      "currency": "INR",
      "total_amount": 11800.00,
      "line_items": [{
        "description": {"value": "Steel Bolts M10", "confidence": 0.88},
        "quantity": 100,
        "unit_price": 100.00,
        "uom": {"value": "NOS", "confidence": 0.95},
        "tax_rate": {"value": "18", "confidence": 0.91}
      }]
    }
  }'
```

### Sync Master Data
```bash
curl -X POST http://localhost:8080/api/v1/sync \
  -H "Content-Type: application/json" \
  -d '{
    "entity": "vendors",
    "tenant_id": "tenant_a",
    "erp_system": "erpnext",
    "records": [
      {"erp_id": "SUP-001", "text": "Acme Supplies Industrial", "gstin": "29ABCDE1234F1Z5", "category": "Raw Material", "state_code": "29", "active": true}
    ],
    "synced_at": "2025-06-15T10:00:00Z"
  }'
```

### Health Checks
```bash
curl http://localhost:8080/api/v1/health
curl http://localhost:8080/api/v1/health/ready
```

## Architecture

See [CLAUDE.md](CLAUDE.md) for detailed pipeline documentation.
