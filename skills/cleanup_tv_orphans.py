"""
Clean up MY_F* content IDs that exist on the TV but aren't in our
content_id_map.json. These accumulate when images were deleted from the
Frame Art Manager library without the TV copy being removed (exactly what
we just fixed for new deletes — this handles historical drift).

For each untracked content_id:
  1. Fetch the TV thumbnail via samsungtvws art.get_thumbnail
  2. Embed via Jina and look it up in Qdrant (find_by_thumbnail)
  3. Classify:
       - "safe duplicate"     — Qdrant match ≥0.75 AND the matched file
                                 still exists in library/  (we still have
                                 the image, TV can drop it)
       - "ambiguous"          — Qdrant match 0.50-0.75
       - "unknown"            — Qdrant match <0.50
  4. Report, optionally delete

Modes:
  (no flag)        dry-run — report only
  --delete-safe    delete TV copies classified "safe duplicate"
  --delete-all     delete EVERY untracked content_id (nuclear)
  -y / --yes       skip the final confirmation prompt

Run (from inside HA container so qdrant-client + PIL are available):
    docker exec -it homeassistant python3 -m skills.cleanup_tv_orphans
    docker exec -it homeassistant python3 -m skills.cleanup_tv_orphans --delete-safe
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re as _re
import sys
import time
from pathlib import Path


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qs_mod = _load_module(
    "qdrant_store",
    "/config/custom_components/frame_art_shuffler/qdrant_store.py",
)
get_store = qs_mod.get_store

sys.path.insert(0, "/config/custom_components/frame_art_shuffler")
from samsungtvws.remote import SamsungTVWS as _SamsungTVWS  # noqa: E402


MAP_FILE = Path("/config/frame_art_shuffler/content_id_map.json")
LIBRARY_DIR = Path("/config/www/frame_art/library")
TV_IP = "10.0.0.104"  # LivingRoom Frame
TV_PORT = 8002
SAFE_SCORE = 0.75
AMBIGUOUS_SCORE = 0.50

_safe_ip = _re.sub(r"[^A-Za-z0-9]+", "_", TV_IP)
TOKEN_FILE = Path(f"/config/frame_art_shuffler/tokens/{_safe_ip}.token")

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BOLD = "\033[1m"


def ok(m): print(f"  {GREEN}✓{RESET} {m}")
def warn(m): print(f"  {YELLOW}⚠{RESET}  {m}")
def fail(m): print(f"  {RED}✗{RESET} {m}")
def section(t): print(f"\n{CYAN}{'─'*70}{RESET}\n{CYAN}{BOLD}{t}{RESET}\n{CYAN}{'─'*70}{RESET}")


def _classify(score: float, matched_file: str) -> str:
    if score >= SAFE_SCORE and (LIBRARY_DIR / matched_file).exists():
        return "safe duplicate"
    if score >= AMBIGUOUS_SCORE:
        return "ambiguous"
    return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--delete-safe", action="store_true",
                      help="delete TV copies classified as safe duplicates")
    mode.add_argument("--delete-all", action="store_true",
                      help="delete EVERY untracked content_id (dangerous)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the final confirmation prompt")
    args = ap.parse_args()

    cmap = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
    known_cids = set(cmap.get(TV_IP, {}).values())

    section("1. Connect to TV & list gallery")
    remote = _SamsungTVWS(
        host=TV_IP, port=TV_PORT, timeout=25,
        token_file=str(TOKEN_FILE), name="FrameArtCleanup",
    )
    art = remote.art(timeout=25)

    try:
        available = art.available() or []
        all_cids = {img["content_id"]: img for img in available if img.get("content_id")}
        ok(f"TV reports {len(all_cids)} items")
        untracked = {
            cid: info for cid, info in all_cids.items()
            if cid not in known_cids and cid.startswith("MY")
        }
        if not untracked:
            ok("No untracked MY_F* content IDs — nothing to clean up")
            return
        warn(f"{len(untracked)} untracked MY_F* content IDs on TV")

        section("2. Match each thumbnail against Qdrant")
        store = get_store()
        rows: list[dict] = []

        for cid in sorted(untracked):
            info = untracked[cid]
            dims = f"{info.get('width','?')}x{info.get('height','?')}"
            try:
                thumb = art.get_thumbnail(cid)
                if isinstance(thumb, dict):
                    thumb_bytes = bytes(next(iter(thumb.values()))) if thumb else None
                elif thumb is not None:
                    thumb_bytes = bytes(thumb)
                else:
                    thumb_bytes = None

                if not thumb_bytes:
                    fail(f"{cid}: empty thumbnail")
                    rows.append({"cid": cid, "dims": dims, "top_file": None,
                                 "score": 0.0, "klass": "unknown"})
                    continue

                hits = store.find_by_thumbnail(thumb_bytes, top_k=3)
                if not hits:
                    rows.append({"cid": cid, "dims": dims, "top_file": None,
                                 "score": 0.0, "klass": "unknown"})
                    continue

                top_file, top_score = hits[0]
                klass = _classify(top_score, top_file)
                rows.append({
                    "cid": cid, "dims": dims,
                    "top_file": top_file, "score": top_score,
                    "klass": klass, "hits": hits,
                })
                colour = (GREEN if klass == "safe duplicate"
                          else YELLOW if klass == "ambiguous"
                          else RED)
                print(f"  {colour}{klass:16s}{RESET}  {cid:<10} {dims:>12}  "
                      f"→ {top_file} ({top_score:.3f})")
                time.sleep(1.5)  # Jina rate-limit courtesy
            except Exception as e:  # noqa: BLE001
                fail(f"{cid}: {e}")
                rows.append({"cid": cid, "dims": dims, "top_file": None,
                             "score": 0.0, "klass": "unknown"})

        # ── summary ───────────────────────────────────────────────────
        section("3. Summary")
        counts = {"safe duplicate": 0, "ambiguous": 0, "unknown": 0}
        for r in rows:
            counts[r["klass"]] = counts.get(r["klass"], 0) + 1
        print(f"  safe duplicate: {counts['safe duplicate']}   "
              f"ambiguous: {counts['ambiguous']}   "
              f"unknown: {counts['unknown']}")

        # ── deletion ──────────────────────────────────────────────────
        if not (args.delete_safe or args.delete_all):
            section("4. Dry-run complete")
            print("  Re-run with --delete-safe to remove confirmed duplicates,")
            print("  or --delete-all to remove every untracked item (dangerous).")
            return

        to_delete = [
            r for r in rows
            if (args.delete_all or r["klass"] == "safe duplicate")
        ]
        if not to_delete:
            ok("Nothing selected for deletion.")
            return

        section(f"4. Will delete {len(to_delete)} content_id(s) from the TV")
        for r in to_delete:
            print(f"  {RED}DEL{RESET} {r['cid']:<10} {r['dims']:>12}  "
                  f"(matched {r['top_file']} @ {r['score']:.3f}, {r['klass']})")

        if not args.yes:
            resp = input("\n  Proceed? [y/N] ").strip().lower()
            if resp != "y":
                print("  aborted")
                return

        for r in to_delete:
            try:
                art.delete(r["cid"])
                ok(f"deleted {r['cid']}")
                time.sleep(0.8)
            except Exception as e:  # noqa: BLE001
                fail(f"{r['cid']}: {e}")

    finally:
        for conn in (art, remote):
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
