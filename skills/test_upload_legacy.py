"""
Standalone upload smoke test against the LivingRoom Frame (10.0.0.104).

Bypasses Home Assistant entirely — talks straight to the TV via the
vendored samsungtvws library, taking the legacy WS-binary path that the
0.97 firmware needs. Resizes the payload first using the same logic the
integration uses.

Usage:
    /homeassistant/env_test/.venv/bin/python3 -m skills.test_upload_legacy <library_filename>

Example:
    /homeassistant/env_test/.venv/bin/python3 -m skills.test_upload_legacy basquiat-jean-michel-man-from-naples-1982-5fb168e5.jpg

Exits 0 on success (prints content_id), 1 on failure.
"""
from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/config/custom_components/frame_art_shuffler")
from samsungtvws.remote import SamsungTVWS  # noqa: E402

LIBRARY = Path("/config/www/frame_art/library")
TV_IP = "10.0.0.104"
TV_PORT = 8002
MAX_EDGE = 3840
TARGET_BYTES = 4 * 1024 * 1024
QUALITY_LADDER = (88, 82, 76, 70, 64)

GREEN = "\033[32m"; YELLOW = "\033[33m"; RED = "\033[31m"; CYAN = "\033[36m"; RESET = "\033[0m"


def prep(payload: bytes, file_type: str) -> tuple[bytes, str]:
    from PIL import Image
    if file_type.lower() in ("jpeg", "jpg") and len(payload) <= TARGET_BYTES:
        try:
            img = Image.open(io.BytesIO(payload)); img.load()
            if max(img.size) <= MAX_EDGE:
                return payload, "jpeg"
        except Exception:
            pass
    img = Image.open(io.BytesIO(payload)); img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > MAX_EDGE:
        ratio = MAX_EDGE / max(img.size)
        img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), Image.LANCZOS)
    for q in QUALITY_LADDER:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True, progressive=False)
        if len(buf.getvalue()) <= TARGET_BYTES:
            print(f"  resized → {img.size} q={q} bytes={len(buf.getvalue())}")
            return buf.getvalue(), "jpeg"
    print(f"  resized → {img.size} q={QUALITY_LADDER[-1]} bytes={len(buf.getvalue())} (over target)")
    return buf.getvalue(), "jpeg"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__); return 1
    fname = sys.argv[1]
    src = LIBRARY / fname
    if not src.exists():
        print(f"  {RED}✗{RESET} {src} does not exist"); return 1

    payload = src.read_bytes()
    print(f"\n{CYAN}1. Source{RESET}")
    print(f"  {fname} — {len(payload):,} bytes")

    print(f"\n{CYAN}2. Resize payload (legacy-friendly){RESET}")
    payload, file_type = prep(payload, "jpeg")

    print(f"\n{CYAN}3. Connect to TV {TV_IP}{RESET}")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", TV_IP)
    token = Path(f"/config/frame_art_shuffler/tokens/{safe}.token")
    tv = SamsungTVWS(host=TV_IP, port=TV_PORT, timeout=30,
                     token_file=str(token), name="FrameArtSmokeTest")
    art = tv.art(timeout=30)

    try:
        version = art.get_api_version()
        is_legacy = art._is_legacy_api()
        print(f"  api_version={version!r}  is_legacy={is_legacy}")
        if not is_legacy:
            print(f"  {YELLOW}⚠{RESET}  Expected legacy=True for 0.97 firmware")

        print(f"\n{CYAN}4. List current gallery{RESET}")
        before = art.available() or []
        before_ids = {a["content_id"] for a in before}
        print(f"  {len(before_ids)} unique content_ids on TV")

        print(f"\n{CYAN}5. Wake screen{RESET}")
        art.set_artmode("on")
        time.sleep(3)

        print(f"\n{CYAN}6. Upload {len(payload):,} bytes via legacy WS-binary path{RESET}")
        t0 = time.time()
        try:
            result = art.upload(payload, matte="none", portrait_matte="none", file_type="jpeg")
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  {RED}✗ FAILED{RESET} after {elapsed:.1f}s — {type(e).__name__}: {e}")
            return 1
        elapsed = time.time() - t0
        print(f"  result repr: {result!r}")
        cid = result.get("content_id") if isinstance(result, dict) else result
        print(f"  {GREEN}✓ UPLOADED{RESET} content_id={cid!r} in {elapsed:.1f}s")

        print(f"\n{CYAN}7. Verify gallery grew{RESET}")
        time.sleep(2)
        after = art.available() or []
        after_ids = {a["content_id"] for a in after}
        new = after_ids - before_ids
        print(f"  {len(after_ids)} unique content_ids on TV (was {len(before_ids)})")
        print(f"  new: {sorted(new)}")

        # Pick the content_id to verify with select_image. Prefer what the
        # upload returned; fall back to the one new entry in the gallery.
        verify_cid = cid
        if not verify_cid and len(new) == 1:
            verify_cid = next(iter(new))

        if not verify_cid:
            print(f"  {RED}✗{RESET} No content_id to verify — upload likely silently failed")
            return 1

        if verify_cid not in after_ids:
            print(f"  {RED}✗{RESET} {verify_cid!r} reported by upload but NOT in gallery")
            print(f"      → 0.97 firmware ACK-without-persist quirk; needs library fix")
            return 1

        print(f"\n{CYAN}8. Try to display via select_image({verify_cid!r}){RESET}")
        try:
            sel_result = art.select_image(verify_cid)
            print(f"  select_image returned: {sel_result!r}")
        except Exception as e:
            print(f"  {YELLOW}⚠{RESET}  select_image errored: {type(e).__name__}: {e}")
            print(f"      → uploaded but not addressable; persistence half-failed")
            return 1

        print(f"\n{CYAN}9. Verify the TV is now showing it{RESET}")
        time.sleep(3)
        try:
            current = art.get_current()
            current_cid = current.get("content_id") if isinstance(current, dict) else None
            print(f"  TV reports current_content_id={current_cid!r}")
            if current_cid == verify_cid:
                print(f"  {GREEN}✓{RESET} Confirmed: TV is displaying {verify_cid}")
                print(f"\n{GREEN}===== UPLOAD + DISPLAY SUCCESS ====={RESET}")
                return 0
            else:
                print(f"  {YELLOW}⚠{RESET}  Mismatch — TV showing {current_cid}, expected {verify_cid}")
                print(f"      Image is in gallery but not currently selected")
                return 0
        except Exception as e:
            print(f"  {YELLOW}⚠{RESET}  get_current errored: {type(e).__name__}: {e}")
            return 0
    finally:
        try: tv.close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
