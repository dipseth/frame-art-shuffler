"""
Test script: content_id_map sync system.

Tests three things without requiring HA to be running:
  1. JSON file state (current content_id_map.json)
  2. entry.data state (what's in .storage/core.config_entries)
  3. metadata.json tvContentIds alignment

Run: python3 -m skills.test_content_id_sync
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAP_FILE = Path("/config/frame_art_shuffler/content_id_map.json")
META_FILE = Path("/homeassistant/www/frame_art/metadata.json")
STORAGE_FILE = Path("/config/.storage/core.config_entries")
DOMAIN = "frame_art_shuffler"
TV_IP = "10.0.0.104"

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET}  {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def section(title: str) -> None:
    print(f"\n{CYAN}{'─' * 60}{RESET}")
    print(f"{CYAN}{title}{RESET}")
    print(f"{CYAN}{'─' * 60}{RESET}")


# ── Load sources ──────────────────────────────────────────────

section("1. JSON file  →  content_id_map.json")
json_map: dict[str, str] = {}
if MAP_FILE.exists():
    raw = json.loads(MAP_FILE.read_text())
    json_map = raw.get(TV_IP, {})
    ok(f"Found {len(json_map)} entries for {TV_IP}")
    for fname, cid in sorted(json_map.items(), key=lambda x: x[1]):
        print(f"      {cid}  →  {fname}")
else:
    fail(f"File not found: {MAP_FILE}")

section("2. HA entry.data  →  core.config_entries")
entry_map: dict[str, str] = {}
entry_data: dict = {}
if STORAGE_FILE.exists():
    storage = json.loads(STORAGE_FILE.read_text())
    for e in storage.get("data", {}).get("entries", []):
        if e.get("domain") == DOMAIN:
            entry_data = e.get("data", {})
            entry_map = entry_data.get("content_id_maps", {}).get(TV_IP, {})
            if entry_map:
                ok(f"entry.data has {len(entry_map)} entries for {TV_IP}")
                for fname, cid in sorted(entry_map.items(), key=lambda x: x[1]):
                    print(f"      {cid}  →  {fname}")
            else:
                warn(
                    "entry.data has NO content_id_maps yet — "
                    "will be populated on next HA restart"
                )
            break
else:
    fail(f"Storage file not found: {STORAGE_FILE}")

section("3. metadata.json  →  tvContentIds")
meta_map: dict[str, str] = {}
if META_FILE.exists():
    meta = json.loads(META_FILE.read_text())
    for fname, info in meta.get("images", {}).items():
        cid = info.get("tvContentIds", {}).get(TV_IP)
        if cid:
            meta_map[fname] = cid
    if meta_map:
        ok(f"metadata.json has tvContentIds for {len(meta_map)} images")
        for fname, cid in sorted(meta_map.items(), key=lambda x: x[1]):
            print(f"      {cid}  →  {fname}")
    else:
        warn("No tvContentIds in metadata.json yet")
else:
    fail(f"File not found: {META_FILE}")

# ── Cross-checks ─────────────────────────────────────────────

section("4. Alignment checks")

# JSON vs entry.data
if entry_map:
    only_json = {f: c for f, c in json_map.items() if f not in entry_map}
    only_entry = {f: c for f, c in entry_map.items() if f not in json_map}
    mismatch = {f for f in json_map if f in entry_map and json_map[f] != entry_map[f]}

    if not only_json and not only_entry and not mismatch:
        ok("JSON file and entry.data are perfectly in sync")
    if only_json:
        warn(f"In JSON but NOT in entry.data (will sync on restart): {list(only_json)}")
    if only_entry:
        warn(f"In entry.data but NOT in JSON: {list(only_entry)}")
    if mismatch:
        fail(f"CONTENT_ID MISMATCH between JSON and entry.data: {list(mismatch)}")
else:
    warn("Skipping JSON vs entry.data check (entry.data not yet bootstrapped)")

# metadata.json tvContentIds vs JSON map
if meta_map:
    meta_conflicts = {
        f for f in meta_map
        if f in json_map and meta_map[f] != json_map[f]
    }
    if meta_conflicts:
        fail(f"metadata.json tvContentIds conflict with JSON map: {list(meta_conflicts)}")
    else:
        ok("metadata.json tvContentIds consistent with JSON map")

section("5. Library files without any mapping")
lib_dir = META_FILE.parent / "library"
if lib_dir.exists() and META_FILE.exists():
    meta = json.loads(META_FILE.read_text())
    all_meta_images = set(meta.get("images", {}).keys())
    mapped = set(json_map.keys())
    unmapped = all_meta_images - mapped
    if unmapped:
        warn(f"{len(unmapped)} images in metadata.json with no content_id mapping:")
        for f in sorted(unmapped):
            print(f"      {f}")
    else:
        ok("All metadata.json images are mapped")

    orphan_files = [
        f.name for f in lib_dir.iterdir()
        if f.suffix.lower() in {".jpg", ".jpeg", ".png"}
        and f.name not in all_meta_images
    ]
    if orphan_files:
        warn(f"{len(orphan_files)} files in library/ not in metadata.json (orphans):")
        for f in sorted(orphan_files):
            print(f"      {f}")
    else:
        ok("No orphaned files in library/")

print(f"\n{CYAN}{'─' * 60}{RESET}")
print("Done. Run again after HA restart to verify entry.data is populated.")
print(f"{CYAN}{'─' * 60}{RESET}\n")
