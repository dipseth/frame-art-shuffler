"""
Seed the Qdrant 'frame_art' collection from metadata.json + content_id_map.json.

Each image is embedded using its actual JPEG/PNG file via jina-clip-v2 so that
later TV thumbnail lookups land in the same vector space.

Run:  python3 -m skills.seed_qdrant
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import importlib.util

def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

qdrant_store = _load_module(
    "qdrant_store",
    "/homeassistant/custom_components/frame_art_shuffler/qdrant_store.py",
)
get_store = qdrant_store.get_store

META_FILE = Path("/homeassistant/www/frame_art/metadata.json")
MAP_FILE = Path("/config/frame_art_shuffler/content_id_map.json")
LIBRARY_DIR = Path("/homeassistant/www/frame_art/library")


def _read_image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Read actual pixel dimensions from an image file."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.width, img.height
    except Exception:
        return None, None

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"


def ok(msg): print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET} {msg}")
def section(t): print(f"\n{CYAN}{'─'*60}{RESET}\n{CYAN}{t}{RESET}\n{CYAN}{'─'*60}{RESET}")


section("Connecting to Qdrant")
store = get_store()
before = store.count()
print(f"  Collection 'frame_art': {before} points before seed")

section("Loading sources")
meta = json.loads(META_FILE.read_text()) if META_FILE.exists() else {"images": {}}
cid_map = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
images = meta.get("images", {})
print(f"  metadata.json: {len(images)} images")
print(f"  content_id_map.json: {sum(len(v) for v in cid_map.values())} total mappings")

section("Seeding images")
skipped = []
failed = []

meta_updated = False
for filename, info in sorted(images.items()):
    image_path = LIBRARY_DIR / filename
    tags = info.get("tags", [])
    matte = info.get("matte", "none")
    filter_type = info.get("filter", "none")
    width = info.get("width")
    height = info.get("height")

    # Auto-populate missing dimensions from actual image file
    if (width is None or height is None) and image_path.exists():
        width, height = _read_image_dimensions(image_path)
        if width and height:
            info["width"] = width
            info["height"] = height
            meta_updated = True

    # Gather content IDs across all TVs
    tv_content_ids: dict[str, str] = {}
    for tv_ip, tv_map in cid_map.items():
        if filename in tv_map:
            tv_content_ids[tv_ip] = tv_map[filename]
    # Also from metadata.json tvContentIds
    for tv_ip, cid in info.get("tvContentIds", {}).items():
        tv_content_ids.setdefault(tv_ip, cid)

    if not image_path.exists():
        warn(f"{filename}: file missing, using text embedding")
        image_path = None  # fall back to text

    success = False
    for attempt in range(4):
        try:
            store.upsert_image(
                filename,
                tags=tags,
                matte=matte,
                filter_type=filter_type,
                width=width,
                height=height,
                tv_content_ids=tv_content_ids,
                image_path=image_path,
            )
            src = "image" if image_path else "text "
            cids = ", ".join(f"{ip}→{c}" for ip, c in tv_content_ids.items()) or "none"
            ok(f"[{src}] {filename}  ({len(tags)} tags, content_ids: {cids})")
            success = True
            break
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = 5 * (attempt + 1)
                warn(f"{filename}: rate limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                fail(f"{filename}: {e}")
                failed.append(filename)
                break
    if success:
        time.sleep(1.5)  # be kind to Jina rate limits

if meta_updated:
    META_FILE.write_text(json.dumps(meta, indent=2))
    ok(f"Backfilled width/height in metadata.json for images missing dimensions")

after = store.count()
section("Result")
print(f"  Points before: {before}")
print(f"  Points after:  {after}")
print(f"  Added/updated: {after - before + len(failed)}")
if failed:
    warn(f"Failed: {failed}")

section("Quick smoke-test: semantic search")
tests = [
    "pink cherry blossom trees in spring",
    "stars night sky astronomy",
    "sand dunes dry desert",
]
for query in tests:
    try:
        time.sleep(1.5)
        hits = store.search_by_text(query, top_k=3)
        hit_str = ", ".join(f"{f} ({s:.3f})" for f, s in hits)
        print(f"  '{query}'\n    → {hit_str}")
    except Exception as e:
        warn(f"search '{query}': {e}")

print(f"\n{CYAN}{'─'*60}{RESET}")
print("Done. Run python3 -m skills.test_qdrant for more detailed checks.")
print(f"{CYAN}{'─'*60}{RESET}\n")
