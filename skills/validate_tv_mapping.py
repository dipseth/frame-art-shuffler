"""
Ad-hoc: validate content_id_map.json vs what the TV actually has, and probe
the TV for storage info.

Run: python3 -m skills.validate_tv_mapping

Reports:
  1. Map entries whose content_id is not on the TV  (stale mappings)
  2. TV content_ids not in our map                  (untracked / orphan)
  3. Library files with no mapping                  (never uploaded to TV)
  4. Per-category counts on the TV                  (MY / Samsung / etc.)
  5. Storage probe: device info + content sizes + best-effort quota
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

# --- load vendored samsungtvws from the integration ----------------------
_BASE = Path("/homeassistant/custom_components/frame_art_shuffler")
sys.path.insert(0, str(_BASE))
from samsungtvws.remote import SamsungTVWS  # noqa: E402

MAP_FILE = Path("/config/frame_art_shuffler/content_id_map.json")
META_FILE = Path("/homeassistant/www/frame_art/metadata.json")
LIBRARY_DIR = Path("/homeassistant/www/frame_art/library")
STORAGE_FILE = Path("/config/.storage/core.config_entries")
TOKEN_DIR = Path("/config/frame_art_shuffler/tokens")
DOMAIN = "frame_art_shuffler"

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"


def ok(msg): print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def section(t): print(f"\n{CYAN}{'─'*70}{RESET}\n{CYAN}{BOLD}{t}{RESET}\n{CYAN}{'─'*70}{RESET}")


def _token_path(ip: str) -> Path:
    safe = "".join(c if c.isalnum() else "_" for c in ip)
    return TOKEN_DIR / f"{safe}.token"


def _load_tv_configs() -> list[dict]:
    """Return [{id, ip, name}] from HA's config entry storage."""
    tvs: list[dict] = []
    if not STORAGE_FILE.exists():
        return tvs
    raw = json.loads(STORAGE_FILE.read_text())
    for entry in raw.get("data", {}).get("entries", []):
        if entry.get("domain") != DOMAIN:
            continue
        for tv_id, cfg in entry.get("data", {}).get("tvs", {}).items():
            tvs.append({"id": tv_id, "ip": cfg.get("ip"), "name": cfg.get("name", tv_id)})
    return tvs


def _query_tv(ip: str) -> dict:
    """Open a websocket, pull device_info + available() list. Returns dict."""
    tv = SamsungTVWS(
        host=ip,
        port=8002,
        timeout=15,
        token_file=str(_token_path(ip)),
        name="FrameArtValidate",
    )
    art = tv.art()
    try:
        device_info = art.get_device_info()
    except Exception as e:  # noqa: BLE001
        device_info = {"_error": str(e)}
    available = art.available()
    # Try a few speculative storage endpoints — some Frame firmwares answer,
    # most silently don't. Non-fatal.
    probes: dict[str, object] = {}
    for req in ("get_storage_info", "get_device_storage", "get_memory_info"):
        try:
            probes[req] = art._send_art_request({"request": req})
        except Exception as e:  # noqa: BLE001
            probes[req] = f"unsupported ({type(e).__name__})"
    try:
        tv.close()
    except Exception:
        pass
    return {"device_info": device_info, "available": available, "probes": probes}


