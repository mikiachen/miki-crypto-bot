"""Local Foundry / Arc toolchain (forge, cast, optional arc) on PATH.

Binaries live in the project root (drag-and-drop). Call `ensure_foundry_on_path()`
before any subprocess that needs `cast` / `forge`.

Default network: Arc testnet (faucet-funded USDC).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TOOLS = ("cast", "forge", "anvil", "arc")

# Arc mainnet (tutorial) — override via env / .env
_DEFAULT_RPC = "https://rpc.testnet.arc.io"
_DEFAULT_BACKUP_RPC = "https://rpc.testnet.arc.io"
_DEFAULT_CHAIN_ID = 5042002
_DEFAULT_SYMBOL = "USDC"
_DEFAULT_EXPLORER = "https://testnet.arcscan.app"


def _host(url: str) -> str:
    try:
        return url.split("/")[2].lower()
    except IndexError:
        return ""


def is_allowed_rpc(url: str) -> bool:
    """HTTPS JSON-RPC only. Explorer and niorfun are not execution endpoints."""
    u = (url or "").strip()
    if not u.startswith("https://"):
        return False
    host = _host(u)
    if not host or host == "niorfun.com" or host.endswith(".niorfun.com"):
        return False
    if host == "arc-scan.org":
        return False
    return True


def get_rpc_url() -> str:
    raw = (os.environ.get("ARC_RPC_URL") or _DEFAULT_RPC).strip()
    if is_allowed_rpc(raw):
        return raw
    for alt in ("https://rpc.arc-scan.org", "https://rpc.mainnet.arc.io"):
        if is_allowed_rpc(alt):
            return alt
    return _DEFAULT_RPC


def get_backup_rpc_url() -> str:
    raw = (os.environ.get("ARC_BACKUP_RPC_URL") or _DEFAULT_BACKUP_RPC).strip()
    if is_allowed_rpc(raw):
        return raw
    return "https://rpc.mainnet.arc.io"


def get_chain_id() -> int:
    return int(os.environ.get("ARC_CHAIN_ID") or _DEFAULT_CHAIN_ID)


def get_symbol() -> str:
    return (os.environ.get("ARC_SYMBOL") or _DEFAULT_SYMBOL).strip() or "USDC"


def get_explorer() -> str:
    return (os.environ.get("ARC_EXPLORER") or _DEFAULT_EXPLORER).strip()


def rpc_endpoints() -> list[str]:
    """Primary + backup + verified HTTPS extras. Never HTTP, never niorfun."""
    primary = get_rpc_url()
    backup = get_backup_rpc_url()
    extra = (os.environ.get("ARC_RPC_EXTRA") or "").strip()
    out: list[str] = []
    for u in (primary, backup, extra, "https://rpc.arc-scan.org", "https://rpc.mainnet.arc.io"):
        if not is_allowed_rpc(u) or u in out:
            continue
        out.append(u)
    return out or ["https://rpc.mainnet.arc.io"]


def ordered_rpc_endpoints(prefer: str | None = None) -> list[str]:
    """Failover order: sticky prefer first, then primary, then backup."""
    base = rpc_endpoints()
    if not prefer:
        return base
    prefer = prefer.strip()
    if not is_allowed_rpc(prefer):
        return base
    if prefer in base:
        return [prefer] + [u for u in base if u != prefer]
    return [prefer] + base


# Back-compat module attrs (re-read env when ensure_foundry_on_path runs)
ARC_RPC_URL = get_rpc_url()
ARC_BACKUP_RPC_URL = get_backup_rpc_url()
ARC_CHAIN_ID = get_chain_id()
ARC_SYMBOL = get_symbol()
ARC_EXPLORER = get_explorer()


def _refresh_globals() -> None:
    global ARC_RPC_URL, ARC_BACKUP_RPC_URL, ARC_CHAIN_ID, ARC_SYMBOL, ARC_EXPLORER
    ARC_RPC_URL = get_rpc_url()
    ARC_BACKUP_RPC_URL = get_backup_rpc_url()
    ARC_CHAIN_ID = get_chain_id()
    ARC_SYMBOL = get_symbol()
    ARC_EXPLORER = get_explorer()


def project_root() -> Path:
    return _ROOT


def ensure_foundry_on_path() -> Path:
    """Prepend project root so `cast` / `forge` resolve to local binaries."""
    root = str(_ROOT)
    path = os.environ.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if root not in parts:
        os.environ["PATH"] = root + os.pathsep + path
    _refresh_globals()
    os.environ.setdefault("ARC_RPC_URL", ARC_RPC_URL)
    os.environ.setdefault("ARC_BACKUP_RPC_URL", ARC_BACKUP_RPC_URL)
    os.environ.setdefault("ARC_CHAIN_ID", str(ARC_CHAIN_ID))
    os.environ.setdefault("ARC_SYMBOL", ARC_SYMBOL)
    return _ROOT


def tool_path(name: str) -> Path | None:
    """Absolute path to a local binary, or None if missing."""
    ensure_foundry_on_path()
    local = _ROOT / name
    if local.is_file() and os.access(local, os.X_OK):
        return local
    which = shutil.which(name)
    return Path(which) if which else None


def toolchain_status() -> dict[str, str]:
    ensure_foundry_on_path()
    out: dict[str, str] = {
        "root": str(_ROOT),
        "rpc": ARC_RPC_URL,
        "backup_rpc": ARC_BACKUP_RPC_URL,
        "chain_id": str(ARC_CHAIN_ID),
        "symbol": ARC_SYMBOL,
        "network": "testnet",
    }
    for name in _TOOLS:
        p = tool_path(name)
        out[name] = str(p) if p else "MISSING"
    return out
