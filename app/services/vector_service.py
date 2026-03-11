"""Vector service — ChromaDB wrapper for upsert, query, and hard-match operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

import chromadb

if TYPE_CHECKING:
    from app.config import Settings

# ChromaDB stores distances; we convert to similarity scores.
# For cosine distance: score = 1 - distance
_DISTANCE_TO_SCORE = lambda d: max(0.0, 1.0 - d)


# ── text builders per entity ────────────────────────────────────────
# Each entity type combines metadata fields into a single embedding string.

def _build_text_vendors(rec: dict[str, Any]) -> str:
    parts = [rec.get("text", "")]
    if rec.get("trade_name"):
        parts.append(rec["trade_name"])
    if rec.get("category"):
        parts.append(rec["category"])
    if rec.get("city"):
        parts.append(rec["city"])
    return " ".join(p for p in parts if p)


def _build_text_items(rec: dict[str, Any]) -> str:
    parts = [rec.get("text", "")]
    if rec.get("description"):
        parts.append(rec["description"])
    return " ".join(p for p in parts if p)


def _build_text_tax_codes(rec: dict[str, Any]) -> str:
    parts = [rec.get("text", "")]
    if rec.get("rate"):
        parts.append(str(rec["rate"]))
    if rec.get("component"):
        parts.append(rec["component"])
    return " ".join(p for p in parts if p)


def _build_text_uoms(rec: dict[str, Any]) -> str:
    return rec.get("text", rec.get("uom_code", ""))


_TEXT_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "vendors": _build_text_vendors,
    "items": _build_text_items,
    "tax_codes": _build_text_tax_codes,
    "uoms": _build_text_uoms,
}

# Fields that are stored as metadata (everything except 'text' and 'erp_id')
_RESERVED_KEYS = {"text", "erp_id"}


class VectorService:
    """Manages ChromaDB collections following the {entity}__{tenant}__{erp} convention."""

    def __init__(self, settings: "Settings") -> None:
        self.client = chromadb.HttpClient(
            host=settings.chroma_host,
            port=settings.chroma_port,
        )
        self._sync_times: dict[str, datetime] = {}

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def collection_name(entity: str, tenant_id: str, erp_system: str) -> str:
        """Build the canonical collection name.

        Format: {entity}__{tenant_id}__{erp_system}
        Example: vendors__tenant_a__erpnext
        """
        return f"{entity}__{tenant_id}__{erp_system}"

    def _get_collection(self, entity: str, tenant_id: str, erp_system: str):
        """Get or create a ChromaDB collection by convention name."""
        name = self.collection_name(entity, tenant_id, erp_system)
        return self.client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    # ── write operations ─────────────────────────────────────────────

    def upsert(
        self,
        entity: str,
        tenant_id: str,
        erp_system: str,
        records: list[dict[str, Any]],
        synced_at: datetime,
        embedding_fn: Callable[[list[str]], list[list[float]]],
    ) -> int:
        """Upsert master-data records into the appropriate collection.

        Each record dict must contain at minimum:
          - erp_id:  the ERP-native primary key
          - text:    display string to embed (or entity-specific fields)

        All other keys become metadata.
        """
        if not records:
            return 0

        collection = self._get_collection(entity, tenant_id, erp_system)
        text_builder = _TEXT_BUILDERS.get(entity, lambda r: r.get("text", ""))

        texts = [text_builder(r) for r in records]
        embeddings = embedding_fn(texts)

        ids: list[str] = []
        metadatas: list[dict[str, Any]] = []
        documents: list[str] = []

        for rec, text in zip(records, texts):
            erp_id = rec["erp_id"]
            ids.append(str(erp_id))
            documents.append(text)

            meta = {"erp_id": str(erp_id)}
            for k, v in rec.items():
                if k not in _RESERVED_KEYS:
                    # ChromaDB metadata values must be str, int, float, or bool
                    if isinstance(v, bool):
                        meta[k] = v
                    elif isinstance(v, (int, float)):
                        meta[k] = v
                    elif v is not None:
                        meta[k] = str(v)
            metadatas.append(meta)

        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=documents,
        )

        # Record sync timestamp
        key = self.collection_name(entity, tenant_id, erp_system)
        self._sync_times[key] = synced_at

        return len(records)

    # ── read operations ──────────────────────────────────────────────

    def hard_match(
        self,
        entity: str,
        tenant_id: str,
        erp_system: str,
        where: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Exact metadata match (e.g. WHERE gstin == '29ABCDE1234F1Z5').

        Returns the first matching record's metadata dict (including erp_id),
        or None if no match.
        """
        collection = self._get_collection(entity, tenant_id, erp_system)
        results = collection.get(where=where, limit=1)

        if not results["ids"]:
            return None

        meta = results["metadatas"][0]
        return meta

    def semantic_search(
        self,
        entity: str,
        tenant_id: str,
        erp_system: str,
        query_embedding: list[float],
        n_results: int = 3,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic similarity search on a collection.

        Returns a list of result dicts, each with:
          erp_id, metadata, distance, score
        sorted by descending score.
        """
        collection = self._get_collection(entity, tenant_id, erp_system)

        query_kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
        }
        if where:
            query_kwargs["where"] = where

        try:
            results = collection.query(**query_kwargs)
        except Exception:
            # If filtered query returns no results ChromaDB may raise;
            # fall back to unfiltered.
            if where:
                query_kwargs.pop("where")
                results = collection.query(**query_kwargs)
            else:
                return []

        if not results["ids"] or not results["ids"][0]:
            return []

        output = []
        for i, doc_id in enumerate(results["ids"][0]):
            dist = results["distances"][0][i]
            meta = results["metadatas"][0][i]
            output.append({
                "erp_id": meta.get("erp_id", doc_id),
                "metadata": meta,
                "distance": dist,
                "score": _DISTANCE_TO_SCORE(dist),
            })

        return output

    def get_sync_time(
        self,
        entity: str,
        tenant_id: str,
        erp_system: str,
    ) -> datetime | None:
        """Return the last synced_at timestamp for a collection."""
        key = self.collection_name(entity, tenant_id, erp_system)
        return self._sync_times.get(key)

    # ── vendor context (feedback learning) ───────────────────────────

    def upsert_vendor_context(
        self,
        tenant_id: str,
        erp_system: str,
        vendor_erp_id: str,
        vendor_name: str,
        vendor_tax_id: str | None,
        items: list[dict[str, Any]],
        embedding_fn: Callable[[list[str]], list[list[float]]],
    ) -> int:
        """Store approved vendor→item mappings in the vendor_context collection.

        Each record is a unique vendor+item pair.  If the pair already
        exists its frequency counter is incremented.

        Collection: vendor_context__{tenant}__{erp}
        ID:         {vendor_erp_id}__{item_erp_id}
        Embed:      "{vendor_name} {item_description}"
        Metadata:   vendor_erp_id, vendor_tax_id, item_erp_id, item_code,
                    hsn_code, uom, description, frequency
        """
        if not items:
            return 0

        collection = self._get_collection("vendor_context", tenant_id, erp_system)

        # Check existing records for frequency increment
        ids: list[str] = []
        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []

        for item in items:
            item_erp_id = item["item_erp_id"]
            doc_id = f"{vendor_erp_id}__{item_erp_id}"
            ids.append(doc_id)

            desc = item.get("description", "")
            texts.append(f"{vendor_name} {desc}")

            # Try to get existing frequency
            frequency = 1
            try:
                existing = collection.get(ids=[doc_id])
                if existing["ids"]:
                    old_freq = existing["metadatas"][0].get("frequency", 0)
                    frequency = old_freq + 1
            except Exception:
                pass

            meta: dict[str, Any] = {
                "vendor_erp_id": str(vendor_erp_id),
                "vendor_name": vendor_name,
                "item_erp_id": str(item_erp_id),
                "description": desc,
                "frequency": frequency,
            }
            if vendor_tax_id:
                meta["vendor_tax_id"] = vendor_tax_id
            if item.get("item_code"):
                meta["item_code"] = str(item["item_code"])
            if item.get("hsn_code"):
                meta["hsn_code"] = str(item["hsn_code"])
            if item.get("uom"):
                meta["uom"] = str(item["uom"])

            metadatas.append(meta)

        embeddings = embedding_fn(texts)

        collection.upsert(
            ids=ids,
            embeddings=embeddings,
            metadatas=metadatas,
            documents=texts,
        )

        return len(items)

    def get_vendor_context(
        self,
        tenant_id: str,
        erp_system: str,
        vendor_erp_id: str,
    ) -> list[dict[str, Any]]:
        """Retrieve all historic item mappings for a vendor.

        Returns a list of metadata dicts sorted by frequency (descending),
        each containing: item_erp_id, item_code, hsn_code, description,
        frequency, vendor_gstin.
        """
        collection = self._get_collection("vendor_context", tenant_id, erp_system)

        try:
            results = collection.get(
                where={"vendor_erp_id": str(vendor_erp_id)},
            )
        except Exception:
            return []

        if not results["ids"]:
            return []

        items = list(results["metadatas"])
        items.sort(key=lambda m: m.get("frequency", 0), reverse=True)
        return items
