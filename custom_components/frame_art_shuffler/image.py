"""Image platform for Frame Art Shuffler.

One ImageEntity per configured TV, showing the artwork currently displayed.
Image bytes are read from /config/www/frame_art/library/<filename>, driven by
the actual_filename field that binary_sensor.py's 30 s poll populates.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Iterable

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from .binary_sensor import SIGNAL_TV_ACTUAL_STATE
from .config_entry import get_tv_config
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

LIBRARY_DIR = Path("/config/www/frame_art/library")

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up one image entity per configured TV."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]

    tracked: set[str] = set()

    @callback
    def _process_tvs(tvs: Iterable[dict[str, Any]]) -> None:
        new_entities: list[ImageEntity] = []
        for tv in tvs:
            tv_id = tv.get("id")
            if not tv_id or tv_id in tracked:
                continue
            tracked.add(tv_id)
            new_entities.append(FrameArtCurrentImageEntity(hass, entry, tv_id))
        if new_entities:
            async_add_entities(new_entities)

    _process_tvs(coordinator.data or [])

    @callback
    def _handle_coordinator_update() -> None:
        _process_tvs(coordinator.data or [])

    unsubscribe = coordinator.async_add_listener(_handle_coordinator_update)
    entry.async_on_unload(unsubscribe)


class FrameArtCurrentImageEntity(ImageEntity):
    """Image entity reflecting the artwork currently on the Frame TV."""

    _attr_has_entity_name = True
    _attr_name = "Current Image"
    _attr_icon = "mdi:image-frame"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        super().__init__(hass)
        self._hass = hass
        self._entry = entry
        self._tv_id = tv_id
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_current_image"
        self._unsubscribe: Callable[[], None] | None = None
        self._current_filename: str | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        # Prime from cache now that hass is attached.
        self._sync_from_cache()

        @callback
        def _updated() -> None:
            if self._sync_from_cache():
                self.async_write_ha_state()

        self._unsubscribe = async_dispatcher_connect(
            self._hass,
            f"{SIGNAL_TV_ACTUAL_STATE}_{self._entry.entry_id}_{self._tv_id}",
            _updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def available(self) -> bool:
        return get_tv_config(self._entry, self._tv_id) is not None

    def _sync_from_cache(self) -> bool:
        """Pick up the latest actual_filename from the poll cache.

        Returns True if the filename changed (so the caller can write state).
        """
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        cache = data.get("tv_status_cache", {}).get(self._tv_id, {})
        filename = cache.get("actual_filename")
        if filename == self._current_filename:
            return False

        self._current_filename = filename
        if filename:
            self._attr_content_type = _CONTENT_TYPES.get(
                Path(filename).suffix.lower(), "image/jpeg"
            )
            self._attr_image_last_updated = dt_util.utcnow()
        return True

    async def async_image(self) -> bytes | None:
        filename = self._current_filename
        if not filename:
            return None
        path = LIBRARY_DIR / filename
        try:
            return await self._hass.async_add_executor_job(path.read_bytes)
        except FileNotFoundError:
            _LOGGER.debug("Frame art image not found: %s", path)
            return None
        except OSError as err:
            _LOGGER.warning("Failed to read frame art image %s: %s", path, err)
            return None
