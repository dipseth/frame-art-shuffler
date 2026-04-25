"""Ad-hoc test script for Frame TV control and token persistence.

Run from /homeassistant as:
    python3 -m skills.test_frame_tv <command> [args]

Commands:
    status              Show TV screen state and art mode
    token               Verify token file exists and is non-empty
    wake                Send Wake-on-LAN and wait for TV to come up (~30s)
    artmode             Set TV to art mode
    list                List images currently stored on the TV
    select <content_id> Select an existing image by content ID
    display <path>      Full display_art flow (select or upload)
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Make the custom_components package importable from /homeassistant
# ---------------------------------------------------------------------------
_HA_ROOT = Path(__file__).resolve().parent.parent
if str(_HA_ROOT) not in sys.path:
    sys.path.insert(0, str(_HA_ROOT))

# ---------------------------------------------------------------------------
# Import frame_tv directly (bypasses custom_components/__init__.py which
# requires the full HA runtime / voluptuous / etc.)
# ---------------------------------------------------------------------------
import types
import importlib.util as _ilu

_PKG_NAME = "custom_components.frame_art_shuffler"
_PKG_DIR = _HA_ROOT / "custom_components" / "frame_art_shuffler"

# Step 1: Add frame_art_shuffler dir to sys.path so `import samsungtvws`
#         works via the fallback path in frame_tv.py's try/except block.
_comp_dir = str(_PKG_DIR)
if _comp_dir not in sys.path:
    sys.path.insert(0, _comp_dir)

# Step 2: Register stub package hierarchy so relative imports resolve:
#   from .const import ... → custom_components.frame_art_shuffler.const
#   from . import samsungtvws → custom_components.frame_art_shuffler.samsungtvws
for _stub_name, _stub_path in [
    ("custom_components", str(_HA_ROOT / "custom_components")),
    (_PKG_NAME, str(_PKG_DIR)),
]:
    if _stub_name not in sys.modules:
        stub = types.ModuleType(_stub_name)
        stub.__path__ = [_stub_path]
        stub.__package__ = _stub_name
        sys.modules[_stub_name] = stub


def _load_file_as_module(full_name: str, file_path: Path, package: str) -> types.ModuleType:
    """Load a .py file and register it as a module with the given full_name."""
    spec = _ilu.spec_from_file_location(full_name, file_path)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    mod.__package__ = package
    sys.modules[full_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Step 3: Load const.py (needed by frame_tv's `from .const import ...`)
_load_file_as_module(f"{_PKG_NAME}.const", _PKG_DIR / "const.py", _PKG_NAME)

# Step 4: Load the samsungtvws package. Since _comp_dir is in sys.path,
#         `import samsungtvws` will work. Load it now so we can alias it.
import samsungtvws as _tvws  # noqa: E402 — loaded from _comp_dir/samsungtvws/

# Alias every already-loaded samsungtvws.* module under the package namespace
# so that frame_tv's relative import `from . import samsungtvws` resolves too.
_tvws_alias = f"{_PKG_NAME}.samsungtvws"
if _tvws_alias not in sys.modules:
    sys.modules[_tvws_alias] = _tvws
for _k, _v in list(sys.modules.items()):
    if _k.startswith("samsungtvws."):
        _alias_k = f"{_PKG_NAME}.{_k}"
        if _alias_k not in sys.modules:
            sys.modules[_alias_k] = _v

# Step 5: Finally load frame_tv itself
frame_tv = _load_file_as_module(f"{_PKG_NAME}.frame_tv", _PKG_DIR / "frame_tv.py", _PKG_NAME)

TV_IP = "10.0.0.104"
TOKEN_PATH = Path("/config/frame_art_shuffler/tokens/10_0_0_104.token")

# ---- helpers ----------------------------------------------------------------

def _ok(msg: str) -> None:
    print(f"  ✓  {msg}")

def _fail(msg: str) -> None:
    print(f"  ✗  {msg}")

def _info(msg: str) -> None:
    print(f"     {msg}")

# ---- commands ---------------------------------------------------------------

def cmd_token() -> None:
    """Verify the token file exists at the correct persistent path."""
    print(f"[token] Checking {TOKEN_PATH}")
    if not TOKEN_PATH.exists():
        _fail(f"Token file NOT found: {TOKEN_PATH}")
        _info("The TV will prompt for approval on next connection.")
        return
    size = TOKEN_PATH.stat().st_size
    if size == 0:
        _fail("Token file exists but is EMPTY — connection will fail.")
        return
    content = TOKEN_PATH.read_text(encoding="utf-8", errors="replace").strip()
    _ok(f"Token OK ({size} bytes): {content!r}")

    # Also check for the old token location
    old_path = _HA_ROOT / "custom_components/frame_art_shuffler/tokens/10_0_0_104.token"
    if old_path.exists():
        _info(f"Old token copy also present at {old_path} (harmless)")
    else:
        _info(f"Old token path not present: {old_path}")


def cmd_status() -> None:
    """Check TV screen state and art mode."""
    print(f"[status] Querying {TV_IP}...")
    try:
        screen_on = frame_tv.is_screen_on(TV_IP)
        _ok(f"screen_on = {screen_on}")
    except Exception as err:
        _fail(f"is_screen_on() raised: {err}")
        screen_on = None

    try:
        art_mode = frame_tv.is_art_mode_enabled(TV_IP)
        _ok(f"art_mode_enabled = {art_mode}")
    except Exception as err:
        _fail(f"is_art_mode_enabled() raised: {err}")
        art_mode = None

    if screen_on is False and art_mode is None:
        _info("TV appears to be fully powered off.")
    elif screen_on is False and art_mode is True:
        _info("TV is in art mode standby (screen off, art mode on).")
    elif screen_on is True and art_mode is True:
        _info("TV is awake and displaying art.")
    elif screen_on is True and art_mode is False:
        _info("TV is on but NOT in art mode (showing live TV / app?).")


def cmd_wake() -> None:
    """Send Wake-on-LAN and report resulting TV state."""
    mac = _get_mac()
    if not mac:
        return
    print(f"[wake] Sending WOL to {TV_IP} (mac={mac})...")
    _info("This takes ~25 seconds (3 WOL packets with delays).")
    try:
        result = frame_tv.tv_on(TV_IP, mac)
        _ok(f"tv_on() returned {result}")
    except Exception as err:
        _fail(f"tv_on() raised: {err}")
        return

    # Follow-up status check
    _info("Checking state after wake...")
    cmd_status()


def cmd_artmode() -> None:
    """Switch TV to art mode."""
    print(f"[artmode] Setting art mode on {TV_IP}...")
    try:
        frame_tv.set_art_mode(TV_IP)
        _ok("set_art_mode() completed without error")
    except Exception as err:
        _fail(f"set_art_mode() raised: {err}")
        return
    time.sleep(2)
    cmd_status()


def cmd_list() -> None:
    """List images currently on the TV."""
    print(f"[list] Listing images on {TV_IP}...")
    try:
        with frame_tv._FrameTVSession(TV_IP, timeout=15) as session:
            available = session.art.available() or []
        if not available:
            _info("No images reported by TV.")
            return
        _ok(f"{len(available)} image(s) on TV:")
        for img in available:
            cid = img.get("content_id", "?")
            name = img.get("file_name") or img.get("name") or ""
            _info(f"  {cid}  {name}")
    except Exception as err:
        _fail(f"Failed to list images: {err}")


def cmd_select(content_id: str) -> None:
    """Select an image already on the TV by content ID."""
    print(f"[select] Selecting {content_id} on {TV_IP}...")
    try:
        with frame_tv._FrameTVSession(TV_IP, timeout=15) as session:
            art = session.art
            available = art.available() or []
            on_tv = {img.get("content_id") for img in available}
            if content_id not in on_tv:
                _fail(f"{content_id} is NOT in TV image list. Available: {sorted(on_tv)}")
                return
            art.select_image(content_id, show=True)
            time.sleep(3)
            _ok(f"select_image({content_id!r}) succeeded — image should be displaying.")
    except Exception as err:
        _fail(f"select_image raised: {err}")


def cmd_display(art_path: str) -> None:
    """Full display_art flow: select if cached, upload otherwise."""
    print(f"[display] Displaying {art_path} on {TV_IP}...")
    try:
        content_id = frame_tv.display_art(TV_IP, art_path)
        _ok(f"display_art() returned content_id={content_id!r}")
    except Exception as err:
        _fail(f"display_art() raised: {err}")
        return

    # Show progress log if available
    log_path = frame_tv.PROGRESS_LOG_FILE
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        _info("Progress log (last 10 lines):")
        for line in lines[-10:]:
            _info(f"  {line}")


# ---- MAC lookup helper ------------------------------------------------------

def _get_mac() -> str | None:
    """Try to find the MAC address for TV_IP from the HA config."""
    # Look for it in config entries under .storage
    storage_path = _HA_ROOT / ".storage/core.config_entries"
    if not storage_path.exists():
        _fail(f"Config entries not found at {storage_path}. Pass MAC manually.")
        return None
    try:
        import json
        data = json.loads(storage_path.read_text())
        for entry in data.get("data", {}).get("entries", []):
            if entry.get("domain") != "frame_art_shuffler":
                continue
            for tv in entry.get("data", {}).get("tvs", []):
                if tv.get("ip") == TV_IP:
                    mac = tv.get("mac")
                    if mac:
                        return mac
    except Exception as err:
        _fail(f"Could not parse config entries: {err}")
    _fail(f"No MAC address found for {TV_IP} in config entries.")
    return None


# ---- main -------------------------------------------------------------------

COMMANDS = {
    "status": (cmd_status, []),
    "token": (cmd_token, []),
    "wake": (cmd_wake, []),
    "artmode": (cmd_artmode, []),
    "list": (cmd_list, []),
    "select": (cmd_select, ["content_id"]),
    "display": (cmd_display, ["path"]),
}


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    if cmd not in COMMANDS:
        print(f"Unknown command {cmd!r}. Available: {', '.join(COMMANDS)}")
        sys.exit(1)

    fn, params = COMMANDS[cmd]
    if len(args) - 1 < len(params):
        print(f"Usage: python3 -m skills.test_frame_tv {cmd} {' '.join(f'<{p}>' for p in params)}")
        sys.exit(1)

    fn(*args[1:1 + len(params)])


if __name__ == "__main__":
    main()
