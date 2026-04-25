"""
Qdrant frame_art collection diagnostic.

Tests:
  1. Collection health (count, payload completeness)
  2. Exact content_id lookup by TV IP
  3. Semantic text search
  4. Thumbnail-based image resolution (the key capability for unknown content IDs)

Run:  python3 -m skills.test_qdrant
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

qdrant_store_mod = _load_module(
    "qdrant_store",
    "/homeassistant/custom_components/frame_art_shuffler/qdrant_store.py",
)
get_store = qdrant_store_mod.get_store

LIBRARY_DIR = Path("/homeassistant/www/frame_art/library")
MAP_FILE = Path("/config/frame_art_shuffler/content_id_map.json")
TV_IP = "10.0.0.104"

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"

def ok(msg): print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def section(t): print(f"\n{CYAN}{'─'*60}{RESET}\n{CYAN}{t}{RESET}\n{CYAN}{'─'*60}{RESET}")


section("1. Connect & collection health")
store = get_store()
count = store.count()
ok(f"Connected — {count} points in 'frame_art'")

all_points = store.list_all()
with_cids = sum(1 for p in all_points if p.get("tv_content_ids"))
no_cids = [p["filename"] for p in all_points if not p.get("tv_content_ids")]
ok(f"{with_cids}/{count} images have TV content_id mappings")
if no_cids:
    warn(f"No content_id yet: {no_cids}")

section("2. Exact content_id lookup")
cid_map = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
tv_map = cid_map.get(TV_IP, {})

all_pass = True
for filename, cid in sorted(tv_map.items(), key=lambda x: x[1]):
    found = store.find_by_content_id(TV_IP, cid)
    if found == filename:
        ok(f"{cid} → {filename}")
    elif found:
        fail(f"{cid} → got '{found}', expected '{filename}'")
        all_pass = False
    else:
        fail(f"{cid}: not found in Qdrant (run seed_qdrant to populate)")
        all_pass = False

if all_pass and tv_map:
    ok("All content_id lookups passed")

section("3. Semantic text search")
queries = [
    ("ocean waves water aerial", "ocean_waves_aerial.jpg"),
    ("mountain lake sunset reflection", "mountain_lake_sunset.jpg"),
    ("northern lights aurora night", "northern_lights.jpg"),
    ("lavender purple field flowers", "lavender_field.jpg"),
    ("abstract paint swirl colorful", "abstract_paint_swirl.jpg"),
]

for query, expected in queries:
    try:
        time.sleep(1.0)
        hits = store.search_by_text(query, top_k=3)
        top_file, top_score = hits[0] if hits else ("none", 0)
        if top_file == expected:
            ok(f"'{query}'\n      → {top_file} ({top_score:.3f}) ✓")
        else:
            hit_str = ", ".join(f"{f}({s:.3f})" for f, s in hits)
            warn(f"'{query}'\n      → {hit_str} (expected {expected})")
    except Exception as e:
        fail(f"search error: {e}")

section("4. Thumbnail-based resolution (self-test)")
# Embed each library image and check it resolves back to itself
# This validates the image→vector→search round-trip
errors = []
tested = 0
for img_path in sorted(LIBRARY_DIR.glob("*.jpg"))[:6]:  # test first 6 to limit API calls
    try:
        time.sleep(1.5)
        img_bytes = img_path.read_bytes()
        hits = store.find_by_thumbnail(img_bytes, top_k=3)
        top_file, top_score = hits[0] if hits else ("none", 0)
        if top_file == img_path.name:
            ok(f"{img_path.name} → self ({top_score:.3f}) ✓")
        else:
            runner_up = ", ".join(f"{f}({s:.2f})" for f, s in hits[1:])
            warn(f"{img_path.name} → top={top_file}({top_score:.3f}), others={runner_up}")
            errors.append(img_path.name)
        tested += 1
    except Exception as e:
        fail(f"{img_path.name}: {e}")

if not errors:
    ok(f"All {tested} thumbnail self-tests passed")
else:
    warn(f"{len(errors)}/{tested} thumbnail self-tests had mismatches: {errors}")

section("5. find_by_filename spot-check")
for fn in ["cherry_blossoms.jpg", "starry_night_sky.jpg"]:
    payload = store.find_by_filename(fn)
    if payload:
        tags = payload.get("tags", [])
        cids = payload.get("tv_content_ids", {})
        ok(f"{fn}: {len(tags)} tags, content_ids={cids}")
    else:
        fail(f"{fn}: not found")

print(f"\n{CYAN}{'─'*60}{RESET}")
print("Done.")
print(f"{CYAN}{'─'*60}{RESET}\n")