def main() -> None:
    section("Sources")
    cmap_full: dict = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
    meta: dict = json.loads(META_FILE.read_text()) if META_FILE.exists() else {"images": {}}
    library_files = {p.name for p in LIBRARY_DIR.iterdir() if p.is_file()}
    tvs = _load_tv_configs()
    ok(f"content_id_map.json — TVs: {list(cmap_full.keys())}")
    ok(f"metadata.json — {len(meta.get('images', {}))} images")
    ok(f"library/ — {len(library_files)} files on disk")
    ok(f"HA config entry — {len(tvs)} TV(s): {[t['name'] for t in tvs]}")

    for tv in tvs:
        ip = tv["ip"]
        name = tv["name"]
        section(f"TV: {name} ({ip})")
        local_map: dict[str, str] = cmap_full.get(ip, {})
        print(f"  local map has {len(local_map)} filename → content_id mappings")

        try:
            tv_state = _query_tv(ip)
        except Exception as e:  # noqa: BLE001
            fail(f"Could not reach {name}: {e}")
            continue

        available = tv_state["available"]
        tv_cids = {a["content_id"]: a for a in available}
        print(f"  TV has {len(available)} items in its gallery")

        # -- category / vendor split ------------------------------------
        cats = Counter(a.get("category_id", "?") for a in available)
        vendors = Counter(a["content_id"].split("_")[0] for a in available)
        print(f"  by category: {dict(cats)}")
        print(f"  by content_id prefix (MY=user, SAM=Samsung): {dict(vendors)}")

        # -- check our mappings vs TV reality ---------------------------
        section(f"  [{name}] Mapping validation")

        # 1. local map entries whose CID no longer exists on TV
        stale = [(fn, cid) for fn, cid in local_map.items() if cid not in tv_cids]
        if stale:
            warn(f"{len(stale)} map entries point to content IDs that are NOT on the TV:")
            for fn, cid in sorted(stale, key=lambda x: x[1]):
                print(f"      {RED}{cid}{RESET}  →  {fn}   (file exists? {fn in library_files})")
        else:
            ok("every mapped content_id is present on the TV")

        # 2. local map entries whose local file is missing (library was cleaned)
        orphan_files = [(fn, cid) for fn, cid in local_map.items() if fn not in library_files]
        if orphan_files:
            warn(f"{len(orphan_files)} map entries whose local file is gone:")
            for fn, cid in sorted(orphan_files, key=lambda x: x[1]):
                on_tv = "still on TV" if cid in tv_cids else "also gone from TV"
                print(f"      {cid}  →  {fn}   ({on_tv})")
        else:
            ok("every mapped filename exists in library/")

        # 3. MY_F content_ids on the TV that we don't track locally
        our_cids = set(local_map.values())
        untracked_my = [
            cid for cid, a in tv_cids.items()
            if cid.startswith("MY") and cid not in our_cids
        ]
        if untracked_my:
            warn(f"{len(untracked_my)} user-uploaded content IDs on TV are NOT in our map:")
            for cid in sorted(untracked_my):
                a = tv_cids[cid]
                dims = f"{a.get('width','?')}x{a.get('height','?')}"
                print(f"      {cid}  ({dims}, cat={a.get('category_id')})")
        else:
            ok("every MY_* content_id on the TV is tracked locally")

        # 4. library files never uploaded to this TV
        mapped_fns = set(local_map.keys())
        unuploaded = sorted(library_files - mapped_fns)
        if unuploaded:
            warn(f"{len(unuploaded)} local files have no mapping for this TV:")
            for fn in unuploaded:
                print(f"      {fn}")
        else:
            ok("every library file has a mapping for this TV")

        # -- storage probe ---------------------------------------------
        section(f"  [{name}] Storage probe")
        dev = tv_state["device_info"]
        if "_error" in dev:
            warn(f"device_info unavailable: {dev['_error']}")
        else:
            # Print any size/capacity-sounding fields if present
            interesting = {
                k: v for k, v in (dev.items() if isinstance(dev, dict) else [])
                if any(w in k.lower() for w in ("size", "memory", "storage", "quota", "capacity", "free", "used"))
            }
            print(f"  device_info keys: {list(dev.keys()) if isinstance(dev, dict) else type(dev).__name__}")
            if interesting:
                ok(f"storage-looking fields: {interesting}")
            else:
                print("  (no storage fields surfaced in device_info — Samsung does not expose a direct quota)")

        # Known-unsupported probes (just for completeness)
        for req, resp in tv_state["probes"].items():
            if isinstance(resp, dict):
                ok(f"{req} → {resp}")
            # silent on unsupported

        # Best-effort used-space estimate: sum of file sizes on disk for
        # content IDs currently on the TV.
        used_bytes = 0
        counted = 0
        for fn, cid in local_map.items():
            if cid in tv_cids and (LIBRARY_DIR / fn).exists():
                used_bytes += (LIBRARY_DIR / fn).stat().st_size
                counted += 1
        used_mb = used_bytes / 1024 / 1024
        print(
            f"  approx. space used by MY_* art (from local file sizes): "
            f"{BOLD}{used_mb:.1f} MB{RESET} across {counted} mapped files"
        )
        print(
            "  Samsung Frame TVs historically cap personal art at ~500 images / ~5 GB; "
            "the firmware does not publish an exact quota"
        )


if __name__ == "__main__":
    main()
