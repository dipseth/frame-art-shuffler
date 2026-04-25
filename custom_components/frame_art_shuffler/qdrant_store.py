"""
Qdrant vector store for frame art images.

Uses jina-clip-v2 (multimodal, 512-dim) so text queries and image thumbnails
share the same vector space — enabling:
  - Semantic search ("find all sunset images")
  - Content-ID resolution (match TV thumbnail → filename)
  - Metadata sync (authoritative store for tags, matte, content_ids)

Credentials are read from HA secrets:
  QDRANT_URL, QDRANT_API_KEY, JINA_API_KEY
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

_LOGGER = logging.getLogger(__name__)

COLLECTION = "frame_art"
VECTOR_DIM = 512           # jina-clip-v2 with dimensions=512
JINA_MODEL = "jina-clip-v2"
JINA_EMBED_URL = "https://api.jina.ai/v1/embeddings"


# ---------------------------------------------------------------------------
# Jina embedding helpers
# ---------------------------------------------------------------------------

def _jina_embed_texts(texts: list[str], api_key: str) -> list[list[float]]:
    """Embed a list of text strings via Jina API."""
    resp = requests.post(
        JINA_EMBED_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": JINA_MODEL,
            "input": [{"text": t} for t in texts],
            "dimensions": VECTOR_DIM,
            "normalized": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return [item["embedding"] for item in data["data"]]


def _resize_image_bytes(raw: bytes, max_side: int = 512) -> bytes:
    """Shrink an image to max_side on the longest dimension, return JPEG bytes."""
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = img.size
    if max(w, h) > max_side:
        ratio = max_side / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _jina_embed_image_bytes(image_bytes: bytes, api_key: str) -> list[float]:
    """Embed a single image (raw bytes) via Jina API. Resizes to ≤512px first."""
    small = _resize_image_bytes(image_bytes)
    b64 = base64.b64encode(small).decode()
    resp = requests.post(
        JINA_EMBED_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": JINA_MODEL,
            "input": [{"image": b64}],
            "dimensions": VECTOR_DIM,
            "normalized": True,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"][0]["embedding"]


def _jina_embed_image_file(image_path: Path, api_key: str) -> list[float]:
    """Embed an image file via Jina API."""
    return _jina_embed_image_bytes(image_path.read_bytes(), api_key)


# ---------------------------------------------------------------------------
# Text description builder
# ---------------------------------------------------------------------------

def _build_text_description(
    filename: str,
    tags: list[str] | None = None,
    matte: str | None = None,
    filter_type: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """Build a rich text description of an image for embedding."""
    stem = Path(filename).stem.replace("-", " ").replace("_", " ")
    parts = [stem]
    if tags:
        parts.extend(tags)
    if matte and matte not in ("none", ""):
        parts.append(f"matte:{matte}")
    if width and height:
        orientation = "portrait" if height > width else "landscape"
        parts.append(orientation)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# QdrantArtStore
# ---------------------------------------------------------------------------

class QdrantArtStore:
    """Manages the 'frame_art' Qdrant collection."""

    def __init__(self, qdrant_url: str, qdrant_api_key: str, jina_api_key: str) -> None:
        self._client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key)
        self._jina_key = jina_api_key
        self._ensure_collection()

    # ------------------------------------------------------------------
    # Collection lifecycle
    # ------------------------------------------------------------------

    def _ensure_collection(self) -> None:
        """Create collection if it doesn't exist."""
        existing = {c.name for c in self._client.get_collections().collections}
        if COLLECTION not in existing:
            self._client.create_collection(
                collection_name=COLLECTION,
                vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
            )
            _LOGGER.info("Created Qdrant collection '%s'", COLLECTION)

    # ------------------------------------------------------------------
    # Upsert / remove
    # ------------------------------------------------------------------

    def upsert_image(
        self,
        filename: str,
        *,
        tags: list[str] | None = None,
        matte: str | None = None,
        filter_type: str | None = None,
        width: int | None = None,
        height: int | None = None,
        tv_content_ids: dict[str, str] | None = None,
        image_path: Path | None = None,
    ) -> None:
        """Add or update an image in the collection.

        If image_path is provided, embeds the actual image file (better for
        content-ID resolution via thumbnail matching).  Falls back to text
        embedding from metadata description.
        """
        if image_path and image_path.exists():
            vector = _jina_embed_image_file(image_path, self._jina_key)
            embed_source = "image"
        else:
            text = _build_text_description(filename, tags, matte, filter_type, width, height)
            vector = _jina_embed_texts([text], self._jina_key)[0]
            embed_source = "text"

        payload: dict[str, Any] = {
            "filename": filename,
            "tags": tags or [],
            "matte": matte or "none",
            "filter_type": filter_type or "none",
            "width": width,
            "height": height,
            "tv_content_ids": tv_content_ids or {},
            "embed_source": embed_source,
        }

        # Use a stable integer ID derived from filename (MD5, deterministic across runs)
        point_id = int(hashlib.md5(filename.encode()).hexdigest(), 16) % (2**53)

        self._client.upsert(
            collection_name=COLLECTION,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        _LOGGER.debug("Upserted '%s' (source=%s)", filename, embed_source)

    def update_content_id(self, filename: str, tv_ip: str, content_id: str) -> None:
        """Record a TV content_id for an existing image point.

        Uses the stable MD5 point ID for O(1) retrieval.
        """
        point_id = int(hashlib.md5(filename.encode()).hexdigest(), 16) % (2**53)
        results = self._client.retrieve(
            collection_name=COLLECTION,
            ids=[point_id],
            with_payload=True,
        )
        if not results:
            _LOGGER.warning("update_content_id: '%s' not found in Qdrant", filename)
            return

        point = results[0]
        tv_ids: dict = dict(point.payload.get("tv_content_ids", {}))
        tv_ids[tv_ip] = content_id
        self._client.set_payload(
            collection_name=COLLECTION,
            payload={"tv_content_ids": tv_ids},
            points=[point_id],
        )

    def remove_image(self, filename: str) -> None:
        """Remove an image point by filename."""
        point_id = int(hashlib.md5(filename.encode()).hexdigest(), 16) % (2**53)
        self._client.delete(
            collection_name=COLLECTION,
            points_selector=[point_id],
        )
        _LOGGER.debug("Removed '%s' from Qdrant", filename)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def find_by_filename(self, filename: str) -> dict[str, Any] | None:
        """Return the payload for a given filename, or None.

        Uses stable MD5 point ID for O(1) retrieval.
        """
        point_id = int(hashlib.md5(filename.encode()).hexdigest(), 16) % (2**53)
        results = self._client.retrieve(
            collection_name=COLLECTION,
            ids=[point_id],
            with_payload=True,
        )
        return results[0].payload if results else None

    def find_by_content_id(self, tv_ip: str, content_id: str) -> str | None:
        """Return the filename for a given TV content_id, or None.

        Uses a full payload scan — acceptable for our small library (<1000 images).
        TV IP keys contain dots which conflict with Qdrant's nested-field dot-path
        notation, so indexed filtering isn't feasible without key normalization.
        """
        all_points, _ = self._client.scroll(
            collection_name=COLLECTION, limit=1000, with_payload=True
        )
        for pt in all_points:
            tv_ids = pt.payload.get("tv_content_ids", {})
            if tv_ids.get(tv_ip) == content_id:
                return pt.payload.get("filename")
        return None

    def find_by_thumbnail(
        self, thumbnail_bytes: bytes, top_k: int = 3
    ) -> list[tuple[str, float]]:
        """Find closest images by embedding a thumbnail.

        Returns list of (filename, score) sorted by descending similarity.
        """
        vector = _jina_embed_image_bytes(thumbnail_bytes, self._jina_key)
        results = self._client.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=top_k,
            with_payload=True,
        )
        return [(r.payload["filename"], r.score) for r in results.points]

    def search_by_text(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Semantic text search across the library.

        Example: search_by_text("pink flowers spring") returns closest images.
        """
        vector = _jina_embed_texts([query], self._jina_key)[0]
        results = self._client.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=top_k,
            with_payload=True,
        )
        return [(r.payload["filename"], r.score) for r in results.points]

    def search_full(self, query: str, top_k: int = 10) -> list[dict]:
        """Semantic search returning full Qdrant payload + score for each result.

        Returns a list of dicts with all image metadata fields plus a 'score' key.
        Intended for the search UI — richer than search_by_text's (filename, score) tuples.
        """
        vector = _jina_embed_texts([query], self._jina_key)[0]
        results = self._client.query_points(
            collection_name=COLLECTION,
            query=vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            {**r.payload, "score": round(r.score, 4)}
            for r in results.points
        ]

    def list_all(self) -> list[dict[str, Any]]:
        """Return all image payloads."""
        points, _ = self._client.scroll(
            collection_name=COLLECTION, limit=1000, with_payload=True
        )
        return [pt.payload for pt in points]

    def count(self) -> int:
        return self._client.count(collection_name=COLLECTION).count


# ---------------------------------------------------------------------------
# Convenience factory — reads creds from environment / secrets
# ---------------------------------------------------------------------------

def _find_secrets_file() -> Path | None:
    """Locate secrets.yaml across host and container mount points.

    Inside HA's core container the config dir is /config; on the host OS it's
    /homeassistant. Try both so this module works in either environment.
    """
    for candidate in ("/config/secrets.yaml", "/homeassistant/secrets.yaml"):
        p = Path(candidate)
        if p.exists():
            return p
    return None


def _read_yaml_secret(key: str, secrets_path: str | None = None) -> str | None:
    """Read a value from secrets.yaml without importing HA machinery."""
    path = Path(secrets_path) if secrets_path else _find_secrets_file()
    if path is None:
        return None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def get_store() -> QdrantArtStore:
    """Return a QdrantArtStore using secrets.yaml credentials."""
    url = os.getenv("QDRANT_URL") or _read_yaml_secret("QDRANT_URL")
    qdrant_key = os.getenv("QDRANT_API_KEY") or _read_yaml_secret("QDRANT_API_KEY")
    jina_key = os.getenv("JINA_API_KEY") or _read_yaml_secret("JINA_API_KEY")
    if not all([url, qdrant_key, jina_key]):
        raise RuntimeError("Missing QDRANT_URL / QDRANT_API_KEY / JINA_API_KEY in secrets.yaml")
    return QdrantArtStore(url, qdrant_key, jina_key)
