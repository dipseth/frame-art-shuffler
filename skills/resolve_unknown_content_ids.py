"""
Resolve unknown TV content IDs by fetching their thumbnails and matching
them against the Qdrant 'frame_art' collection via jina-clip-v2 embeddings.

For each unresolved content_id on the TV:
  1. Fetch the thumbnail from the TV (via samsungtvws get_thumbnail)
  2. Embed the thumbnail via Jina API
  3. Find the nearest image in Qdrant
  4. If confident (score ≥ 0.75), write the mapping to:
       - content_id_map.json
       - metadata.json tvContentIds
       - Qdrant payload
  5. Print a table of results for manual review of low-confidence hits

Run:  python3 -m skills.resolve_unknown_content_ids
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import re as _re
import sys
import time
from pathlib import Path


def _atomic_json_update(path: Path, updater) -> None:
    """Read → update → write a JSON file under an exclusive lock."""
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            updater(data)
            path.write_text(json.dumps(data, indent=2))
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

# ── Load qdrant_store ─────────────────────────────────────────────────────
def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

qs_mod = _load_module(
    "qdrant_store",
    "/homeassistant/custom_components/frame_art_shuffler/qdrant_store.py",
)
get_store = qs_mod.get_store

# ── Load vendored samsungtvws ─────────────────────────────────────────────
sys.path.insert(0, "/homeassistant/custom_components/frame_art_shuffler")
from samsungtvws.remote import SamsungTVWS as _SamsungTVWS

MAP_FILE   = Path("/config/frame_art_shuffler/content_id_map.json")
META_FILE  = Path("/homeassistant/www/frame_art/metadata.json")
TV_IP      = "10.0.0.104"
TV_PORT    = 8002
CONFIDENCE = 0.75

_safe_ip   = _re.sub(r"[^A-Za-z0-9]+", "_", TV_IP)
TOKEN_FILE = Path(f"/config/frame_art_shuffler/tokens/{_safe_ip}.token")

RESET = "\033[0m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
RED   = "\033[31m"; CYAN  = "\033[36m"
def ok(m):      print(f"  {GREEN}✓{RESET} {m}")
def warn(m):    print(f"  {YELLOW}⚠{RESET}  {m}")
def fail(m):    print(f"  {RED}✗{RESET} {m}")
def section(t): print(f"\n{CYAN}{'─'*60}{RESET}\n{CYAN}{t}{RESET}\n{CYAN}{'─'*60}{RESET}")

# ── Load known mappings ───────────────────────────────────────────────────
section("1. Find unresolved content IDs")
cid_map = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
known_cids = set(cid_map.get(TV_IP, {}).values())
print(f"  Known content_ids: {sorted(known_cids)}")

# ── Single TV session: gallery + thumbnails ───────────────────────────────
section("2. Query TV gallery + fetch thumbnails (single session)")
print(f"  Connecting to {TV_IP}…")

store = get_store()
auto_resolved: dict[str, str] = {}
low_confidence: list[tuple] = []

remote = _SamsungTVWS(
    host=TV_IP, port=TV_PORT, timeout=25,
    token_file=str(TOKEN_FILE), name="FrameArtResolver",
)
art = remote.art(timeout=25)

try:
    # Gallery listing
    available = art.available() or []
    all_cids = {img["content_id"]: img for img in available if img.get("content_id")}
    ok(f"TV reports {len(all_cids)} images: {sorted(all_cids.keys())}")

    unknown_cids = {cid: info for cid, info in all_cids.items() if cid not in known_cids}
    if not unknown_cids:
        ok("No unresolved content IDs — everything is mapped!")
        sys.exit(0)
    warn(f"{len(unknown_cids)} unresolved: {sorted(unknown_cids.keys())}")

    # Thumbnails — same art session, no reconnect
    section("3. Fetch thumbnails & match via Qdrant")

    for cid in sorted(unknown_cids):
        print(f"\n  Fetching thumbnail for {cid}…")
        try:
            thumb_raw = art.get_thumbnail(cid)

            # get_thumbnail returns bytearray | dict | None depending on version
            if isinstance(thumb_raw, dict):
                thumb_bytes = bytes(next(iter(thumb_raw.values()))) if thumb_raw else None
            elif thumb_raw is not None:
                thumb_bytes = bytes(thumb_raw)
            else:
                thumb_bytes = None

            if not thumb_bytes:
                fail(f"  {cid}: empty thumbnail")
                continue

            ok(f"  Got {len(thumb_bytes):,} bytes")
            time.sleep(1.5)

            hits = store.find_by_thumbnail(thumb_bytes, top_k=5)
            if not hits:
                fail(f"  {cid}: Qdrant returned no results")
                continue

            top_file, top_score = hits[0]
            print(f"  Top matches: {', '.join(f'{f}({s:.3f})' for f, s in hits[:3])}")

            if top_score >= CONFIDENCE:
                auto_resolved[cid] = top_file
                ok(f"  AUTO-MAPPED: {cid} → {top_file} (score={top_score:.3f})")
            else:
                low_confidence.append((cid, top_file, top_score, hits))
                warn(f"  LOW CONFIDENCE: {cid} → {top_file}? ({top_score:.3f} < {CONFIDENCE})")

        except Exception as e:
            import traceback
            fail(f"  {cid}: {e}")
            traceback.print_exc()

finally:
    for conn in (art, remote):
        try: conn.close()
        except Exception: pass

# ── Persist auto-resolved ─────────────────────────────────────────────────
if auto_resolved:
    section("4. Persisting auto-resolved mappings")

    def _update_map(data):
        tv = data.setdefault(TV_IP, {})
        for cid, fname in auto_resolved.items():
            tv.setdefault(fname, cid)  # skip-if-present

    def _update_meta(data):
        images = data.get("images", {})
        for cid, fname in auto_resolved.items():
            if fname in images:
                images[fname].setdefault("tvContentIds", {})[TV_IP] = cid

    _atomic_json_update(MAP_FILE, _update_map)
    ok(f"Updated content_id_map.json (+{len(auto_resolved)} entries, locked)")

    if META_FILE.exists():
        _atomic_json_update(META_FILE, _update_meta)
        ok("Updated metadata.json tvContentIds (locked)")

    for cid, fname in auto_resolved.items():
        store.update_content_id(fname, TV_IP, cid)
    ok("Updated Qdrant payload")
else:
    section("4. No auto-resolved mappings")

# ── Manual review ─────────────────────────────────────────────────────────
if low_confidence:
    section("5. Manual review needed")
    for cid, top_file, top_score, hits in low_confidence:
        print(f"\n  {YELLOW}{cid}{RESET} (best guess: {top_file}, score={top_score:.3f})")
        for i, (f, s) in enumerate(hits[:3]):
            marker = GREEN + "→" + RESET if i == 0 else " "
            print(f"    {marker} {f:40s} {s:.4f}")

print(f"\n{CYAN}{'─'*60}{RESET}")
print(f"Done.  Auto-resolved: {len(auto_resolved)},  Need review: {len(low_confidence)}")
print(f"{CYAN}{'─'*60}{RESET}\n")
