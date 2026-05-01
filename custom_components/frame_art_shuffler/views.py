"""HTTP views for Frame Art Shuffler — semantic search and display API."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.components.http import HomeAssistantView
except ImportError:  # pragma: no cover
    HomeAssistantView = object  # type: ignore[assignment, misc]


def _tv_sensor_entity_id(hass: Any, entry: Any, tv_id: str) -> str | None:
    """Return the entity_id of the tv_actual_image sensor for a given TV."""
    from homeassistant.helpers import entity_registry as er
    from .const import DOMAIN
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{tv_id}_tv_actual_image"
    )


class ArtSearchView(HomeAssistantView):
    """Semantic search endpoint.

    GET /api/frame_art_shuffler/search?q=<query>&top_k=10

    Returns JSON:
        {"query": "...", "results": [{"filename": "...", "score": 0.37, "tags": [...], ...}]}

    The Qdrant store is pre-initialised in hass.data — if it's not available
    (Qdrant not configured) the endpoint returns 503.
    """

    url = "/api/frame_art_shuffler/search"
    name = "api:frame_art_shuffler:search"
    requires_auth = False

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry

    async def get(self, request: Any) -> Any:
        from aiohttp import web
        from .const import DOMAIN

        query = request.query.get("q", "").strip()
        if not query:
            return web.json_response({"error": "Missing query parameter 'q'"}, status=400)

        try:
            top_k = max(1, min(int(request.query.get("top_k", "16")), 200))
        except (ValueError, TypeError):
            top_k = 16

        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        store = data.get("qdrant_store")
        if store is None:
            return web.json_response(
                {"error": "Qdrant not configured — run seed_qdrant and add credentials to secrets.yaml"},
                status=503,
            )

        try:
            results = await self._hass.async_add_executor_job(
                store.search_full, query, top_k
            )
            return web.json_response({"query": query, "results": results})
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Art search failed for query %r: %s", query, err)
            return web.json_response({"error": str(err)}, status=500)


class ArtSearchConfigView(HomeAssistantView):
    """Returns TV names and entity IDs for the search UI selector.

    GET /api/frame_art_shuffler/search_config

    Returns JSON:
        {"tvs": [{"tv_id": "livingroom", "name": "Living Room", "entity_id": "sensor...."}]}
    """

    url = "/api/frame_art_shuffler/search_config"
    name = "api:frame_art_shuffler:search_config"
    requires_auth = False

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry

    async def get(self, request: Any) -> Any:
        from aiohttp import web
        from .config_entry import list_tv_configs

        tv_configs = list_tv_configs(self._entry)
        tvs = []
        for tv_id, tv_config in tv_configs.items():
            entity_id = _tv_sensor_entity_id(self._hass, self._entry, tv_id)
            current_image = None
            if entity_id:
                state = self._hass.states.get(entity_id)
                if state and state.state not in (None, "", "unknown", "unavailable"):
                    current_image = state.state
            tvs.append({
                "tv_id": tv_id,
                "name": tv_config.get("name", tv_id),
                "entity_id": entity_id,
                "current_image": current_image,
            })

        return web.json_response({"tvs": tvs})


class ArtDisplayView(HomeAssistantView):
    """Trigger display_image on a TV from the search UI.

    POST /api/frame_art_shuffler/display
    Body JSON: {"tv_id": "livingroom", "filename": "cherry_blossoms.jpg"}

    The TV must be registered in this config entry — unknown tv_ids are rejected.
    """

    url = "/api/frame_art_shuffler/display"
    name = "api:frame_art_shuffler:display"
    requires_auth = False

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry

    async def post(self, request: Any) -> Any:
        from aiohttp import web
        from .config_entry import list_tv_configs
        from .const import DOMAIN

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        filename = (body.get("filename") or "").strip()
        tv_id = (body.get("tv_id") or "").strip()

        if not filename or not tv_id:
            return web.json_response(
                {"error": "'filename' and 'tv_id' are required"}, status=400
            )

        tv_configs = list_tv_configs(self._entry)
        if tv_id not in tv_configs:
            return web.json_response({"error": f"Unknown TV id: {tv_id!r}"}, status=404)

        entity_id = _tv_sensor_entity_id(self._hass, self._entry, tv_id)
        if not entity_id:
            return web.json_response(
                {"error": f"Entity not found for TV {tv_id!r}"},
                status=500,
            )

        try:
            # blocking=True so the HTTP response reflects the real outcome.
            # Frees up the search UI to show errors instead of always-green.
            # Uploads typically settle in 5-10s; aiohttp's request handler
            # tolerates that comfortably.
            await self._hass.services.async_call(
                DOMAIN,
                "display_image",
                {"entity_id": entity_id, "filename": filename},
                blocking=True,
            )
            return web.json_response({"status": "ok", "filename": filename, "tv_id": tv_id})
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("display_image call failed for %r on %r: %s", filename, tv_id, err)
            return web.json_response({"error": str(err)}, status=500)


class ArtSimilarView(HomeAssistantView):
    """Find images similar to a given filename via Qdrant nearest-neighbor.

    GET /api/frame_art_shuffler/similar?target=<filename>&top_k=<k>

    Internally reuses store.discover() with empty context — Qdrant's
    HNSW index returns the closest neighbors of the target point.
    Excludes the target from the result list.
    """

    url = "/api/frame_art_shuffler/similar"
    name = "api:frame_art_shuffler:similar"
    requires_auth = False

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry

    async def get(self, request: Any) -> Any:
        from aiohttp import web
        from .const import DOMAIN

        target = (request.query.get("target") or "").strip()
        if not target:
            return web.json_response(
                {"error": "Missing query parameter 'target'"}, status=400
            )

        try:
            top_k = max(1, min(int(request.query.get("top_k", "16")), 200))
        except (ValueError, TypeError):
            top_k = 16

        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        store = data.get("qdrant_store")
        if store is None:
            return web.json_response({"error": "Qdrant not configured"}, status=503)

        try:
            # +1 because Qdrant returns the target itself at score 1.0; drop it.
            raw = await self._hass.async_add_executor_job(
                lambda: store.discover(
                    target_filename=target,
                    positives=[],
                    negatives=[],
                    top_k=top_k + 1,
                )
            )
            results = [r for r in raw if r.get("filename") != target][:top_k]
            return web.json_response({"target": target, "results": results})
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Similar failed for target=%r: %s", target, err)
            return web.json_response({"error": str(err)}, status=500)


class ArtDiscoverView(HomeAssistantView):
    """Qdrant Discover endpoint — image-based / context-shaped search.

    POST /api/frame_art_shuffler/discover
    Body JSON:
        {
            "text_query": "pink flowers" | null,
            "positives": ["roses.jpg", ...],
            "negatives": ["dog.jpg", ...],
            "top_k": 16
        }

    If text_query is non-empty it becomes the discover target. Otherwise the
    first positive becomes the target, and we require >=2 positives and
    >=3 selections total.
    """

    url = "/api/frame_art_shuffler/discover"
    name = "api:frame_art_shuffler:discover"
    requires_auth = False

    def __init__(self, hass: Any, entry: Any) -> None:
        self._hass = hass
        self._entry = entry

    async def post(self, request: Any) -> Any:
        from aiohttp import web
        from .const import DOMAIN

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        text_query = (body.get("text_query") or "").strip() or None
        positives = [s for s in (body.get("positives") or []) if isinstance(s, str) and s]
        negatives = [s for s in (body.get("negatives") or []) if isinstance(s, str) and s]

        try:
            top_k = max(1, min(int(body.get("top_k", 16)), 200))
        except (ValueError, TypeError):
            top_k = 16

        # Validation — mirrors the front-end gating, returns 400 on failure.
        if text_query is None:
            if len(positives) < 2:
                return web.json_response(
                    {"error": "Add at least 2 positives, or include a text query."},
                    status=400,
                )
            if len(positives) + len(negatives) < 3:
                return web.json_response(
                    {"error": "Add at least 3 images total, or include a text query."},
                    status=400,
                )

        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        store = data.get("qdrant_store")
        if store is None:
            return web.json_response(
                {"error": "Qdrant not configured"},
                status=503,
            )

        if text_query is not None:
            target_kwargs = {"target_text": text_query}
            pos_for_pairs = positives
        else:
            target_kwargs = {"target_filename": positives[0]}
            pos_for_pairs = positives[1:]

        try:
            results = await self._hass.async_add_executor_job(
                lambda: store.discover(
                    **target_kwargs,
                    positives=pos_for_pairs,
                    negatives=negatives,
                    top_k=top_k,
                )
            )
            return web.json_response({"results": results})
        except ValueError as err:
            return web.json_response({"error": str(err)}, status=400)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Discover failed: %s", err)
            return web.json_response({"error": str(err)}, status=500)


class ArtSearchUIView(HomeAssistantView):
    """Serves the search UI HTML page.

    GET /api/frame_art_shuffler/search-ui

    Reads search.html from the www/ subdirectory at startup and caches it.
    """

    url = "/api/frame_art_shuffler/search-ui"
    name = "api:frame_art_shuffler:search_ui"
    requires_auth = False

    def __init__(self) -> None:
        html_path = Path(__file__).parent / "www" / "search.html"
        try:
            self._html = html_path.read_text(encoding="utf-8")
        except OSError:
            self._html = "<h1>Search UI not found</h1><p>www/search.html is missing.</p>"

    async def get(self, request: Any) -> Any:
        from aiohttp import web
        return web.Response(text=self._html, content_type="text/html")
