"""miki crypto bot · grok trencher — video-faithful UI on grok-trading-desk logs."""

from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import streamlit as st

ROOT = Path(__file__).resolve().parent
# Prefer vendored deps (websockets) used by grok-trading-desk
_VENDOR = ROOT / "grok-trading-desk" / ".vendor"
if _VENDOR.is_dir() and str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> None:
    """Load key=value into os.environ without overriding existing exports."""
    if not path.is_file():
        return
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


# Must load before desk_units so DESK_CHAIN=robinhood → USDG.
_load_dotenv(ROOT / ".env")
_load_dotenv(ROOT / ".env.rh")

from desk_realtime.bus import DeskBus
from desk_realtime.client import ensure_ws_client, inject_local
from desk_realtime.fastforward import (
    TapePlayer,
    ensure_tape,
    is_fastforward,
)
from desk_realtime.desk_units import (
    CHAIN_ID,
    CHAIN_LABEL,
    DESK_CHAIN,
    ENTRY,
    ENTRY_SOL,
    FEE_BLURB,
    QUOTE,
    STAKE,
    fmt_quote,
    fmt_quote_html,
    normalize_fill_amount,
)
from desk_realtime.foundry_bin import ensure_foundry_on_path
from desk_realtime.schema import stage_packets

# Local forge/cast (project root) on PATH for Arc RPC tooling
ensure_foundry_on_path()

LOG = ROOT / "grok-trading-desk" / "logs" / "desk.jsonl"
HALT_FLAG = ROOT / "grok-trading-desk" / "logs" / "desk.halt"
DEPLOY_FLAG = ROOT / "grok-trading-desk" / "logs" / "desk.deploy"
AVATARS = ROOT / "assets" / "avatars"
SESSION = 8 * 3600
# Quote: Arc USDC · RH USDG · Solana SOL
DESK_PAPER = os.environ.get("DESK_PAPER", "auto")
DESK_WS_URL = os.environ.get("DESK_WS_URL", "ws://127.0.0.1:8765")
DESK_FF = is_fastforward()
RUN_KEY = f"{QUOTE.lower()}{STAKE:g}_8h_ff_v3" if DESK_FF else f"{QUOTE.lower()}{STAKE:g}_8h_v3"
_FRAGMENT_KW: dict = {"run_every": 0.55} if DESK_FF else {"run_every": 2.5}
_IS_RH = DESK_CHAIN in ("robinhood", "rh", "rhchain")

# Back-compat aliases
fmt_sol = fmt_quote
fmt_sol_html = fmt_quote_html


@lru_cache(maxsize=16)
def avatar_data_uri(stem: str) -> str:
    path = AVATARS / f"{stem}.png"
    if not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


@lru_cache(maxsize=1)
def arc_mark_data_uri() -> str:
    """Official Arc mark for network badge (does not replace miki brand)."""
    path = ROOT / "assets" / "arc-mark.png"
    if not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


@lru_cache(maxsize=1)
def rh_mark_data_uri() -> str:
    """Robinhood Chain mark for network badge (RH desk)."""
    path = ROOT / "assets" / "rh-mark.png"
    if not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


@lru_cache(maxsize=1)
def miki_mark_data_uri() -> str:
    """Cyberpunk brand mark for topbar (replaces leaf emoji)."""
    path = ROOT / "assets" / "miki-cyber-mark.png"
    if not path.exists():
        return ""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def arc_network_label() -> tuple[str, str]:
    """Return (env_label, title) for Arc badge — Arc / Arc Network wording."""
    net = (os.environ.get("ARC_NETWORK") or "").strip().lower()
    rpc = (os.environ.get("ARC_RPC_URL") or "").lower()
    try:
        cid = int(os.environ.get("ARC_CHAIN_ID") or "0")
    except ValueError:
        cid = 0
    if net in ("mainnet", "main"):
        return "mainnet", "Arc Network · mainnet"
    if net in ("testnet", "test") or "testnet" in rpc or cid in (5042002,):
        return "testnet", "Arc Network · testnet"
    if cid and cid != 5042002:
        return "mainnet", f"Arc Network · chain {cid}"
    return "testnet", "Arc Network"


def rh_network_label() -> tuple[str, str]:
    try:
        cid = int(os.environ.get("RH_CHAIN_ID") or CHAIN_ID or "4663")
    except ValueError:
        cid = 4663
    return "mainnet", f"Robinhood Chain · {cid}"


def desk_intel_line() -> str:
    """One-line pump + RHC intel for RH desk (read-only)."""
    if not _IS_RH:
        return ""
    bits: list[str] = []
    try:
        from desk_realtime.pump_intel import theme_line as pump_line

        pl = pump_line()
        if pl and "unread" not in pl:
            bits.append(pl)
    except Exception:
        pass
    try:
        from desk_realtime.rhc_intel import theme_line as rhc_line

        rl = rhc_line()
        if rl and "unread" not in rl:
            bits.append(rl)
    except Exception:
        pass
    if not bits:
        try:
            from desk_realtime.engine_state import read_engine_state

            eng = read_engine_state()
            hot = eng.get("rhc_hot") or []
            if hot:
                bits.append("rhc · " + " · ".join(str(x) for x in hot[:3] if x))
            themes = eng.get("pump_themes") or []
            if themes:
                bits.append("pump · " + " · ".join(str(x) for x in themes[:3] if x))
        except Exception:
            pass
    return " · ".join(bits) if bits else "intel · pump+rhc read-only"
_MIKI_ICON = ROOT / "assets" / "miki-cyber-mark.png"
st.set_page_config(
    page_title="miki crypto bot · grok trencher",
    page_icon=str(_MIKI_ICON) if _MIKI_ICON.exists() else "⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');

:root,html[data-theme="night"]{
  /* LAYOUT LOCKED — do not change outer padding / gap shells without explicit ask */
  --p-container:24px;
  --gap-system:20px;
  --gap:var(--gap-system);
  --pad:16px;
  --radius:12px;
  --app-bg:#000000;
  --glass-bg:#0a0d14;
  --glass-border:1px solid #1e2530;
  --glass-blur:blur(16px);
  --glass-shadow:0 8px 32px 0 rgba(0,0,0,.37);
  --line:rgba(255,255,255,.06);
  --muted:#6a8a6a;
  --text:#c8ffd8;
  --hi:#e8ffe8;
  --green:#00ff66;
  --red:#ff3d6e;
  --yellow:#ffcc33;
  --blue:#4da3ff;
  --topbar-bg:#000000;
  --topbar-border:rgba(255,255,255,.1);
  --kv-bg:rgba(255,255,255,.02);
  --kv-border:rgba(255,255,255,.05);
  --live:#52ff8c;
  --theme-label:NIGHT;
  --desk-h:calc(100vh - 2.75rem - 12px);
  --desk-top-h:400px;
  --agents-h:320px;
  --dock-h:230px;
  --body-h:calc(var(--agents-h) + var(--dock-h) + var(--gap-system));
  --sig-h:88px;
  /* Web type scale (≈15px base) — labels ≥12px, body ≥14px, headers ≥15px */
  --fs-2xs:.75rem;   /* 12px captions */
  --fs-xs:.8125rem;  /* 13px secondary */
  --fs-sm:.875rem;   /* 14px body / feed */
  --fs-md:1rem;      /* 16px section headers */
  --fs-lg:1.125rem;  /* 18px KPI values */
  --fs-xl:1.375rem;  /* 22px panel heroes */
  --fs-2xl:1.75rem;  /* 28px balance */
  --fs-3xl:2.25rem;  /* 36px big number */
}
/* Day theme — WCAG-leaning light desk: high-contrast ink on white (no neon leftovers) */
html[data-theme="day"]{
  --app-bg:#F5F7F9;
  --glass-bg:#FFFFFF;
  --glass-border:1px solid #E0E0E0;
  --glass-blur:blur(8px);
  --glass-shadow:0 1px 2px rgba(20,30,40,.04),0 8px 20px rgba(20,30,40,.05);
  --line:rgba(20,30,40,.14);
  --muted:#4A4A4A;
  --text:#1A1A1A;
  --hi:#111111;
  --green:#137333;
  --red:#C5221F;
  --yellow:#B06000;
  --blue:#1967D2;
  --topbar-bg:#FFFFFF;
  --topbar-border:#E0E0E0;
  --kv-bg:#F7F8FA;
  --kv-border:#DADCE0;
  --live:#137333;
  --theme-label:DAY;
}
html[data-theme="day"] .ph.ph-green,
html[data-theme="day"] .bal-big.up,
html[data-theme="day"] .topbar .title,
html[data-theme="day"] .topbar .sub,
.desk[data-theme="day"] .ph.ph-green,
.desk[data-theme="day"] .bal-big.up,
.desk[data-theme="day"] .topbar .title,
.desk[data-theme="day"] .topbar .sub{text-shadow:none!important;}
html[data-theme="day"] .topbar .title,
.desk[data-theme="day"] .topbar .title{color:#1A1A1A!important;}
html[data-theme="day"] .topbar .sub,
.desk[data-theme="day"] .topbar .sub{color:#0D652D!important;}
html[data-theme="day"] .topbar .meta,
.desk[data-theme="day"] .topbar .meta,
html[data-theme="day"] .topbar .kv .lbl,
.desk[data-theme="day"] .topbar .kv .lbl,
html[data-theme="day"] .scoreboard-cell .sb-lbl,
.desk[data-theme="day"] .scoreboard-cell .sb-lbl,
html[data-theme="day"] .scoreboard-cell .sb-sub:not(.g):not(.r),
.desk[data-theme="day"] .scoreboard-cell .sb-sub:not(.g):not(.r),
html[data-theme="day"] .bal-sub,
.desk[data-theme="day"] .bal-sub,
html[data-theme="day"] .panel-sub,
.desk[data-theme="day"] .panel-sub,
html[data-theme="day"] .ph .tag,
.desk[data-theme="day"] .ph .tag,
html[data-theme="day"] .agent .ds,
.desk[data-theme="day"] .agent .ds,
html[data-theme="day"] .ac-sub,
.desk[data-theme="day"] .ac-sub,
html[data-theme="day"] .footer .time,
.desk[data-theme="day"] .footer .time,
html[data-theme="day"] .theme-chip,
.desk[data-theme="day"] .theme-chip,
html[data-theme="day"] .theme-chip span,
.desk[data-theme="day"] .theme-chip span{color:#4A4A4A!important;}
html[data-theme="day"] .topbar .kv .val,
.desk[data-theme="day"] .topbar .kv .val{color:#1A1A1A!important;}
html[data-theme="day"] .topbar .kv .val.g,
.desk[data-theme="day"] .topbar .kv .val.g{color:#137333!important;}
html[data-theme="day"] .topbar .kv .val.r,
.desk[data-theme="day"] .topbar .kv .val.r{color:#C5221F!important;}
html[data-theme="day"] .topbar .kv .val.y,
.desk[data-theme="day"] .topbar .kv .val.y{color:#B06000!important;}
html[data-theme="day"] .scoreboard-cell .sb-val,
.desk[data-theme="day"] .scoreboard-cell .sb-val{color:#1A1A1A!important;}
html[data-theme="day"] .scoreboard-cell .sb-val.g,
.desk[data-theme="day"] .scoreboard-cell .sb-val.g,
html[data-theme="day"] .scoreboard-cell .sb-sub.g,
.desk[data-theme="day"] .scoreboard-cell .sb-sub.g{color:#137333!important;text-shadow:none!important;}
html[data-theme="day"] .scoreboard-cell .sb-val.r,
.desk[data-theme="day"] .scoreboard-cell .sb-val.r,
html[data-theme="day"] .scoreboard-cell .sb-sub.r,
.desk[data-theme="day"] .scoreboard-cell .sb-sub.r{color:#C5221F!important;text-shadow:none!important;}
html[data-theme="day"] .agents-panel,
.desk[data-theme="day"] .agents-panel{background:transparent!important;}
html[data-theme="day"] .g,
.desk[data-theme="day"] .g,
html[data-theme="day"] .pos,
.desk[data-theme="day"] .pos{color:#137333!important;}
html[data-theme="day"] .r,
.desk[data-theme="day"] .r,
html[data-theme="day"] .neg,
.desk[data-theme="day"] .neg{color:#C5221F!important;}
html[data-theme="day"] .y,
.desk[data-theme="day"] .y{color:#B06000!important;}
html[data-theme="day"] .ph,
.desk[data-theme="day"] .ph{color:#333333!important;border-bottom-color:#DADCE0!important;}
html[data-theme="day"] .ph.ph-green,
.desk[data-theme="day"] .ph.ph-green{color:#0D652D!important;}
/* Chart axis labels */
html[data-theme="day"] .bal-y,
.desk[data-theme="day"] .bal-y,
html[data-theme="day"] .bal-x,
.desk[data-theme="day"] .bal-x{color:#333333!important;opacity:1!important;}
/* Desk Feed rows */
html[data-theme="day"] .feed .line .ts,
.desk[data-theme="day"] .feed .line .ts{color:#4A4A4A!important;}
html[data-theme="day"] .feed .line .msg,
.desk[data-theme="day"] .feed .line .msg{color:#333333!important;}
html[data-theme="day"] .feed .line .star,
.desk[data-theme="day"] .feed .line .star{color:#5F6368!important;}
html[data-theme="day"] .feed .line.buy-hit,
.desk[data-theme="day"] .feed .line.buy-hit,
html[data-theme="day"] .feed .line.cat-entry,
.desk[data-theme="day"] .feed .line.cat-entry{
  background:#E6F4EA!important;
  border-left:3px solid #137333!important;
  box-shadow:none!important;
}
html[data-theme="day"] .feed .line.buy-hit .msg,
.desk[data-theme="day"] .feed .line.buy-hit .msg,
html[data-theme="day"] .feed .line.cat-entry .msg,
.desk[data-theme="day"] .feed .line.cat-entry .msg,
html[data-theme="day"] .feed .line.buy-hit .star,
.desk[data-theme="day"] .feed .line.buy-hit .star,
html[data-theme="day"] .feed .line.cat-entry .star,
.desk[data-theme="day"] .feed .line.cat-entry .star,
html[data-theme="day"] .feed-pin .star,
.desk[data-theme="day"] .feed-pin .star,
html[data-theme="day"] .feed-pin .msg,
.desk[data-theme="day"] .feed-pin .msg{color:#137333!important;}
html[data-theme="day"] .feed .line.cat-err,
.desk[data-theme="day"] .feed .line.cat-err,
html[data-theme="day"] .feed .line.cat-skip:has(.tag.t-risk),
.desk[data-theme="day"] .feed .line.cat-skip:has(.tag.t-risk),
html[data-theme="day"] .feed .line.cat-skip:has(.tag.t-stop),
.desk[data-theme="day"] .feed .line.cat-skip:has(.tag.t-stop){
  background:#FCE8E6!important;
  border-left:3px solid #C5221F!important;
  box-shadow:none!important;
}
html[data-theme="day"] .feed .line.cat-err .msg,
.desk[data-theme="day"] .feed .line.cat-err .msg{color:#C5221F!important;}
html[data-theme="day"] .feed .tag.t-buy,
.desk[data-theme="day"] .feed .tag.t-buy,
html[data-theme="day"] .feed .tag.t-entry,
.desk[data-theme="day"] .feed .tag.t-entry,
html[data-theme="day"] .feed .tag.t-exit,
.desk[data-theme="day"] .feed .tag.t-exit,
html[data-theme="day"] .feed .tag.t-score,
.desk[data-theme="day"] .feed .tag.t-score,
html[data-theme="day"] .feed-pin .tag.t-buy,
.desk[data-theme="day"] .feed-pin .tag.t-buy,
html[data-theme="day"] .feed-pin .tag.t-entry,
.desk[data-theme="day"] .feed-pin .tag.t-entry{
  color:#137333!important;background:#E6F4EA!important;border:1px solid #A8DAB5!important;
}
html[data-theme="day"] .feed .tag.t-stop,
.desk[data-theme="day"] .feed .tag.t-stop,
html[data-theme="day"] .feed .tag.t-risk,
.desk[data-theme="day"] .feed .tag.t-risk,
html[data-theme="day"] .feed .tag.t-err,
.desk[data-theme="day"] .feed .tag.t-err,
html[data-theme="day"] .feed-pin .tag.t-stop,
.desk[data-theme="day"] .feed-pin .tag.t-stop,
html[data-theme="day"] .feed-pin .tag.t-risk,
.desk[data-theme="day"] .feed-pin .tag.t-risk{
  color:#C5221F!important;background:#FCE8E6!important;border:1px solid #F5C2C0!important;
}
html[data-theme="day"] .feed .tag.t-scan,
.desk[data-theme="day"] .feed .tag.t-scan{color:#1967D2!important;background:#E8F0FE!important;}
html[data-theme="day"] .feed .tag.t-watch,
.desk[data-theme="day"] .feed .tag.t-watch{color:#B06000!important;background:#FEF7E0!important;}
html[data-theme="day"] .feed .tag.t-dim,
.desk[data-theme="day"] .feed .tag.t-dim{color:#4A4A4A!important;background:#F1F3F4!important;}
html[data-theme="day"] .feed-filters a,
.desk[data-theme="day"] .feed-filters a{
  color:#333333!important;
  border:1.5px solid #5F6368!important;
  background:#FFFFFF!important;
}
html[data-theme="day"] .feed-filters a:hover,
.desk[data-theme="day"] .feed-filters a:hover{
  color:#1A1A1A!important;border-color:#202124!important;background:#F1F3F4!important;
}
html[data-theme="day"] .feed-filters a.on,
.desk[data-theme="day"] .feed-filters a.on{
  color:#FFFFFF!important;
  background:#202124!important;
  border-color:#202124!important;
}
html[data-theme="day"] .feed-pin,
.desk[data-theme="day"] .feed-pin{
  border-left-color:#137333!important;background:#E6F4EA!important;
}
html[data-theme="day"] .feed-pin .ts,
.desk[data-theme="day"] .feed-pin .ts{color:#4A4A4A!important;}
html[data-theme="day"] .theme-buy .tok,
.desk[data-theme="day"] .theme-buy .pct,
html[data-theme="day"] .theme-buy .pos-btn,
.desk[data-theme="day"] .theme-buy .pos-btn{color:#137333!important;text-shadow:none!important;}
html[data-theme="day"] .theme-buy .pos-btn,
.desk[data-theme="day"] .theme-buy .pos-btn{
  background:#E6F4EA!important;border-color:#137333!important;
  box-shadow:none!important;
}
html[data-theme="day"] .theme-buy .pos-bar,
.desk[data-theme="day"] .theme-buy .pos-bar{
  border-color:#137333!important;color:#137333!important;box-shadow:none!important;
}
html[data-theme="day"] .bal-big.up,
.desk[data-theme="day"] .bal-big.up{color:#137333!important;}
html[data-theme="day"] .bal-big.dn,
.desk[data-theme="day"] .bal-big.dn{color:#C5221F!important;}
html[data-theme="day"] .bal-badge,
.desk[data-theme="day"] .bal-badge{
  background:rgba(255,255,255,.92)!important;color:#137333!important;
  border:1px solid #A8DAB5!important;text-shadow:none!important;
  box-shadow:0 1px 4px rgba(20,30,40,.08)!important;
}
html[data-theme="day"] .bal-badge.dn,
.desk[data-theme="day"] .bal-badge.dn{
  color:#C5221F!important;border-color:#F5C2C0!important;
}
/* Phase chip (05 $TOKEN) — was night black slab; day = soft amber pill */
html[data-theme="day"] .phase,
.desk[data-theme="day"] .phase{
  background:#FEF7E0!important;
  color:#B06000!important;
  border:1px solid #F9AB00!important;
  box-shadow:none!important;
  text-shadow:none!important;
}
html[data-theme="day"] .bal-sub,
.desk[data-theme="day"] .bal-sub{color:#4A4A4A!important;}
html[data-theme="day"] .ob.w,
.desk[data-theme="day"] .ob.w{background:#137333!important;box-shadow:none!important;}
html[data-theme="day"] .ob.l,
.desk[data-theme="day"] .ob.l{background:#C5221F!important;}
html[data-theme="day"] .edge-formula,
.desk[data-theme="day"] .edge-formula,
html[data-theme="day"] .edge-exp.pos,
.desk[data-theme="day"] .edge-exp.pos,
html[data-theme="day"] .edge-stat .val.pos,
.desk[data-theme="day"] .edge-stat .val.pos,
html[data-theme="day"] .wc-flag .val,
.desk[data-theme="day"] .wc-flag .val,
html[data-theme="day"] .mf-foot .g,
.desk[data-theme="day"] .mf-foot .g,
html[data-theme="day"] .emb-foot .g,
.desk[data-theme="day"] .emb-foot .g{color:#137333!important;}
html[data-theme="day"] .edge-stat .ebar,
.desk[data-theme="day"] .edge-stat .ebar{background:#E4E7EA!important;}
html[data-theme="day"] .edge-stat .ebar>i.g,
.desk[data-theme="day"] .edge-stat .ebar>i.g,
html[data-theme="day"] .wc-pressure .ebar>i,
.desk[data-theme="day"] .wc-pressure .ebar>i{background:#137333!important;}
html[data-theme="day"] .agent .bubble,
.desk[data-theme="day"] .agent .bubble{
  background:#FFFFFF!important;color:#1A1A1A!important;
  border:1.5px solid #BDC1C6!important;
  box-shadow:0 2px 8px rgba(32,33,36,.12)!important;
  text-shadow:none!important;
}
/* Agent floor — clean white stage, readable labels (was washed gray) */
html[data-theme="day"] .stage,
.desk[data-theme="day"] .stage{
  background:#FFFFFF!important;
  border:1px solid #DADCE0!important;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.8)!important;
}
html[data-theme="day"] .stage:before,
.desk[data-theme="day"] .stage:before{
  background:
    repeating-linear-gradient(90deg,#9AA0A6 0 1px,transparent 1px 24px),
    repeating-linear-gradient(0deg,#9AA0A6 0 1px,transparent 1px 24px)!important;
  opacity:.28!important;
}
html[data-theme="day"] .stage:after,
.desk[data-theme="day"] .stage:after{background:none!important;}
html[data-theme="day"] .agents.floor > .agent:not(:last-child)::after,
.desk[data-theme="day"] .agents.floor > .agent:not(:last-child)::after{
  content:"";
  position:absolute;top:58%;right:-6%;
  width:12%;height:0;
  border-top:2px dashed #5F6368;
  pointer-events:none;z-index:0;opacity:1;
}
html[data-theme="day"] .agent .nm,
.desk[data-theme="day"] .agent .nm{
  color:var(--c)!important;
  font-size:var(--fs-xs)!important;
  font-weight:800!important;
  letter-spacing:.08em!important;
  text-shadow:none!important;
  filter:none!important;
}
html[data-theme="day"] .agent .ds,
.desk[data-theme="day"] .agent .ds{
  color:#3C4043!important;
  font-weight:600!important;
  opacity:1!important;
}
html[data-theme="day"] .agents-panel .ph,
.desk[data-theme="day"] .agents-panel .ph{
  color:#0D652D!important;
  border-bottom-color:#DADCE0!important;
}
/* Soften desk ground shadows on white stage; keep avatar above desk */
html[data-theme="day"] .station svg .desk-shadows .floor-shadow,
.desk[data-theme="day"] .station svg .desk-shadows .floor-shadow{fill:#5F6368!important;opacity:.18!important;}
html[data-theme="day"] .station svg .desk-shadows .chair-shadow,
.desk[data-theme="day"] .station svg .desk-shadows .chair-shadow{fill:#5F6368!important;opacity:.14!important;}
html[data-theme="day"] .station svg .desk-shadows .table-shadow,
.desk[data-theme="day"] .station svg .desk-shadows .table-shadow{fill:#5F6368!important;opacity:.12!important;}
.station svg .avatar-top{isolation:isolate;}
.station svg .trader-bob{filter:drop-shadow(0 1px 1px rgba(0,0,0,.25));}

html,body,[data-testid="stAppViewContainer"],[data-testid="stHeader"],.stApp,.main{
  background:var(--app-bg)!important;color:var(--text)!important;
  font-family:'IBM Plex Mono','Courier New',monospace!important;
  transition:background-color .45s ease,color .35s ease;}
html,body,.stApp,[data-testid="stAppViewContainer"]{
  height:auto!important;min-height:100vh!important;max-width:100%!important;
  overflow-x:hidden!important;}
[data-testid="stAppViewContainer"]{overflow-y:auto!important;}
section.main,[data-testid="stMain"]{
  height:auto!important;min-height:0!important;overflow:visible!important;}
[data-testid="stSidebar"]{display:none!important;}
[data-testid="stHeader"]{background:transparent!important;height:0!important;min-height:0!important;}
/* Recording: hide Streamlit chrome so Apple screen record doesn't catch Deploy/menu flash */
#MainMenu{visibility:hidden!important;}
header[data-testid="stHeader"]{visibility:hidden!important;}
[data-testid="stToolbar"]{display:none!important;}
[data-testid="stDecoration"]{display:none!important;}
[data-testid="stStatusWidget"]{display:none!important;}
footer{visibility:hidden!important;}
/* Soften fragment repaint (avoid hard black flash) */
[data-testid="stFragment"]{animation:none!important;}
.desk{/* no will-change — it forces layer thrash / flicker on FF remounts */}
.block-container{
  max-width:100%!important;width:100%!important;
  padding-top:var(--p-container)!important;padding-bottom:var(--p-container)!important;
  padding-left:var(--p-container)!important;padding-right:var(--p-container)!important;
  height:auto!important;min-height:100vh!important;box-sizing:border-box!important;
  display:flex!important;flex-direction:column!important;
  justify-content:flex-start!important;align-items:stretch!important;}
/* Stretch Streamlit wrappers — allow page to grow; only the app scrolls */
.block-container > div,
.block-container [data-testid="stVerticalBlock"],
.block-container [data-testid="element-container"],
.block-container [data-testid="stElementContainer"],
.block-container [data-testid="stMarkdownContainer"],
.block-container [data-testid="stMarkdownContainer"] > div,
.block-container [data-testid="stFragment"]{
  width:100%!important;max-width:100%!important;
  flex:0 0 auto!important;min-height:0!important;
  display:block!important;}
.block-container [data-testid="stVerticalBlock"]{
  display:flex!important;flex-direction:column!important;
}
*,div,p,span,label,code{font-family:'IBM Plex Mono','Courier New',monospace!important;}

/* Hide unused Streamlit layout chrome — desk is pure HTML grid */
div[data-testid="stVerticalBlockBorderWrapper"],
div[data-testid="stCheckbox"]{display:none!important;}

/* —— Glass card (unified wrapper) —— */
.glass,.glass-card{
  background:var(--glass-bg);
  backdrop-filter:var(--glass-blur);
  -webkit-backdrop-filter:var(--glass-blur);
  border:var(--glass-border);
  border-radius:var(--radius);
  padding:var(--pad);
  box-shadow:var(--glass-shadow);
  box-sizing:border-box;
  overflow:hidden;
  min-width:0;min-height:0;
  display:flex;flex-direction:column;
  height:100%;
  margin:0;
  transition:background-color .45s ease,border-color .35s ease,box-shadow .45s ease;
}
.glass.topbar{
  height:auto;flex:0 0 auto;flex-direction:row;align-items:center;overflow:visible;
  padding:10px 18px;border-radius:10px;min-height:52px;
  background:var(--topbar-bg);backdrop-filter:none;-webkit-backdrop-filter:none;
  box-shadow:none;border:1px solid var(--topbar-border);
}
.glass.footer,.glass-card.topbar,.glass-card.footer{
  height:auto;flex:0 0 auto;flex-direction:row;align-items:center;overflow:visible;
}

.topbar{
  display:flex;flex-direction:row;align-items:center;gap:0;flex-wrap:nowrap;
  width:100%;box-sizing:border-box;
}
.topbar .brand{display:flex;flex-direction:row;align-items:center;gap:10px;flex:0 0 auto;}
.topbar .logo{
  width:28px;height:28px;border-radius:50%;background:#0a0a0a;color:#d4ff00;
  display:inline-flex;align-items:center;justify-content:center;font-size:var(--fs-sm);flex-shrink:0;
  overflow:hidden;border:1px solid rgba(212,255,0,.35);padding:0;
}
.topbar .logo img{width:100%;height:100%;object-fit:cover;display:block;}
html[data-theme="day"] .topbar .logo,
.desk[data-theme="day"] .topbar .logo{
  background:#111;border-color:#50A88E;
}
.topbar .logo.is-rh{
  background:#CCFF00;border:none;border-radius:50%;
  box-shadow:none;outline:none;
}
.topbar .logo.is-rh img{
  object-fit:cover;border-radius:50%;
}
.topbar .title{color:#ffffff;font-weight:700;font-size:var(--fs-lg);letter-spacing:.01em;white-space:nowrap;}
html[data-theme="day"] .topbar .title.is-rh,
.desk[data-theme="day"] .topbar .title.is-rh{color:#111111;}
.topbar .title.is-rh{color:#e8ffe8;}
.topbar .net-badge{
  display:inline-flex;align-items:center;gap:8px;flex-shrink:0;
  margin-left:14px;margin-right:4px;
  padding:5px 10px 5px 6px;border-radius:6px;
  border:1px solid rgba(255,255,255,.14);
  background:#081426;
  text-decoration:none;color:#fff;line-height:1;
}
.topbar .net-badge img{
  width:16px;height:16px;display:block;flex-shrink:0;object-fit:contain;
}
.topbar .net-badge .net-name{
  font-size:var(--fs-xs);font-weight:700;letter-spacing:.04em;color:#fff;
}
.topbar .net-badge .net-env{
  font-size:var(--fs-2xs);font-weight:600;letter-spacing:.1em;text-transform:uppercase;
  color:#9AA4B2;padding-left:8px;margin-left:2px;border-left:1px solid rgba(255,255,255,.14);
}
.topbar .net-badge .net-env.is-main{color:#50A88E;}
.topbar .net-badge .net-env.is-test{color:#9AA4B2;}
html[data-theme="day"] .topbar .net-badge,
.desk[data-theme="day"] .topbar .net-badge{
  background:#0B1B2E;border-color:#D0D5DD;
}
html[data-theme="day"] .topbar .net-badge .net-name,
.desk[data-theme="day"] .topbar .net-badge .net-name{color:#FFFFFF;}
html[data-theme="day"] .topbar .net-badge .net-env,
.desk[data-theme="day"] .topbar .net-badge .net-env{color:#98A2B3;border-left-color:#344054;}
html[data-theme="day"] .topbar .net-badge .net-env.is-main,
.desk[data-theme="day"] .topbar .net-badge .net-env.is-main{color:#50A88E;}
.topbar .vdiv{width:1px;height:22px;background:rgba(255,255,255,.14);flex-shrink:0;margin:0 18px;}
.topbar .id{display:flex;flex-direction:row;align-items:baseline;gap:14px;flex:0 0 auto;min-width:0;}
.topbar .sub{color:#52ff8c;font-weight:700;font-size:var(--fs-md);white-space:nowrap;flex-shrink:0;}
.topbar .meta{color:#888888;font-size:var(--fs-xs);white-space:nowrap;flex-shrink:0;letter-spacing:.02em;}
.topbar .metrics{
  display:flex;flex-direction:row;align-items:center;gap:12px;
  flex:1 1 auto;min-width:0;margin-left:0;padding-left:16px;
}
.topbar .metrics-grid{
  display:grid;grid-template-columns:repeat(10,minmax(0,1fr));
  flex:1 1 auto;min-width:0;align-items:center;justify-items:center;
  gap:4px 8px;
}
.topbar .kv{
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;line-height:1.15;
  min-width:0;width:100%;text-align:center;
  padding:6px 4px;border-radius:6px;
  background:var(--kv-bg);border:1px solid var(--kv-border);
  box-sizing:border-box;
}
.topbar .kv .lbl{color:#888888;font-size:var(--fs-2xs);letter-spacing:.08em;font-weight:600;text-transform:uppercase;}
.topbar .kv .val{color:#ffffff;font-size:var(--fs-sm);font-weight:700;white-space:nowrap;letter-spacing:.01em;
  max-width:100%;overflow:hidden;text-overflow:ellipsis;}
.topbar .kv .val.g{color:#52ff8c;} .topbar .kv .val.r{color:#ff5f5f;} .topbar .kv .val.y{color:#d4ff00;}
.topbar .live{
  display:inline-flex;align-items:center;gap:7px;margin-left:2px;flex-shrink:0;
  color:var(--live);font-size:var(--fs-xs);font-weight:700;letter-spacing:.12em;
  padding:6px 10px;border:1px solid color-mix(in srgb,var(--live) 28%,transparent);border-radius:6px;
}
.topbar .live.halted{color:#ff5f5f;border-color:rgba(255,95,95,.35);animation:none;}
.topbar .live.halted:after{background:#ff5f5f;box-shadow:0 0 8px #ff5f5f;}
.topbar .live:after{
  content:"";width:7px;height:7px;border-radius:50%;background:var(--live);
  box-shadow:0 0 8px var(--live);flex-shrink:0;
}
.topbar .theme-chip{
  display:inline-flex;align-items:center;gap:6px;margin-left:8px;flex-shrink:0;
  color:var(--muted);font-size:var(--fs-2xs);font-weight:700;letter-spacing:.1em;
  padding:5px 8px;border:1px solid var(--kv-border);border-radius:6px;
  background:var(--kv-bg);cursor:pointer;user-select:none;
}
.topbar .theme-chip:hover{color:var(--text);border-color:color-mix(in srgb,var(--green) 35%,transparent);}
.topbar .theme-chip b{color:var(--green);font-weight:700;}
@keyframes blink{50%{opacity:.35}}
/* LIVE chip: soft pulse only in night; day/FF stay solid (no flicker) */
.topbar .live{animation:blink 1.5s step-start infinite;}
html[data-theme="day"] .topbar .live,
.desk[data-theme="day"] .topbar .live,
.desk.ff-mode .topbar .live{animation:none!important;opacity:1!important;}
/* Recording / FF: kill decorative blinkers that look like screen flicker */
.desk.ff-mode .sig-module .sig-wave > i,
.desk.ff-mode .agent .trader-bob,
.desk.ff-mode .agent .type-l,
.desk.ff-mode .agent .type-r,
.desk.ff-mode .bal-dot-halo,
.desk.ff-mode .pos-panic,
.desk.ff-mode .feed .line.buy-hit,
.desk.ff-mode .feed .line.cat-entry{
  animation:none!important;
}
.topbar .console{
  display:inline-flex;align-items:center;gap:8px;margin-left:10px;flex-shrink:0;
}
.topbar .console .tc{
  font-size:var(--fs-xs);font-weight:700;letter-spacing:.06em;padding:6px 10px;
  border-radius:6px;border:1px solid rgba(255,255,255,.14);color:#c8d0d8;
  background:rgba(255,255,255,.03);white-space:nowrap;
}
.topbar .console .tc.stop{color:#ff5f5f;border-color:rgba(255,95,95,.4);}
.topbar .console .tc.deploy{color:#d4ff00;border-color:rgba(212,255,0,.35);}
/* Streamlit Stop/Deploy strip (maps to top-right console actions) */
div[data-testid="stHorizontalBlock"]:has(button[kind="secondary"]) button{
  min-height:2rem!important;
}

/* —— Tactical scoreboard — 4 equal cards, GAP_SYSTEM —— */
.desk-score{
  display:grid;grid-template-columns:repeat(5,minmax(0,1fr));
  gap:var(--gap-system);flex:0 0 auto;width:100%;align-items:stretch;
  margin:0;padding:0;
}
.desk-score > .glass.scoreboard-cell{
  height:auto;min-height:96px;padding:14px 16px;
  flex:1 1 auto;
}
.scoreboard-cell{
  display:flex;flex-direction:column;justify-content:center;gap:5px;
  min-width:0;position:relative;overflow:hidden;
}
.scoreboard-cell .sb-lbl{
  color:#8a9490;font-size:var(--fs-2xs);font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;line-height:1.2;position:relative;z-index:1;
}
.scoreboard-cell .sb-lbl.alert{color:#ff5f6d;}
.scoreboard-cell .sb-val{
  color:#f2f5f3;font-size:var(--fs-lg);font-weight:700;letter-spacing:.01em;
  line-height:1.15;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  position:relative;z-index:1;max-width:70%;
}
.scoreboard-cell .sb-val.clock{max-width:100%;letter-spacing:0;}
.scoreboard-cell .sb-val.g{color:#22c55e;text-shadow:0 0 12px rgba(34,197,94,.28);}
.scoreboard-cell .sb-val.r{color:#ff4d5e;text-shadow:0 0 12px rgba(255,77,94,.22);}
.scoreboard-cell .sb-sub{color:#6a7a72;font-size:var(--fs-2xs);letter-spacing:.04em;line-height:1.25;
  position:relative;z-index:1;}
.scoreboard-cell .sb-sub.g{color:#22c55e;}
.scoreboard-cell .sb-sub.r{color:#ff6b7a;}
.scoreboard-cell .sb-spark{
  position:absolute;right:10px;bottom:8px;width:42%;max-width:140px;height:36px;
  pointer-events:none;z-index:0;opacity:.95;
}
.scoreboard-cell .sb-spark svg{width:100%;height:100%;display:block;}
.scoreboard-cell .sb-gate-top{display:flex;align-items:center;justify-content:space-between;gap:8px;position:relative;z-index:1;}
.scoreboard-cell .sb-gate-row{display:flex;align-items:baseline;gap:8px;min-width:0;position:relative;z-index:1;}
.scoreboard-cell .sb-gate-name{
  color:#ff4d5e;font-size:1.05rem;font-weight:700;letter-spacing:.08em;
  text-shadow:0 0 10px rgba(255,77,94,.25);
}
.scoreboard-cell .sb-gate-tag{color:#ff6b7a;font-size:var(--fs-2xs);font-weight:700;letter-spacing:.08em;white-space:nowrap;}
.scoreboard-cell .sb-dot{
  width:7px;height:7px;border-radius:50%;background:#ff4d5e;flex-shrink:0;
  box-shadow:0 0 8px #ff4d5e;animation:blink 1.4s step-start infinite;
}
.scoreboard-cell .sb-pips{display:flex;gap:3px;align-items:center;margin-top:1px;position:relative;z-index:1;}
.scoreboard-cell .sb-pips i{
  flex:1 1 0;height:7px;min-width:8px;max-width:18px;border-radius:2px;
  background:rgba(255,255,255,.08);display:block;
}
.scoreboard-cell .sb-pips i.on{background:#ff4d5e;box-shadow:0 0 6px rgba(255,77,94,.45);}

/* —— Desk frame: unified GAP_SYSTEM only —— */
.desk{
  display:flex;flex-direction:column;
  gap:var(--gap-system);
  row-gap:var(--gap-system);
  width:100%;max-width:100%;
  height:auto;max-height:none;
  min-height:calc(100vh - (2 * var(--p-container)));
  box-sizing:border-box;margin:0 auto;padding:0;
}
/* Utility spacing shell for SIGNAL INTERCEPT mount point */
.w-full{width:100%;}
.my-5{margin-top:20px;margin-bottom:20px;}
.h-20{height:80px;}
.footer{
  display:flex;flex-direction:row;align-items:center;gap:var(--gap-system);
  padding:8px var(--pad);font-size:var(--fs-xs);color:var(--muted);white-space:nowrap;
  flex:0 0 auto;margin:0;
}

/* Main stage — left agents+dock | right stack */
.desk-body{
  display:grid;
  grid-template-columns:minmax(0,7fr) minmax(0,5fr);
  gap:var(--gap-system);
  align-items:stretch;
  flex:0 0 auto;
  height:var(--body-h);
  min-height:var(--body-h);
  max-height:var(--body-h);
  overflow:hidden;
  margin:0;
}
.desk-left{
  display:flex;flex-direction:column;
  gap:var(--gap-system);row-gap:var(--gap-system);
  min-height:0;height:100%;margin:0;
}
.desk-left > .glass:has(.agents-panel){
  flex:0 0 auto;
  height:var(--agents-h);min-height:var(--agents-h);max-height:var(--agents-h);
  overflow:hidden;margin:0;padding:var(--pad);box-sizing:border-box;
}
.desk-left .dock{
  display:grid;grid-template-columns:repeat(2,minmax(0,1fr));
  gap:var(--gap-system);
  flex:1 1 auto;
  min-height:var(--dock-h);height:auto;max-height:none;
  align-items:stretch;margin:0;
}
.desk-left .dock > .glass{
  min-width:0;min-height:0;height:100%;
  overflow:hidden;padding:var(--pad);margin:0;box-sizing:border-box;
}
.desk-left .dock .mf-panel,
.desk-left .dock .emb-panel{
  min-height:0;height:100%;
  display:flex;flex-direction:column;
}
.desk-left .dock .mf-panel .ph,
.desk-left .dock .emb-panel .ph{
  margin-bottom:6px;padding-bottom:6px;font-size:var(--fs-sm);
}
.desk-left .dock .mf-sub,
.desk-left .dock .emb-sub{margin:0 0 6px;font-size:var(--fs-xs);line-height:1.35;}
.desk-left .dock .chart-slot{min-height:0;flex:1 1 auto;}
.desk-left .dock .mf-foot,
.desk-left .dock .emb-foot{margin-top:6px;font-size:var(--fs-xs);}
/* THE BALANCE gets the tall chart band */
.desk-top > .glass:first-child{
  min-height:0;overflow:hidden;
}
.desk-top > .glass:first-child .chart-slot{
  flex:1 1 auto;min-height:140px;
}
.desk-top > .glass:first-child .bal-chart{min-height:140px;}
.desk-top > .glass:first-child .bal-badge-wrap{
  transform:translate(-50%,calc(-100% - 8px));
}

/* —— RIGHT COLUMN —— */
.desk-right{
  display:flex;
  flex-direction:column;
  gap:var(--gap-system);
  min-height:0;
  height:100%;
  overflow:visible;
  align-items:stretch;
  isolation:isolate;
  margin:0;
}
.desk-right > .pair{
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:var(--gap-system);
  min-width:0;
  width:100%;
  position:relative;
  z-index:1;
  flex:0 0 auto;
  height:auto;
  min-height:0;
  overflow:visible;
  isolation:isolate;
  margin:0;
}
.desk-right > .pair > .glass{
  min-width:0;
  min-height:0;
  height:100%;
  align-self:stretch;
  display:flex;
  flex-direction:column;
  overflow:hidden;
  position:relative;
  z-index:1;
  margin:0;
}
.desk-right > .pair.pair-mid{
  flex:0 0 auto;
  min-height:180px;
  align-items:stretch;
  overflow:visible;
}
.desk-right > .pair.pair-mid > .glass{
  padding:var(--pad);
  min-height:180px;
  height:100%;
  overflow:hidden;
}
.desk-right > .pair.pair-bot{
  flex:1 1 auto;
  min-height:140px;
  overflow:hidden;
  align-items:stretch;
}
.desk-right > .pair.pair-bot > .glass{
  height:100%;min-height:0;overflow:hidden;
}
.desk-right > .pair.pair-mid .chart-slot,
.desk-right > .pair.pair-bot .chart-slot,
.desk-right > .pair.pair-bot .wc-body{
  min-height:0;
}
.desk-right .glass.edge-card,
.desk-right .edge-card,
.glass.edge-card{
  flex:0 0 auto!important;
  flex-grow:0!important;
  height:auto!important;
  min-height:0!important;
  max-height:none;
  overflow:hidden;
  align-self:stretch;
  width:100%;
  display:grid!important;
  grid-template-columns:max-content minmax(0,1fr);
  grid-template-rows:auto auto;
  column-gap:12px;
  row-gap:6px;
  align-items:start;
  justify-content:unset!important;
  flex-direction:unset!important;
  padding:16px!important;
  margin:0;
}
/* Mid row: rings + compact scan */
.desk-right .pair-mid .ac-wrap{height:auto;min-height:0;}
.desk-right .pair-mid .ac-list{
  gap:12px;padding:10px 4px 6px;justify-content:space-evenly;
}
.desk-right .pair-mid .ac-row{gap:24px;}
.desk-right .pair-mid .ac-row .ring{
  width:78px;height:78px;flex:0 0 78px;aspect-ratio:1/1;font-size:1.15rem;
}
.desk-right .pair-mid .ac-title{font-size:var(--fs-sm);letter-spacing:.06em;}
.desk-right .pair-mid .ac-sub{font-size:var(--fs-xs);}
.desk-right .pair-mid .scan-wrap{gap:6px;min-height:0;height:auto;overflow:hidden;}
.desk-right .pair-mid .scan-grid{
  grid-template-columns:repeat(10,minmax(0,1fr));
  grid-template-rows:repeat(8,minmax(0,1fr));
  gap:3px;align-content:stretch;flex:0 0 auto;min-height:100px;height:120px;
  max-height:none;overflow:hidden;
}
.desk-right .pair-mid .scan-grid .sg{
  aspect-ratio:auto;width:100%;height:100%;min-width:0;min-height:0;
}
.desk-right .pair-mid .panel-name,
.desk-right .pair-mid .ph{
  margin-bottom:6px;padding-bottom:5px;font-size:var(--fs-sm);
}
.desk-right .pair-mid .panel-sub{font-size:var(--fs-2xs);margin:0 0 4px;}
.desk-right .pair-mid .glass-foot{font-size:var(--fs-2xs);margin-top:4px;}

/* —— Panel chrome (headers unified: white / size / font) —— */
.ph,.panel-name{
  color:#ffffff;font-size:var(--fs-sm);font-weight:700;letter-spacing:.1em;
  font-family:'IBM Plex Mono','Courier New',monospace;
  border-bottom:1px solid var(--line);
  padding-bottom:8px;margin:0 0 var(--gap-system);display:flex;justify-content:space-between;align-items:center;
  flex:0 0 auto;gap:8px;line-height:1.2;text-transform:uppercase;
}
.ph .tag{color:var(--muted);letter-spacing:.06em;font-size:var(--fs-2xs);font-weight:500;text-transform:none;
  border:1px solid rgba(255,255,255,.08);padding:3px 8px;border-radius:4px;}
/* Only these two left titles use neon green */
.ph.ph-green{color:var(--green);text-shadow:0 0 10px rgba(0,255,102,.25);}
.ph.ph-green .tag{text-shadow:none;}
.panel-sub{color:var(--muted);font-size:var(--fs-2xs);letter-spacing:.04em;margin:4px 0 0;line-height:1.3;
  border:none;padding:0;}
.glass-body{flex:1 1 auto;min-height:0;overflow:hidden;display:flex;flex-direction:column;}
.desk-top-right > .pair > .glass > .glass-body,
.desk-right .pair-top > .glass > .glass-body{
  overflow:hidden;min-height:0;flex:1 1 auto;height:auto;
}
.desk-top-right > .pair > .glass:has(.pos-panel) > .glass-body,
.desk-right .pair-top > .glass:has(.pos-panel) > .glass-body{
  overflow:hidden;flex:1 1 auto;min-height:0;height:100%;
  display:flex;flex-direction:column;
}
.desk-top-right > .pair > .glass:has(.pos-panel) > .glass-body > .pos-panel{
  flex:1 1 auto;min-height:0;height:100%;
}
.desk-right .pair-mid > .glass > .glass-body,
.desk-right .pair-bot > .glass > .glass-body{min-height:0;height:auto;flex:1 1 auto;}
.glass-foot{flex:0 0 auto;margin-top:var(--gap-system);color:var(--muted);font-size:var(--fs-2xs);line-height:1.4;}

/* DESK FEED — activity-log layout on dark terminal base */
.feed-card{
  padding:16px;min-height:0;height:100%;max-height:100%;
  display:flex;flex-direction:column;overflow:hidden;
}
.feed-card .ph{
  margin-bottom:4px;padding-bottom:6px;flex:0 0 auto;
  display:flex;justify-content:space-between;align-items:center;gap:8px;
}
.feed-card .ph .tag{
  color:#8a9490;letter-spacing:.06em;font-size:var(--fs-2xs);font-weight:600;
  border:1px solid rgba(255,255,255,.08);padding:3px 8px;border-radius:4px;
  text-transform:uppercase;
}
.feed-card .panel-sub{margin:0 0 8px;flex:0 0 auto;}
.feed-card .glass-body{
  margin-top:0;min-height:0;flex:1 1 auto;
  overflow:hidden;display:flex;flex-direction:column;gap:8px;
}
.feed-pin{
  flex:0 0 auto;
  display:grid;
  grid-template-columns:72px 64px minmax(0,1fr) 16px;
  gap:8px;align-items:center;
  padding:8px 10px;border-radius:6px;
  background:linear-gradient(90deg,rgba(0,255,102,.10),rgba(0,255,102,.02) 70%,transparent);
  border:1px solid rgba(0,255,102,.18);
  font-size:var(--fs-xs);line-height:1.4;
}
.feed-pin .ts{color:#6a8a6a;font-variant-numeric:tabular-nums;white-space:nowrap;}
.feed-pin .msg{color:#9dffc0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.feed-pin .star{color:#00ff66;text-align:right;}
.feed{
  flex:1 1 auto;min-height:0;height:100%;max-height:100%;
  overflow-y:auto;overflow-x:hidden;
  font-size:var(--fs-sm);line-height:1.65;
  display:flex;flex-direction:column-reverse;justify-content:flex-start;gap:4px;
  padding-right:6px;
  scrollbar-width:thin;scrollbar-color:#2a3238 transparent;
}
.feed::-webkit-scrollbar{width:3px;}
.feed::-webkit-scrollbar-track{background:transparent;}
.feed::-webkit-scrollbar-thumb{
  background:linear-gradient(180deg,#3a444c 0%,#1e252b 100%);
  border-radius:3px;
}
.feed::-webkit-scrollbar-thumb:hover{background:#4a5560;}
.feed .line{
  display:grid;
  grid-template-columns:72px 64px minmax(0,1fr) 16px;
  gap:8px;align-items:center;
  width:100%;box-sizing:border-box;
  text-align:left;word-break:break-word;flex:0 0 auto;
  padding:6px 6px;border-radius:4px;
}
.feed .line .ts{color:#5a6670;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:visible;}
.feed .line .tag{
  display:inline-flex;align-items:center;justify-content:center;
  min-width:48px;max-width:64px;padding:2px 6px;border-radius:3px;
  font-size:var(--fs-2xs);font-weight:700;letter-spacing:.05em;text-transform:uppercase;
  white-space:nowrap;justify-self:start;
}
.feed .line .msg{
  color:#b8c4bc;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;
}
.feed .line .star{color:#3a444c;text-align:right;font-weight:700;}
.feed .line.buy-hit,
.feed .line.cat-entry{
  background:rgba(0,255,102,.10);
  border-left:2px solid #00ff66;
  padding-left:6px;
  /* one-shot pulse only — infinite flash causes “一闪一闪” */
  animation:feedBuyFlash .9s ease-out 1;
}
.feed .line.buy-hit .msg,
.feed .line.cat-entry .msg{color:#7CFF9A;font-weight:700;}
.feed .line.buy-hit .star,
.feed .line.cat-entry .star{color:#00ff66;}
.feed .line.cat-skip{
  opacity:.72;
}
.feed .line.cat-skip .msg{color:#8a8570;}
.feed .line.cat-skip .tag{color:#c4a35a;background:rgba(196,163,90,.12);}
.feed .line.cat-err{
  background:rgba(255,59,48,.10);
  border-left:2px solid #FF3B30;
  padding-left:6px;
}
.feed .line.cat-err .msg{color:#ff6b6b;font-weight:700;}
.feed .line.cat-err .tag{color:#FF3B30;background:rgba(255,59,48,.16);font-weight:800;}
/* tag color chips */
.feed .tag.t-scan,.feed-pin .tag.t-scan{color:#7aaaff;background:rgba(77,163,255,.14);}
.feed .tag.t-buy,.feed-pin .tag.t-buy{color:#00ff66;background:rgba(0,255,102,.18);font-weight:800;}
.feed .tag.t-entry,.feed-pin .tag.t-entry{color:#7CFF9A;background:rgba(124,255,154,.18);font-weight:800;}
.feed .tag.t-exit,.feed-pin .tag.t-exit{color:#52ff8c;background:rgba(82,255,140,.12);}
.feed .tag.t-stop,.feed-pin .tag.t-stop{color:#FF3B30;background:rgba(255,59,48,.16);font-weight:800;}
.feed .tag.t-score,.feed-pin .tag.t-score{color:#39ff14;background:rgba(57,255,20,.10);}
.feed .tag.t-watch,.feed-pin .tag.t-watch{color:#ffcc33;background:rgba(255,204,51,.12);}
.feed .tag.t-risk,.feed-pin .tag.t-risk{color:#c4a35a;background:rgba(196,163,90,.12);}
.feed .tag.t-dim,.feed-pin .tag.t-dim{color:#7a8a7a;background:rgba(255,255,255,.05);}
.feed .tag.t-err,.feed-pin .tag.t-err{color:#FF3B30;background:rgba(255,59,48,.18);font-weight:800;}

/* Desk Feed filter tabs (CSS-only, no JS) */
.feed-card .feed-tools{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  flex:0 0 auto;margin:0 0 8px;
}
.feed-filters{
  display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap;
}
.feed-filters a{
  cursor:pointer;user-select:none;text-decoration:none!important;
  font-size:var(--fs-2xs);font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:#6a7a72;border:1px solid rgba(255,255,255,.1);border-radius:4px;
  padding:3px 8px;line-height:1.2;
  transition:color .15s ease,border-color .15s ease,background .15s ease;
}
.feed-filters a:hover{color:#c8d0d8;border-color:rgba(255,255,255,.22);}
.feed-filters a.on{
  color:#00ff66;border-color:rgba(0,255,102,.45);background:rgba(0,255,102,.08);
}
.feed-card.ff-entries .feed .line:not([data-cat="entry"]),
.feed-card.ff-skipped .feed .line:not([data-cat="skip"]),
.feed-card.ff-errors .feed .line:not([data-cat="err"]){display:none!important;}
.feed-card.ff-entries .feed-pin:not([data-cat="entry"]),
.feed-card.ff-skipped .feed-pin:not([data-cat="skip"]),
.feed-card.ff-errors .feed-pin:not([data-cat="err"]){display:none!important;}

/* Critical disconnect banner */
.dc-crit{
  display:none;flex:0 0 auto;width:100%;box-sizing:border-box;
  align-items:center;justify-content:center;gap:10px;
  background:#FF3B30;color:#ffffff;
  font-size:var(--fs-sm);font-weight:800;letter-spacing:.12em;text-transform:uppercase;
  padding:10px 16px;border-radius:8px;margin:0 0 var(--gap-system);
  box-shadow:0 0 24px rgba(255,59,48,.45);
  animation:dcCritBlink 1s step-end infinite;
}
.desk.dc-on .dc-crit{display:flex;}
@keyframes dcCritBlink{
  0%,49%{opacity:1;filter:brightness(1);}
  50%,100%{opacity:.72;filter:brightness(1.25);}
}
.glass.pos-dc{
  border-color:#FF3B30!important;
  box-shadow:0 0 0 1px rgba(255,59,48,.55),0 0 22px rgba(255,59,48,.28)!important;
  animation:posDcBreath 1.2s ease-in-out infinite;
}
@keyframes posDcBreath{
  0%,100%{box-shadow:0 0 0 1px rgba(255,59,48,.45),0 0 14px rgba(255,59,48,.18);}
  50%{box-shadow:0 0 0 2px rgba(255,59,48,.9),0 0 28px rgba(255,59,48,.42);}
}

/* Panic exit */
.pos-actions{
  display:grid;grid-template-columns:1fr 1fr;gap:10px;
  flex:0 0 auto;margin:0 0 16px;width:100%;
}
.pos-actions .pos-btn{min-height:48px;padding:14px 10px;font-size:.82rem;letter-spacing:.1em;}
.pos-panic{
  display:flex;align-items:center;justify-content:center;text-align:center;
  width:100%;box-sizing:border-box;padding:14px 10px;border-radius:12px;
  font-weight:800;letter-spacing:.1em;font-size:.82rem;text-transform:uppercase;
  line-height:1.15;min-height:48px;text-decoration:none!important;
  color:#ffffff!important;background:#FF3B30;border:1px solid #ff6b63;
  box-shadow:0 0 16px rgba(255,59,48,.35);
  animation:panicPulse 1.4s ease-in-out infinite;
}
.pos-panic:hover{filter:brightness(1.08);color:#ffffff!important;}
.pos-panic.is-disabled{
  opacity:.35;pointer-events:none;animation:none;background:#4a2020;border-color:#662828;
}
@keyframes panicPulse{
  0%,100%{box-shadow:0 0 12px rgba(255,59,48,.28);}
  50%{box-shadow:0 0 22px rgba(255,59,48,.55);}
}

.bal-head{display:grid;grid-template-columns:1fr auto;gap:var(--gap-system);align-items:start;flex:0 0 auto;margin:0;}
.bal-head > div:last-child{text-align:right;}
.bal-big{font-size:var(--fs-3xl);font-weight:700;line-height:1;}
.bal-big.up{color:var(--green);text-shadow:0 0 14px #00ff6688;}
.bal-big.dn{color:var(--red);text-shadow:0 0 14px #ff3d6e66;}
.bal-sub{color:var(--muted);font-size:var(--fs-xs);margin:6px 0 0;}
.phase{display:inline-block;background:#2a2200;color:var(--yellow);border:1px solid #665500;
  padding:.22rem .55rem;font-size:var(--fs-sm);letter-spacing:.06em;font-weight:700;}
.outcome-row{
  display:grid;
  grid-template-columns:42px minmax(0,1fr);
  gap:2px 6px;
  margin-top:6px;
  flex:0 0 auto;
  align-items:end;
  min-height:18px;
  width:100%;
  box-sizing:border-box;
}
.outcome-row .ob-track{
  grid-column:2;
  display:flex;align-items:flex-end;justify-content:flex-start;
  gap:1.5px;width:100%;min-height:16px;padding:0 2px 0 4px;
  box-sizing:border-box;
}
.outcome-row .ob-track.sync{
  justify-content:stretch;
  gap:4px;
  padding:0 4px 0 4px;
}
.outcome-row .ob-track.sync .ob{
  flex:1 1 0;min-width:8px;max-width:none;
}
.ob{
  flex:1 1 0;min-width:2px;max-width:5px;
  display:block;border-radius:1px 1px 0 0;
}
.ob.up{
  background:var(--green);
  box-shadow:0 0 3px rgba(0,255,102,.45);
}
.ob.dn{
  background:var(--red);
  box-shadow:0 0 3px rgba(255,61,110,.35);
}

/* SIGNAL INTERCEPT — ref layout: header row + full-width wave */
.desk-sig{
  flex:0 0 auto;
  width:100%;
  padding:0;
  box-sizing:border-box;
}
.glass.sig-module{
  flex:0 0 var(--sig-h)!important;
  flex-grow:0!important;
  flex-shrink:0!important;
  height:var(--sig-h)!important;
  min-height:var(--sig-h)!important;
  max-height:var(--sig-h)!important;
  width:100%;
  padding:10px 16px 8px;
  display:flex;flex-direction:column;justify-content:space-between;gap:6px;
  border:1px solid #1e2530;
  background:#0a0d14;
  border-radius:var(--radius);
  box-shadow:inset 0 1px 0 rgba(255,80,100,.06),0 6px 20px rgba(0,0,0,.35);
  overflow:hidden;
  box-sizing:border-box;
}
.sig-module .sig-head{
  display:flex;flex-direction:row;align-items:center;justify-content:space-between;
  gap:12px;margin:0;line-height:1;flex:0 0 auto;
  border:none;padding:0;width:100%;min-width:0;
}
.sig-module .sig-title{
  color:#ff4d5e;font-size:var(--fs-sm);font-weight:700;letter-spacing:.12em;
  text-transform:uppercase;white-space:nowrap;
  text-shadow:0 0 10px rgba(255,77,94,.4);
}
.sig-module .sig-tag{
  color:#5a626c;font-size:var(--fs-2xs);letter-spacing:.1em;font-weight:500;
  white-space:nowrap;text-transform:uppercase;flex:0 0 auto;
}
.sig-module .sig-wave{
  display:flex;align-items:flex-end;justify-content:space-between;
  gap:1px;height:36px;width:100%;min-height:36px;max-height:36px;
  flex:1 1 auto;min-width:0;align-self:stretch;
  border-top:1px solid rgba(255,70,90,.08);
  padding-top:4px;
}
.sig-module .sig-wave > i{
  display:block;flex:1 1 0;min-width:1px;max-width:3px;
  height:calc(var(--h,40) * 1%);
  border-radius:1px 1px 0 0;
  background:linear-gradient(180deg,#ff6a78 0%,#ff3348 55%,#8a1020 100%);
  box-shadow:0 0 3px rgba(255,60,80,.35);
  transform-origin:bottom center;
  animation:sigPulse var(--dur,1.2s) ease-in-out infinite;
  animation-delay:var(--d,0s);
  opacity:.92;
}
/* Day: SIGNAL INTERCEPT → white card, readable crimson (no black slab) */
html[data-theme="day"] .glass.sig-module,
.desk[data-theme="day"] .glass.sig-module{
  background:#FFFFFF!important;
  border:1px solid #E0E0E0!important;
  box-shadow:0 1px 2px rgba(20,30,40,.04),0 6px 16px rgba(20,30,40,.05)!important;
}
html[data-theme="day"] .sig-module .sig-title,
.desk[data-theme="day"] .sig-module .sig-title{
  color:#C5221F!important;
  text-shadow:none!important;
}
html[data-theme="day"] .sig-module .sig-tag,
.desk[data-theme="day"] .sig-module .sig-tag{
  color:#4A4A4A!important;
}
html[data-theme="day"] .sig-module .sig-wave,
.desk[data-theme="day"] .sig-module .sig-wave{
  border-top-color:#F0F0F0!important;
}
html[data-theme="day"] .sig-module .sig-wave > i,
.desk[data-theme="day"] .sig-module .sig-wave > i{
  background:linear-gradient(180deg,#E57373 0%,#D64550 60%,#B71C1C 100%)!important;
  box-shadow:none!important;
}
@keyframes sigPulse{
  0%,100%{transform:scaleY(.42);opacity:.5;}
  40%{transform:scaleY(1);opacity:1;}
  70%{transform:scaleY(.7);opacity:.78;}
}

/* Upper band: THE BALANCE (5.6) | FEED+POS (6.4) — height locked, GAP_SYSTEM */
.desk-top{
  display:grid;
  grid-template-columns:minmax(0,5.6fr) minmax(0,6.4fr);
  gap:var(--gap-system);
  align-items:stretch;
  flex:0 0 auto;
  height:var(--desk-top-h);
  min-height:var(--desk-top-h);
  max-height:var(--desk-top-h);
  overflow:visible;
  margin:0;
}
.desk-top > .glass{
  height:100%;min-height:0;max-height:100%;overflow:hidden;margin:0;
}
.desk-top-right{
  display:flex;flex-direction:column;min-width:0;
  height:100%;min-height:0;max-height:100%;overflow:hidden;margin:0;
}
.desk-top-right > .pair{
  flex:1 1 auto;min-height:0;height:100%;max-height:100%;
  display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);
  gap:var(--gap-system);
  overflow:hidden;align-items:stretch;margin:0;
}
.desk-top-right > .pair > .glass{
  min-width:0;min-height:0;height:100%;max-height:100%;
  display:flex;flex-direction:column;overflow:hidden;margin:0;
}
.desk-top-right > .pair > .glass.feed-card{height:100%;max-height:100%;}
.desk-top-right > .pair > .glass.feed-card > .glass-body{
  flex:1 1 auto;min-height:0;height:auto;max-height:100%;
  display:flex;flex-direction:column;overflow:hidden;
}
.desk-top-right > .pair > .glass:has(.pos-panel){
  padding:var(--pad);background:var(--glass-bg);border:var(--glass-border);
  height:100%;max-height:100%;
}
.ob{width:6px;display:inline-block;} .ob.w{background:var(--green);box-shadow:0 0 3px var(--green);} .ob.l{background:var(--red);}
.chart-slot{flex:1 1 auto;min-height:0;width:100%;display:flex;}
.chart-slot svg{width:100%;height:100%;display:block;}

/* THE BALANCE live endpoint + axes */
.bal-chart{
  position:relative;width:100%;height:100%;flex:1;min-height:0;
  display:grid;grid-template-columns:42px minmax(0,1fr);grid-template-rows:minmax(0,1fr) 16px auto;
  gap:2px 6px;box-sizing:border-box;
}
.bal-y{
  grid-column:1;grid-row:1;display:flex;flex-direction:column;justify-content:space-between;
  align-items:flex-end;padding:28px 0 2px;box-sizing:border-box;min-height:0;
  font-family:'IBM Plex Mono','Courier New',monospace;font-size:12px;line-height:1;
  color:rgba(255,255,255,.4);letter-spacing:.02em;user-select:none;
}
.bal-plot{grid-column:2;grid-row:1;position:relative;min-width:0;min-height:0;display:flex;overflow:hidden;cursor:crosshair;}
.bal-plot > svg{width:100%;height:100%;display:block;}
.bal-x{
  grid-column:2;grid-row:2;display:block;overflow:hidden;
  padding:0 2px;box-sizing:border-box;
  font-family:'IBM Plex Mono','Courier New',monospace;font-size:12px;line-height:1;
  color:rgba(255,255,255,.4);letter-spacing:.02em;user-select:none;
}
.bal-x-in{
  display:flex;justify-content:space-between;align-items:center;
  width:100%;height:100%;transform-origin:left center;
}
.bal-bars{
  grid-column:2;grid-row:3;min-height:16px;width:100%;overflow:hidden;
  box-sizing:border-box;padding:0 2px 0 4px;
}
.bal-bars-fill{
  height:100%;max-width:100%;box-sizing:border-box;
}
.bal-bars-fill .ob-track{width:100%;}
.bal-bars .ob-track{
  display:flex;align-items:flex-end;justify-content:flex-start;
  gap:1.5px;width:100%;min-height:16px;box-sizing:border-box;
  transform-origin:left bottom;
}
.bal-bars .ob-track.sync{
  justify-content:stretch;gap:4px;
}
.bal-bars .ob-track.sync .ob{
  flex:1 1 0;min-width:8px;max-width:none;
}
.bal-mode-row{
  display:inline-flex;gap:6px;align-items:center;margin-left:8px;
  font-size:var(--fs-2xs);letter-spacing:.04em;
}
.bal-mode-row a{
  color:var(--muted);text-decoration:none;border-bottom:1px solid transparent;
  opacity:.75;
}
.bal-mode-row a.on{
  color:var(--green);opacity:1;border-bottom-color:var(--green);
}
[data-testid="stCustomComponentV1"]{
  height:0!important;min-height:0!important;margin:0!important;padding:0!important;
  overflow:hidden!important;border:0!important;
}
.outcome-row{overflow:hidden;}
.outcome-row .ob-track.sync{transform-origin:left bottom;}
.bal-badge-wrap{
  position:absolute;z-index:2;pointer-events:none;
  transform:translate(-50%,calc(-100% - 10px));
}
.bal-badge{
  background:rgba(10,15,12,.85);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  border:1px solid rgba(0,255,102,.4);border-radius:4px;
  padding:4px 8px;white-space:nowrap;
  color:#00ff66;font-size:var(--fs-sm);font-weight:700;letter-spacing:.04em;
  font-family:'IBM Plex Mono','Courier New',monospace;
  text-shadow:0 0 8px rgba(0,255,102,.45);
  box-shadow:0 0 12px rgba(0,255,102,.12);
}
.bal-badge.dn{
  border-color:rgba(255,61,110,.45);color:#ff3d6e;
  text-shadow:0 0 8px rgba(255,61,110,.4);
  box-shadow:0 0 12px rgba(255,61,110,.12);
}
/* Perfect-circle endpoint — HTML, not SVG (SVG stretch would ovalize it) */
.bal-dot-wrap{
  position:absolute;z-index:3;pointer-events:none;
  width:16px;height:16px;
  transform:translate(-50%,-50%);
}
.bal-dot{
  position:absolute;left:50%;top:50%;
  width:8px;height:8px;margin:-4px 0 0 -4px;
  border-radius:50%;
  background:#ffffff;
  box-shadow:
    0 0 0 2px var(--dot,#00ff66),
    0 0 10px var(--dot,#00ff66),
    0 0 20px color-mix(in srgb,var(--dot,#00ff66) 55%,transparent);
}
.bal-dot-halo{
  position:absolute;left:50%;top:50%;
  width:16px;height:16px;margin:-8px 0 0 -8px;
  border-radius:50%;
  border:1.5px solid var(--dot,#00ff66);
  box-sizing:border-box;
  animation:balRipple 1.8s ease-out infinite;
}
.bal-dot-halo.delay{animation-delay:.9s;}
@keyframes balRipple{
  0%{transform:scale(.55);opacity:.7;}
  70%{transform:scale(2.15);opacity:0;}
  100%{transform:scale(2.15);opacity:0;}
}

/* Agents stage — cartoon trading floor (robot-office desks) */
.agents-panel{
  flex:1;min-height:0;height:100%;
  display:flex;flex-direction:column;margin:0;gap:0;
}
.agents-panel .ph{
  margin:0 0 var(--gap-system);padding-bottom:8px;flex:0 0 auto;
}
.stage{
  position:relative;flex:1;min-height:0;overflow:hidden;
  border-radius:8px;
  background:
    radial-gradient(ellipse at 50% 90%,rgba(20,40,80,.35) 0%,transparent 50%),
    linear-gradient(180deg,#0a1020 0%,#121c34 45%,#0e1830 100%);
  display:flex;align-items:center;justify-content:center;
}
.stage:before{
  content:"";position:absolute;inset:8% 4% 6%;
  background:
    repeating-linear-gradient(90deg,rgba(40,70,120,.22) 0 1px,transparent 1px 22px),
    repeating-linear-gradient(0deg,rgba(40,70,120,.18) 0 1px,transparent 1px 22px);
  opacity:.5;border-radius:4px;pointer-events:none;
}
.stage:after{
  content:"";position:absolute;inset:0;
  background:radial-gradient(ellipse at 50% 40%,transparent 40%,rgba(0,0,0,.35) 100%);
  pointer-events:none;
}
.agents.floor{
  position:relative;z-index:1;display:flex;justify-content:space-around;align-items:flex-end;
  width:100%;box-sizing:border-box;padding:22px 4px 8px;gap:4px;
}
.agent{
  text-align:center;flex:1 1 0;min-width:0;position:relative;
  display:flex;flex-direction:column;align-items:center;
}
.agent .bubble{
  position:absolute;top:0;left:50%;transform:translateX(-50%) translateY(4px);
  white-space:nowrap;font-size:var(--fs-2xs);font-weight:700;letter-spacing:.04em;
  color:var(--c,#00ff66);
  background:color-mix(in srgb,var(--c,#00ff66) 12%,rgba(0,0,0,.88));
  border:1px solid color-mix(in srgb,var(--c,#00ff66) 55%,transparent);
  border-radius:999px;
  padding:.18rem .45rem;z-index:3;max-width:96%;
  overflow:hidden;text-overflow:ellipsis;
  box-shadow:0 0 10px color-mix(in srgb,var(--c,#00ff66) 28%,transparent);
  text-shadow:0 0 6px color-mix(in srgb,var(--c,#00ff66) 35%,transparent);
  opacity:0;pointer-events:none;
  animation:bubbleIn .4s ease forwards;
}
.agent .bubble.out{animation:bubbleOut .55s ease forwards;}
.agent .bubble.sticky{opacity:1;animation:none;transform:translateX(-50%) translateY(0);}
@keyframes bubbleIn{
  from{opacity:0;transform:translateX(-50%) translateY(6px);}
  to{opacity:1;transform:translateX(-50%) translateY(0);}
}
@keyframes bubbleOut{
  from{opacity:1;transform:translateX(-50%) translateY(0);}
  to{opacity:0;transform:translateX(-50%) translateY(-4px);}
}
.agent .station{
  width:100%;max-width:132px;aspect-ratio:92/108;margin:16px auto 2px;
  filter:drop-shadow(0 8px 12px rgba(0,0,0,.5));
}
.agent .station svg{width:100%;height:100%;display:block;}
.agent .trader-bob{transform-origin:46px 72px;animation:traderBob 1.4s ease-in-out infinite;}
.agent:nth-child(2) .trader-bob{animation-delay:.15s;}
.agent:nth-child(3) .trader-bob{animation-delay:.3s;}
.agent:nth-child(4) .trader-bob{animation-delay:.45s;}
.agent:nth-child(5) .trader-bob{animation-delay:.6s;}
/* Left/right hands: independent irregular key-hop (not synced metronome) */
.agent .trader-type{transform-origin:center;}
.agent .trader-type.lh{
  animation:traderTypeL var(--type-l-dur,.42s) steps(1,end) infinite;
  animation-delay:var(--type-l-del,0s);
}
.agent .trader-type.rh{
  animation:traderTypeR var(--type-r-dur,.58s) steps(1,end) infinite;
  animation-delay:var(--type-r-del,.12s);
}
.agent:nth-child(1){--type-l-dur:.34s;--type-r-dur:.51s;--type-l-del:0s;--type-r-del:.19s;}
.agent:nth-child(2){--type-l-dur:.47s;--type-r-dur:.39s;--type-l-del:.22s;--type-r-del:.05s;}
.agent:nth-child(3){--type-l-dur:.41s;--type-r-dur:.63s;--type-l-del:.08s;--type-r-del:.31s;}
.agent:nth-child(4){--type-l-dur:.55s;--type-r-dur:.36s;--type-l-del:.27s;--type-r-del:.11s;}
.agent:nth-child(5){--type-l-dur:.38s;--type-r-dur:.49s;--type-l-del:.14s;--type-r-del:.33s;}
.agent.busy-hi .trader-bob{animation-duration:.85s;}
.agent.busy-hi{--type-l-dur:.22s;--type-r-dur:.28s;}
.agent.busy-lo .trader-bob{animation-duration:1.8s;}
.agent.busy-lo{--type-l-dur:.72s;--type-r-dur:.88s;}
.agent.flash-risk{
  animation:riskFlash .4s ease-in-out infinite;
}
@keyframes riskFlash{
  0%,100%{filter:brightness(1);}
  50%{filter:brightness(1.55) drop-shadow(0 0 10px #ff3d6e);}
}
.pos-bar.bar-dc{
  border-color:#ff3d6e!important;color:#ff3d6e!important;
  box-shadow:0 0 12px rgba(255,61,110,.35);
  animation:dcPulse 1.1s ease-in-out infinite;
}
@keyframes dcPulse{
  0%,100%{opacity:.75;}
  50%{opacity:1;}
}
/* Debug sync toolbar (Streamlit buttons) */
.desk-sync-tools{margin-top:8px;}
div[data-testid="stHorizontalBlock"] button{
  font-family:'IBM Plex Mono',monospace!important;
  font-size:var(--fs-xs)!important;letter-spacing:.04em;
}
@keyframes traderBob{
  0%,100%{transform:translateY(0);}
  50%{transform:translateY(-1.8px);}
}
@keyframes traderTypeL{
  0%{transform:translate(0,0) scale(1);opacity:.95;}
  18%{transform:translate(1.6px,-.7px) scale(.82);opacity:.55;}
  37%{transform:translate(-1.1px,.9px) scale(1.12);opacity:1;}
  61%{transform:translate(2.2px,.35px) scale(.9);opacity:.7;}
  82%{transform:translate(-.6px,1.1px) scale(1.05);opacity:.9;}
  100%{transform:translate(0,0) scale(1);opacity:.95;}
}
@keyframes traderTypeR{
  0%{transform:translate(0,0) scale(1);opacity:.9;}
  14%{transform:translate(-1.4px,.8px) scale(1.08);opacity:1;}
  33%{transform:translate(1.9px,-.5px) scale(.85);opacity:.5;}
  52%{transform:translate(-.4px,1.15px) scale(1.1);opacity:.95;}
  74%{transform:translate(1.3px,.2px) scale(.92);opacity:.65;}
  100%{transform:translate(0,0) scale(1);opacity:.9;}
}
.agent .nm{color:var(--c);font-size:var(--fs-2xs);letter-spacing:.06em;font-weight:700;margin-top:.2rem;}
.agent .ds{color:var(--muted);font-size:var(--fs-2xs);line-height:1.2;margin-top:.02rem;}

/* Consensus — fig.1: 3 circular meters in a row */
.ac-list{
  display:flex;flex-direction:row;justify-content:space-around;align-items:center;
  flex:1 1 auto;height:100%;min-height:0;gap:10px;padding:6px 4px 2px;
  box-sizing:border-box;width:100%;
}
.ac-row{
  display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:24px;min-width:0;flex:1 1 0;text-align:center;
}
.ac-row .ring{
  --p:50;--c:var(--green);
  width:72px;height:72px;flex:0 0 72px;aspect-ratio:1/1;border-radius:50%;
  background:
    radial-gradient(circle at center,var(--ring-core,#050805) 0 56%,transparent 57%),
    conic-gradient(var(--c) calc(var(--p)*1%),var(--ring-track,#1a1a1a) 0);
  display:flex;align-items:center;justify-content:center;
  font-size:1.05rem;font-weight:700;color:var(--c);letter-spacing:.02em;
  box-shadow:0 0 14px color-mix(in srgb,var(--c) 38%,transparent);
  box-sizing:border-box;
}
.ac-txt{display:flex;flex-direction:column;justify-content:center;align-items:center;gap:4px;min-width:0;}
.ac-title{color:#ffffff;font-size:var(--fs-sm);font-weight:700;letter-spacing:.06em;text-transform:uppercase;line-height:1.2;}
.ac-sub{color:#6a7a6a;font-size:var(--fs-xs);letter-spacing:.02em;text-transform:lowercase;line-height:1.3;}
/* Day consensus — white cores, soft accents, no neon glow */
html[data-theme="day"] .ac-row .ring,
.desk[data-theme="day"] .ac-row .ring{
  --ring-core:#FFFFFF;
  --ring-track:#E6E8EA;
  box-shadow:0 2px 8px rgba(20,30,40,.08)!important;
}
html[data-theme="day"] .ac-title,
.desk[data-theme="day"] .ac-title{color:#1A1A1A!important;}
html[data-theme="day"] .ac-sub,
.desk[data-theme="day"] .ac-sub{color:#4A4A4A!important;}
.dock .ac-wrap{flex:1;min-height:0;height:100%;display:flex;flex-direction:column;}
.dock .ac-wrap .ph{margin-bottom:8px;padding-bottom:6px;}

.pos-panel{
  display:flex;flex-direction:column;justify-content:flex-start;
  gap:0;width:100%;height:100%;min-height:0;max-height:100%;
  padding:2px 2px 0;box-sizing:border-box;overflow:hidden;background:transparent;
}
.pos-label{
  flex:0 0 auto;margin:0 0 10px;
  color:#c8d0d8;font-size:var(--fs-2xs);font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;line-height:1.2;
}
.pos-hero{
  display:flex;justify-content:space-between;align-items:baseline;gap:10px;
  flex:0 0 auto;margin:0 0 14px;min-width:0;
}
.pos-hero .tok{
  font-size:var(--fs-2xl);font-weight:800;letter-spacing:.02em;line-height:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:62%;
}
.pos-hero .pct{
  flex:0 0 auto;font-size:var(--fs-2xl);font-weight:800;letter-spacing:.02em;line-height:1;
  white-space:nowrap;
}
.pos-hero .pct.hidden{visibility:hidden;}
.pos-buy-wrap{flex:0 0 auto;margin:0 0 16px;width:100%;}
.pos-btn{
  display:flex;align-items:center;justify-content:center;text-align:center;
  width:100%;box-sizing:border-box;padding:18px 16px;border-radius:12px;
  font-weight:800;letter-spacing:.18em;font-size:1rem;text-transform:uppercase;
  line-height:1.1;min-height:52px;
  transition:box-shadow .3s ease,filter .3s ease,transform .3s ease;
}
.pos-btn.press{animation:buyBreath 1.1s ease-in-out infinite;}
@keyframes buyBreath{
  0%,100%{transform:scale(1);filter:brightness(1);}
  50%{transform:scale(1.02);filter:brightness(1.08);}
}
@keyframes armCursor{
  0%,49%{opacity:1;}
  50%,100%{opacity:0;}
}
@keyframes feedBuyFlash{
  0%,100%{background:rgba(0,255,102,.05);}
  50%{background:rgba(0,255,102,.14);}
}
.pos-rows{
  display:flex;flex-direction:column;justify-content:flex-start;
  flex:1 1 auto;width:100%;margin:0;min-height:0;gap:0;
}
.pos-row{
  display:flex;justify-content:space-between;align-items:center;gap:12px;
  padding:12px 0;border-bottom:1px solid rgba(255,255,255,.08);
  line-height:1.15;width:100%;box-sizing:border-box;
}
.pos-row:last-child{border-bottom:none;}
.pos-row .k{
  color:#8a9490;flex-shrink:0;text-transform:lowercase;letter-spacing:.04em;
  font-size:var(--fs-sm);font-weight:500;
}
.pos-row .v{
  text-align:right;flex:1 1 auto;min-width:0;font-weight:700;font-size:var(--fs-md);
  letter-spacing:.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  display:inline-flex;align-items:center;justify-content:flex-end;gap:4px;color:#f2f5f3;
}
.pos-row .v.entry{font-size:var(--fs-lg);font-weight:800;color:#ffffff;}
.pos-row .v .unit{color:#64748B;font-size:var(--fs-xs);font-weight:500;margin-left:2px;}
.pos-row .v.dim{color:#8a9490;font-weight:500;font-size:var(--fs-sm);}
.pos-row .v .arming{
  color:#00e1ff;font-size:var(--fs-xs);font-weight:700;letter-spacing:.06em;
  background:rgba(0,225,255,.1);border:1px solid rgba(0,225,255,.22);
  padding:4px 9px;border-radius:999px;text-transform:uppercase;
}
.pos-row .v .arming .cursor{display:inline-block;animation:armCursor 1s steps(1) infinite;}
.pos-row .v.muted{color:#94a3b8;opacity:.85;}
.pos-foot{flex:0 0 auto;margin-top:auto;padding-top:12px;width:100%;}
.pos-bar{
  display:flex;align-items:center;justify-content:center;width:100%;
  box-sizing:border-box;padding:12px 12px;border-radius:10px;
  font-size:var(--fs-sm);font-weight:800;letter-spacing:.1em;text-transform:uppercase;
  line-height:1.1;border:1px solid transparent;min-height:40px;
}

/* —— Panel themes (PANEL_THEMES class map) —— */
.theme-hold .tok,.theme-hold .pct{color:#e0a96d;text-shadow:none;}
.theme-hold .pos-btn{
  background:transparent;border:1px solid #2a3238;color:#64748B;box-shadow:none;
}
.theme-hold .pos-bar{
  background:transparent;color:#e0a96d;border-color:rgba(224,169,109,.45);
}

.theme-voting .tok,.theme-voting .pct{color:#ff4d5e;text-shadow:none;}
.theme-voting .pos-btn{
  background:transparent;border:1.5px solid rgba(255,77,94,.7);color:#ff4d5e;box-shadow:none;
}
.theme-voting .pos-bar{
  background:transparent;color:#ff4d5e;border-color:rgba(255,77,94,.45);
}

.theme-buy .tok,.theme-buy .pct{color:#00ff88;text-shadow:0 0 14px rgba(0,255,136,.28);}
.theme-buy .pos-btn{
  background:linear-gradient(90deg,#00ff88 0%,#22ff9a 100%);color:#041208;border:none;
  box-shadow:0 0 20px rgba(0,255,136,.4);
}
.theme-buy .pos-bar{
  background:transparent;color:#00ff88;border-color:rgba(0,255,136,.4);
}

.theme-arming .tok,.theme-arming .pct{color:#00ff88;text-shadow:0 0 14px rgba(0,255,136,.28);}
.theme-arming .pos-btn{
  background:linear-gradient(90deg,#00ff88 0%,#00e1ff 100%);color:#041208;border:none;
  box-shadow:0 0 22px rgba(0,255,136,.4),0 0 40px rgba(0,225,255,.18);
}
.theme-arming .pos-bar{
  background:transparent;color:#00ff88;border-color:rgba(36,49,60,.95);
}

.theme-stopped .tok,.theme-stopped .pct{color:#ff4d5e;text-shadow:none;}
.theme-stopped .pos-btn{
  background:transparent;border:1.5px solid rgba(255,77,94,.75);color:#ff4d5e;box-shadow:none;
}
.theme-stopped .pos-bar{
  background:transparent;color:#ff4d5e;border-color:rgba(255,77,94,.55);
}
.theme-stopped .pos-row .v{color:#d0d4d8;}
.theme-stopped .pos-row .v.entry{color:#ffffff;}

.edge-card{
  overflow:hidden;padding:16px;
  display:grid;grid-template-columns:max-content minmax(0,1fr);
  grid-template-rows:auto auto;column-gap:12px;row-gap:6px;align-items:start;
}
.edge-head{grid-column:1;grid-row:1;margin:0;min-width:0;}
.edge-title{
  color:#ffffff;font-size:var(--fs-md);font-weight:700;letter-spacing:.08em;
  text-transform:uppercase;line-height:1.2;margin:0 0 4px;
}
.edge-sub{color:#6a7a6a;font-size:var(--fs-xs);letter-spacing:.02em;line-height:1.35;text-transform:none;font-weight:400;}
.edge-body{display:contents;}
.edge-left{
  grid-column:1;grid-row:2;
  display:flex;flex-direction:column;gap:6px;min-width:0;justify-content:flex-start;
}
.edge-formula{color:#7CFF9A;font-size:var(--fs-md);font-weight:500;letter-spacing:.02em;line-height:1.35;}
.edge-meta{color:#6a7a6a;font-size:var(--fs-xs);line-height:1.35;}
.edge-exp{font-size:var(--fs-xl);font-weight:700;letter-spacing:.02em;line-height:1.15;margin-top:2px;}
.edge-exp.pos{color:#7CFF9A;}
.edge-exp.neg{color:#FF6B7A;}
.edge-stats{
  grid-column:2;grid-row:1 / span 2;
  display:flex;flex-wrap:nowrap;
  justify-content:space-around;align-items:flex-start;align-self:center;
  justify-self:center;
  height:auto;min-height:0;margin:0;
  padding:4px 8px;box-sizing:border-box;
  width:100%;max-width:30rem;
}
.edge-stat{
  display:flex;flex-direction:column;justify-content:flex-start;align-items:flex-start;
  gap:4px;flex:0 0 auto;min-width:7.75rem;box-sizing:border-box;
  padding:0;text-align:left;
}
.edge-stat .lbl{
  color:#6a7a6a;font-size:var(--fs-xs);letter-spacing:.03em;
  text-transform:lowercase;line-height:1.2;text-align:left;width:100%;
}
.edge-stat .val{
  font-size:var(--fs-2xl);font-weight:800;line-height:1;margin-top:0;
  letter-spacing:-.02em;text-align:left;width:100%;
  white-space:nowrap;
}
.edge-stat .val.pos{color:#7CFF9A;}
.edge-stat .val.neg{color:#FF6B7A;}
.edge-stat .ebar{
  height:3px;width:5.5rem;max-width:100%;background:#1a221a;border-radius:2px;overflow:hidden;margin-top:4px;
}
.edge-stat .ebar>i{display:block;height:100%;border-radius:2px;}
.edge-stat .ebar>i.g{background:#7CFF9A;}
.edge-stat .ebar>i.r{background:#FF6B7A;}

/* Wallet / sizing bottom twin — footer row always reserved */
.wc-panel,.sz-panel{
  display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;gap:0;
  flex:1;min-height:0;height:100%;width:100%;
}
.sz-panel .ph,.wc-panel .ph{margin-bottom:4px;padding-bottom:5px;}
.wc-sub,.sz-sub{
  color:#6a7a6a;font-size:var(--fs-2xs);margin:0 0 6px;
  font-family:'IBM Plex Mono','Courier New',monospace;letter-spacing:.02em;line-height:1.3;
}
.sz-sub{display:flex;justify-content:space-between;align-items:baseline;gap:8px;}
.wc-body{position:relative;min-height:0;display:flex;overflow:hidden;}
.wc-body .chart-slot{flex:1;min-height:0;height:100%;}
.wc-flag{
  position:absolute;right:4px;top:50%;transform:translateY(-50%);
  text-align:right;pointer-events:none;
}
.wc-flag .lbl{color:#6a7a6a;font-size:var(--fs-2xs);line-height:1.25;}
.wc-flag .val{color:#7CFF9A;font-size:var(--fs-md);font-weight:700;line-height:1.2;margin-top:2px;}
.wc-pressure{
  display:flex;align-items:center;gap:8px;margin-top:6px;
}
.wc-pressure .lbl{color:#6a7a6a;font-size:var(--fs-2xs);flex-shrink:0;white-space:nowrap;}
.wc-pressure .ebar{
  flex:1;height:3px;background:#122012;border-radius:2px;overflow:hidden;min-width:40px;
}
.wc-pressure .ebar>i{display:block;height:100%;background:#7CFF9A;border-radius:2px;}
.sz-panel .chart-slot{min-height:0;}
.sz-stats{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;
  margin-top:6px;padding-top:0;
}
.sz-stats .cell{display:flex;flex-direction:column;gap:3px;min-width:0;}
.sz-stats .lbl{
  color:#6a7a6a;font-size:var(--fs-2xs);font-weight:500;text-transform:lowercase;
  font-family:'IBM Plex Mono','Courier New',monospace;letter-spacing:.03em;line-height:1.2;
}
.sz-stats .val{
  font-size:var(--fs-md);font-weight:700;line-height:1;letter-spacing:.02em;
  font-family:'IBM Plex Mono','Courier New',monospace;
}
.sz-stats .val.y{color:#FFB86C;}
.sz-stats .val.g{color:#5EEAD4;}
.sz-stats .val.r{color:#FF6B7A;}

/* RH text panels — column stack, no SVG overlay chrome */
.wc-body.rh-text,.sz-body.rh-text{
  display:flex;flex-direction:column;justify-content:center;gap:8px;
  min-height:0;overflow:hidden;padding:2px 0 4px;
}
.rh-kicker{
  color:var(--hi,#e8ffe8);font-size:var(--fs-sm);font-weight:700;letter-spacing:.04em;
  line-height:1.25;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.rh-line{
  color:var(--muted,#6a8a6a);font-size:var(--fs-xs);line-height:1.35;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}
.rh-meta{
  display:flex;flex-wrap:wrap;gap:10px 14px;align-items:baseline;
  color:#6a7a6a;font-size:var(--fs-2xs);letter-spacing:.03em;
}
.rh-meta b{color:#7CFF9A;font-weight:700;font-size:var(--fs-sm);}
.rh-meta b.warn{color:#FFB86C;}
.rh-meta b.bad{color:#FF6B7A;}
.sz-panel .sz-body.rh-text{grid-row:3;}
.sz-panel .sz-stats.rh-stats{margin-top:0;}

/* Manifold / embedding — HTML titles left-aligned like other panels */
.mf-panel,.emb-panel{
  display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;gap:0;
  flex:1;min-height:0;height:100%;width:100%;
}
.mf-panel .ph,.emb-panel .ph{
  display:flex!important;justify-content:flex-start;align-items:center;
  margin:0 0 4px;padding:0 0 6px;text-align:left;
  font-size:var(--fs-sm);letter-spacing:.1em;width:100%;
}
.mf-sub,.emb-sub{
  display:block!important;text-align:left;
  color:#8a9490;font-size:var(--fs-xs);margin:0 0 6px;line-height:1.35;
  font-family:'IBM Plex Mono','Courier New',monospace;letter-spacing:.02em;
}
.mf-panel .chart-slot,.emb-panel .chart-slot{
  position:relative;min-height:0;overflow:hidden;flex:1 1 auto;
}
.mf-panel .chart-slot svg,.emb-panel .chart-slot svg{
  width:100%;height:100%;min-height:96px;display:block;
}
.mf-foot,.emb-foot{
  display:flex;justify-content:space-between;align-items:baseline;gap:10px;
  margin-top:8px;font-size:var(--fs-xs);line-height:1.3;
  font-family:'IBM Plex Mono','Courier New',monospace;
}
.mf-foot .g,.emb-foot .g{color:#7CFF9A;font-weight:700;font-size:var(--fs-sm);}
.mf-foot .dim,.emb-foot .dim{color:#8a9a8a;font-size:var(--fs-xs);}
.emb-foot .r{color:#FF5566;font-weight:700;font-size:var(--fs-sm);}
html[data-theme="day"] .mf-sub,
.desk[data-theme="day"] .mf-sub,
html[data-theme="day"] .emb-sub,
.desk[data-theme="day"] .emb-sub{color:#4A4A4A!important;}
html[data-theme="day"] .ac-title,
.desk[data-theme="day"] .ac-title{color:#1A1A1A!important;}
html[data-theme="day"] .sig-module .sig-tag,
.desk[data-theme="day"] .sig-module .sig-tag{color:#4A4A4A!important;}

/* Feed colors / scan */
.feed .dim{color:var(--muted);}
.scan-wrap{flex:1;min-height:0;display:flex;flex-direction:column;gap:8px;overflow:hidden;}
.scan-grid{
  display:grid;grid-template-columns:repeat(10,minmax(0,1fr));
  grid-template-rows:repeat(8,minmax(0,1fr));
  gap:2px;flex:1;align-content:stretch;overflow:hidden;min-height:0;height:100%;
}
.sg{aspect-ratio:auto;width:100%;height:100%;min-width:0;min-height:0;background:#0a120a;border:1px solid #122012;}
.sg.on{background:var(--green);box-shadow:0 0 4px var(--green);} .sg.hot{background:#39ff14;}
.sg.bad{background:#ff3d6e88;} .sg.mid{background:#1a4a1a;}
/* Day SCAN GRID — saturated cells readable on white */
html[data-theme="day"] .sg,
.desk[data-theme="day"] .sg{
  background:#E8EAED!important;
  border:1px solid #DADCE0!important;
  box-shadow:none!important;
}
html[data-theme="day"] .sg.off,
.desk[data-theme="day"] .sg.off{
  background:#F1F3F4!important;
  border-color:#DADCE0!important;
}
html[data-theme="day"] .sg.mid,
.desk[data-theme="day"] .sg.mid{
  background:#81C995!important;
  border-color:#34A853!important;
}
html[data-theme="day"] .sg.on,
.desk[data-theme="day"] .sg.on{
  background:#34A853!important;
  border-color:#137333!important;
  box-shadow:none!important;
}
html[data-theme="day"] .sg.hot,
.desk[data-theme="day"] .sg.hot{
  background:#0D652D!important;
  border-color:#0B4A22!important;
}
html[data-theme="day"] .sg.bad,
.desk[data-theme="day"] .sg.bad{
  background:#EA4335!important;
  border-color:#C5221F!important;
}
html[data-theme="day"] .bar,
.desk[data-theme="day"] .bar{
  background:#DADCE0!important;
}
html[data-theme="day"] .bar>i,
.desk[data-theme="day"] .bar>i{
  background:linear-gradient(90deg,#137333,#34A853)!important;
  box-shadow:none!important;
}

.footer .who{color:var(--green);flex-shrink:0;}
.footer .time{color:var(--muted);flex-shrink:0;}
.bar{flex:1;height:4px;background:#122012;border-radius:2px;overflow:hidden;min-width:40px;}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--green),#39ff14);box-shadow:0 0 8px var(--green);}

@media (max-width:1100px){
  [data-testid="stAppViewContainer"],section.main,.block-container{
    height:auto!important;min-height:100vh!important;overflow:auto!important;}
  .desk{width:100%;height:auto;max-height:none;gap:var(--gap-system);}
  .topbar{flex-wrap:wrap;row-gap:10px;}
  .topbar .metrics{width:100%;padding-left:0;}
  .topbar .metrics-grid{grid-template-columns:repeat(3,minmax(0,1fr));}
  .desk-score{grid-template-columns:repeat(2,minmax(0,1fr));}
  .desk-top{
    grid-template-columns:1fr;height:auto;min-height:0;max-height:none;
    flex:0 0 auto;overflow:visible;
  }
  .desk-top > .glass,.desk-top-right{
    height:auto;min-height:280px;max-height:none;
  }
  .desk-top-right > .pair{height:auto;min-height:220px;max-height:none;grid-template-columns:1fr;}
  .desk-top-right > .pair > .glass{height:auto;min-height:220px;max-height:none;}
  .feed{max-height:280px;}
  .desk-body{grid-template-columns:1fr;height:auto;min-height:0;flex:0 0 auto;}
  .desk-left{
    display:flex;flex-direction:column;
    gap:var(--gap-system);row-gap:var(--gap-system);
    height:auto;min-height:0;
  }
  .desk-left > .glass:has(.agents-panel){
    height:var(--agents-h);min-height:var(--agents-h);max-height:var(--agents-h);
    padding:var(--pad);
  }
  .desk-right{
    display:flex;flex-direction:column;gap:var(--gap-system);
    height:auto;overflow:visible;
  }
  .desk-right > .pair{height:auto;min-height:180px;overflow:visible;}
  .desk-right > .pair > .glass{height:auto;min-height:180px;overflow:hidden;}
  .desk-left .dock{flex:0 0 auto;height:auto;min-height:var(--dock-h);max-height:none;}
  .desk-left .dock > .glass{height:auto;min-height:var(--dock-h);overflow:hidden;padding:var(--pad);}
}
</style>
""",
    unsafe_allow_html=True,
)


def resolve_desk_theme(ss) -> str:
    """day | night — auto by local clock unless DESK_THEME / session override."""
    mode = str(ss.get("theme_mode") or os.environ.get("DESK_THEME", "auto")).lower()
    if mode in ("day", "night"):
        return mode
    hour = datetime.now().astimezone().hour
    return "day" if 7 <= hour < 19 else "night"


def inject_theme_vars(theme: str) -> None:
    """Re-apply :root tokens each fragment — module CSS defaults to night :root."""
    if theme == "day":
        block = """
<style>
:root, html{
  --app-bg:#F5F7F9;--glass-bg:#FFFFFF;--glass-border:1px solid #E0E0E0;
  --glass-blur:blur(8px);--glass-shadow:0 1px 2px rgba(20,30,40,.04),0 8px 20px rgba(20,30,40,.05);
  --line:rgba(20,30,40,.14);--muted:#4A4A4A;--text:#1A1A1A;--hi:#111111;
  --green:#137333;--red:#C5221F;--yellow:#B06000;--blue:#1967D2;
  --topbar-bg:#FFFFFF;--topbar-border:#E0E0E0;
  --kv-bg:#F7F8FA;--kv-border:#DADCE0;--live:#137333;
  --theme-label:DAY;
}
html,body,[data-testid="stAppViewContainer"],.stApp,.main{background:var(--app-bg)!important;color:var(--text)!important;}
</style>
"""
    else:
        block = """
<style>
:root, html{
  --app-bg:#000000;--glass-bg:#0a0d14;--glass-border:1px solid #1e2530;
  --glass-blur:blur(16px);--glass-shadow:0 8px 32px 0 rgba(0,0,0,.37);
  --line:rgba(255,255,255,.06);--muted:#6a8a6a;--text:#c8ffd8;--hi:#e8ffe8;
  --green:#00ff66;--red:#ff3d6e;--yellow:#ffcc33;--blue:#4da3ff;
  --topbar-bg:#000000;--topbar-border:rgba(255,255,255,.1);
  --kv-bg:rgba(255,255,255,.02);--kv-border:rgba(255,255,255,.05);--live:#52ff8c;
  --theme-label:NIGHT;
}
html,body,[data-testid="stAppViewContainer"],.stApp,.main{background:var(--app-bg)!important;color:var(--text)!important;}
</style>
"""
    st.markdown(block, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Log bridge
# ---------------------------------------------------------------------------


def read_log(limit: int = 900) -> list[dict]:
    if not LOG.exists():
        return []
    rows = []
    with LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows[-limit:]


def write_log(**rec) -> None:
    from desk_realtime.secrets import redact_text, scrub_mapping

    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    # Ensure agent_type rides on detail for desk → robot bubble routing
    detail = rec.get("detail")
    if isinstance(detail, dict) and detail.get("agent_type"):
        detail["agent_type"] = str(detail["agent_type"]).upper()
    # Never let secrets / key-shaped blobs into Desk Feed jsonl
    safe = scrub_mapping(dict(rec))
    for k, v in list(safe.items()):
        if isinstance(v, str):
            safe[k] = redact_text(v)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(safe, ensure_ascii=False) + "\n")


def _tx_locked(kind: str, window: float = 8.0) -> bool:
    """True while a BUY/SELL/PANIC click is still in flight."""
    until = float(st.session_state.get(f"tx_lock_{kind}") or 0)
    return time.time() < until


def _lock_tx(kind: str, window: float = 8.0) -> None:
    """8s click lock only. Does not broadcast or flatten."""
    st.session_state[f"tx_lock_{kind}"] = time.time() + window


def execute_panic_exit(position: dict | None) -> None:
    """UI → engine IPC: Valve Gate CLOSED + desk.panic flatten request.
    Trading loop (separate process) executes the sell. UI does not sign.
    """
    from desk_realtime.engine_state import request_panic, set_valve
    from desk_realtime.secrets import redact_text

    ss = st.session_state
    ss.halted = True
    DeskBus.set_halted(True)
    DeskBus.set_metrics({
        "desk_mode": "HALTED",
        "agents_live": 0,
        "panic": True,
        "valve_gate": "CLOSED",
    })

    sym = ""
    addr = ""
    if position:
        sym = str(position.get("token") or "").lstrip("$")
        addr = str(position.get("token_address") or position.get("mint") or "")
        entry = float(position.get("entry") or ENTRY)
        value = float(position.get("value") or entry)
        mult = float(position.get("mult") or 1.0)
        pnl = round(value - entry, 4)
        # Immediate UI close row (engine will also emit EXIT when sell lands)
        write_log(
            type="close",
            market="crypto",
            symbol=sym,
            pnl=pnl,
            detail={
                "bot": "exit_manager",
                "event": "EXIT",
                "agent_type": "EXIT",
                "mult": round(mult, 2),
                "panel": "STOPPED_OUT",
                "panic": True,
                "order": "market",
                "note": "PANIC SELL · market flatten requested",
            },
        )
        inject_local([{
            "status": "STOPPED_OUT",
            "agent_type": "EXIT",
            "token_name": f"${sym}",
            "log_text": redact_text(
                f"PANIC SELL ${sym} · market flatten {pnl:+.3f} {QUOTE} · valve CLOSED"
            ),
            "multiplier": mult,
        }])
    else:
        inject_local([{
            "status": "HOLD_OFF",
            "agent_type": "EXIT",
            "token_name": "$DESK",
            "log_text": "PANIC · no open book · valve CLOSED",
        }])

    set_valve(True)
    request_panic(symbol=sym, token_address=addr, reason="ui_panic")

    write_log(
        type="action",
        market="crypto",
        symbol="DESK",
        action="PANIC",
        reason="manual panic exit · valve CLOSED · auto-buy paused",
        detail={"event": "PANIC", "agent_type": "EXIT", "panel": "STOPPED_OUT"},
    )


def _short_bubble(text: str, limit: int = 34) -> str:
    t = " ".join(str(text).split())
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip(" ·-—,") + "…"


def route_agent_bubble(
    msg: str,
    kind: str = "",
    detail: dict | None = None,
) -> tuple[str, str, bool] | None:
    """Map a desk-feed line to (agent_type, bubble_text, sticky)."""
    d = detail if isinstance(detail, dict) else {}
    raw = str(msg or "")
    m = raw.lower()
    explicit = str(d.get("agent_type") or "").upper()
    agents = {"SCANNER", "NARRATIVE", "RISK", "TIMING", "EXIT"}

    # EXIT close / lock
    if ("exit" in m and "closed" in m) or d.get("event") == "EXIT":
        mult = d.get("mult")
        if mult is None:
            # EXIT $SOLCAT closed +22.5x
            hit = re.search(r"\+(\d+(?:\.\d+)?)x", raw, re.I)
            if not hit:
                hit = re.search(r"(\d+(?:\.\d+)?)x", raw, re.I)
            mult = hit.group(1) if hit else None
        if mult is not None:
            try:
                mult_s = f"{float(mult):.0f}"
            except (TypeError, ValueError):
                mult_s = str(mult)
            return ("EXIT", f"exit fired · {mult_s}x locked", True)
        return ("EXIT", _short_bubble(raw.replace("EXIT ", "exit ")), False)

    # NARRATIVE veto / score
    if "off-narrative" in m or "off_narrative" in m:
        return ("NARRATIVE", "NOT BUY · off-narrative", False)
    if m.startswith("score") or "theme match" in m:
        return ("NARRATIVE", _short_bubble(raw.replace("SCORE ", "")), False)
    if explicit == "NARRATIVE":
        return ("NARRATIVE", _short_bubble(raw), False)

    # RISK veto / Arc audit tiers
    if (
        "risk veto" in m
        or "book too thin" in m
        or "thin_liquidity" in m
        or "audit" in m
        or "待复核" in raw
        or "拒绝" in raw
        or "可看" in raw
        or explicit == "RISK"
    ):
        if "可看" in raw:
            return ("RISK", _short_bubble(raw), False)
        if "待复核" in raw or "unknown" in m:
            return ("RISK", "AUDIT 待复核 · unknown≠pass", False)
        if "拒绝" in raw or "honeypot" in m:
            return ("RISK", "AUDIT 拒绝", False)
        if "book too thin" in m or "risk veto" in m:
            return ("RISK", "book too thin · risk veto", False)
        return ("RISK", _short_bubble(raw), False)

    # SCANNER
    if (
        m.startswith("scan ")
        or kind == "scan"
        or explicit == "SCANNER"
        or d.get("event") == "SCAN"
    ):
        if explicit == "EXIT":
            return ("EXIT", _short_bubble(raw), False)
        if m.startswith("scan "):
            return ("SCANNER", _short_bubble(raw.replace("SCAN ", "scan ")), False)
        return ("SCANNER", _short_bubble(raw), False)

    # TIMING — entry / buy / watch / liquidity go
    if (
        m.startswith("entry ")
        or m.startswith("buy ")
        or kind in ("buy", "watch")
        or explicit == "TIMING"
        or d.get("event") in ("ENTRY", "WATCH", "BUY")
    ):
        if m.startswith("entry "):
            return ("TIMING", _short_bubble(raw.replace("ENTRY ", "entry ")), False)
        if m.startswith("buy "):
            return ("TIMING", _short_bubble(raw.replace("BUY ", "buy ")), False)
        if m.startswith("watch ") or kind == "watch":
            return ("TIMING", _short_bubble(raw.replace("WATCH ", "watch ")), False)
        return ("TIMING", _short_bubble(raw), False)

    if explicit in agents:
        return (explicit, _short_bubble(raw), False)
    return None


def sync_agent_bubbles(ss, feed: list, tick: int, position, last_exit) -> dict:
    """Publish feed lines to per-agent bubble slots; ephemeral ~4s unless sticky."""
    slots = dict(ss.get("agent_bubbles") or {})
    seen = list(ss.get("bubble_seen_keys") or [])
    seen_set = set(seen)
    # ~4s at run_every=2; fade-out on expire tick
    ephemeral_ttl = 2

    for item in feed[-16:]:
        if len(item) >= 4:
            ts, kind, msg, agent_hint = item[0], item[1], item[2], item[3]
        else:
            ts, kind, msg = item[0], item[1], item[2]
            agent_hint = ""
        key = f"{ts}|{kind}|{msg}"
        if key in seen_set:
            continue
        seen.append(key)
        seen_set.add(key)
        detail = {"agent_type": agent_hint} if agent_hint else None
        routed = route_agent_bubble(msg, kind, detail)
        if not routed:
            continue
        agent, text, sticky = routed
        slots[agent] = {
            "text": text,
            "until": 10**9 if sticky else int(tick) + ephemeral_ttl,
            "sticky": bool(sticky),
            "key": key,
            "born": int(tick),
        }

    # Cap seen keys
    ss.bubble_seen_keys = seen[-80:]

    # Sticky EXIT overlays from live position / last exit (locked state)
    if last_exit and float(last_exit.get("mult") or 0) >= 2:
        mult = float(last_exit["mult"])
        sticky_txt = f"exit fired · {mult:.0f}x locked"
        cur = slots.get("EXIT")
        # Prefer fresh ephemeral EXIT pulse; re-assert sticky once it expires
        if not cur or (cur.get("sticky") and cur.get("text") != sticky_txt) or (
            not cur.get("sticky") and int(tick) >= int(cur.get("until", 0))
        ):
            if not cur or cur.get("sticky") or int(tick) > int(cur.get("until", 0)):
                slots["EXIT"] = {
                    "text": sticky_txt,
                    "until": 10**9,
                    "sticky": True,
                    "key": f"lock|{sticky_txt}",
                    "born": int(cur.get("born", tick)) if cur else int(tick),
                }
    elif position and float(position.get("mult") or 0) >= 5:
        sticky_txt = f"watching · {float(position['mult']):.0f}x open"
        cur = slots.get("EXIT")
        if not cur or cur.get("sticky") or int(tick) > int(cur.get("until", 0)):
            slots["EXIT"] = {
                "text": sticky_txt,
                "until": 10**9,
                "sticky": True,
                "key": f"watch|{sticky_txt}",
                "born": int(tick),
            }
    else:
        cur = slots.get("EXIT")
        if cur and cur.get("sticky") and str(cur.get("key", "")).startswith(("lock|", "watch|")):
            slots.pop("EXIT", None)

    # Expire ephemeral + mark fading (one tick of fade-out, then drop)
    live = {}
    for agent, slot in slots.items():
        until = int(slot.get("until", 0))
        sticky = bool(slot.get("sticky"))
        if sticky:
            live[agent] = {**slot, "fading": False}
        elif tick < until:
            live[agent] = {**slot, "fading": False}
        elif tick == until:
            live[agent] = {**slot, "fading": True}

    ss.agent_bubbles = live
    return live


def paper_tick(n: int) -> None:
    rng = random.Random(time.time_ns() ^ n)
    crypto = ["HUNTA", "HUNTB"]
    # Prefer locked hunt symbols from arc_hunt
    try:
        from desk_realtime.arc_hunt import hunt_pool

        crypto = [n for n, _ in hunt_pool()] or crypto
    except Exception:
        pass
    stocks = ["NVDA", "TSLA", "AMD", "PLTR", "COIN"]
    tok, stk = rng.choice(crypto), rng.choice(stocks)
    roll = rng.random()
    if roll < 0.18:
        write_log(
            type="action", market="crypto", symbol=tok, action="SCAN",
            reason="fresh launch spotted",
            detail={"bot": "scanner", "event": "SCAN", "agent_type": "SCANNER",
                    "note": f"SCAN fresh launch ${tok}"},
        )
    elif roll < 0.36:
        # Arc audit stub in paper mode (同步 desk 真引擎三档)
        try:
            from desk_realtime.arc_audit import audit_token_sync
            from desk_realtime.arc_hunt import hunt_pool as _hp

            fake_addr = next((a for n, a in _hp() if n == tok), _hp()[0][1])
            ar = audit_token_sync(tok, fake_addr, chain="arc")
            if ar.allows_buy:
                write_log(
                    type="action", market="crypto", symbol=tok, action="SCORE",
                    reason=ar.log_text,
                    detail={
                        "bot": "crypto_checker",
                        "event": "SCORE",
                        "agent_type": "RISK",
                        "panel": "VOTING",
                        "note": ar.log_text,
                        "audit_tier": ar.tier.value,
                    },
                )
            else:
                write_log(
                    type="skip",
                    market="crypto",
                    symbol=tok,
                    reason="audit_review" if ar.tier.value == "REVIEW" else "audit_reject",
                    detail={
                        "bot": "crypto_checker",
                        "event": "NOT BUY",
                        "agent_type": "RISK",
                        "panel": "STOPPED_OUT",
                        "note": ar.log_text,
                        "audit_tier": ar.tier.value,
                    },
                )
        except Exception:
            write_log(
                type="skip", market="crypto", symbol=tok, reason="thin_liquidity",
                detail={"bot": "crypto_checker", "event": "NOT BUY", "agent_type": "RISK",
                        "note": "book too thin (risk veto)"},
            )
    elif roll < 0.52:
        write_log(type="skip", market="crypto", symbol=tok, reason="off_narrative",
                  detail={"bot": "narrative", "event": "NOT BUY", "agent_type": "NARRATIVE",
                          "note": "off-narrative"})
    elif roll < 0.64:
        write_log(type="buy", market="crypto", symbol=tok, score=round(rng.uniform(0.7, 0.96), 2),
                  amount=ENTRY, tx_id="dry_run",
                  all_agent_scores={"narrative": {"virality": round(rng.uniform(0.7, 0.98), 2)},
                                    "auditor": {"organic_score": round(rng.uniform(0.55, 0.9), 2)},
                                    "crypto_pulse": {"go_signal": round(rng.uniform(0.45, 0.9), 2)}},
                  detail={"event": "ENTRY", "agent_type": "TIMING",
                          "note": "liquidity doubled, veto gone", "unit": QUOTE})
    elif roll < 0.72:
        write_log(type="action", symbol=tok, action="WATCH", reason="linked wallets queue sells",
                  market="crypto",
                  detail={"event": "WATCH", "bot": "auditor", "agent_type": "TIMING"})
    elif roll < 0.84:
        write_log(type="close", market="crypto", symbol=tok,
                  # pnl in quote units (OPEN POSITION)
                  pnl=round(rng.uniform(-ENTRY * 0.4, ENTRY * 4.0), 4),
                  hold_time=round(rng.uniform(0.3, 20), 2),
                  detail={"event": "EXIT", "agent_type": "EXIT",
                          "mult": round(rng.uniform(1.2, 31), 1), "unit": QUOTE})
    elif roll < 0.92:
        write_log(type="skip", market="stocks", symbol=stk, reason="low_score",
                  detail={"bot": "analyst", "event": "SCAN", "agent_type": "SCANNER"})
    else:
        write_log(type="action", symbol=rng.choice(crypto), action=rng.choice(["HOLD", "TIGHTEN", "TRIM"]),
                  reason="exit_manager", market="crypto",
                  detail={"event": "HOLD", "agent_type": "EXIT", "bot": "exit_manager"})


def mark_to_market(position: dict | None, tick: int) -> dict | None:
    """Entry (quote) stays fixed; mult/value mark-to-market each desk tick."""
    if not position:
        return None
    entry = float(position.get("entry") or ENTRY)
    base = float(position.get("base_mult") or position.get("mult") or 2.0)
    tok = str(position.get("token") or "")
    if not DESK_FF:
        out = dict(position)
        out["mult"] = round(base, 2)
        out["value"] = round(entry * base, 4)
        out["unit"] = QUOTE
        return out
    phase = tick * 0.41 + (sum(ord(c) for c in tok) % 97) * 0.07
    wave = 0.055 * math.sin(phase) + 0.028 * math.sin(phase * 2.3 + 0.8)
    micro = ((hash((tok, int(tick))) % 1000) / 1000.0 - 0.5) * 0.028
    drift = 0.018 * math.sin(tick * 0.07 + phase * 0.2)
    live = max(0.92, base * (1.0 + wave + micro + drift))
    out = dict(position)
    out["base_mult"] = round(base, 2)
    out["mult"] = round(live, 2)
    out["value"] = round(entry * live, 4)  # quote mark
    out["unit"] = QUOTE
    return out


def _logged_mark(row: dict) -> tuple[float | None, float | None, str]:
    """Mark the loop already wrote. Score is not a price."""
    d = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    note = str(d.get("note") or row.get("reason") or "")
    mult = d.get("mult")
    mark_usdc = d.get("mark_usdc")
    if mult is None:
        found = re.search(r"([0-9]+(?:\.[0-9]+)?)x mark", note)
        if found:
            mult = found.group(1)
    if not mark_usdc:
        found = re.search(r"· \$([0-9]+(?:\.[0-9]+)?)", note)
        if found:
            mark_usdc = found.group(1)
    try:
        mult_f = float(mult) if mult is not None else None
    except (TypeError, ValueError):
        mult_f = None
    try:
        value_f = float(mark_usdc) if mark_usdc else None
    except (TypeError, ValueError):
        value_f = None
    return mult_f, value_f, str(d.get("mark_src") or "")


def _failed_exit_row(row: dict) -> bool:
    detail = row.get("detail") if isinstance(row.get("detail"), dict) else {}
    note = str(detail.get("note") or "")
    return note.startswith("ERROR") or "exit rpc failed" in note or "sell failed" in note


def _rh_positions_from_engine() -> tuple[list[dict] | None, int]:
    """RH open book from rh_loop engine_state (source of truth).

    Returns (positions, max_slots). positions=None means engine unread → keep
    jsonl fallback. positions=[] means genuinely flat.
    """
    try:
        from desk_realtime.engine_state import read_engine_state

        eng = read_engine_state()
    except Exception:
        return None, 2
    if not isinstance(eng, dict) or not eng:
        return None, 2
    if "open_book" not in eng and "slots_open" not in eng:
        return None, 2
    try:
        max_slots = max(1, int(eng.get("max_slots") or os.environ.get("RH_MAX_SLOTS") or 2))
    except (TypeError, ValueError):
        max_slots = 2
    out: list[dict] = []
    for p in eng.get("open_book") or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").strip()
        if not sym:
            continue
        try:
            entry = float(p.get("entry_usdg") or ENTRY)
        except (TypeError, ValueError):
            entry = float(ENTRY)
        try:
            mult = float(p.get("live_mult") or 1.0)
        except (TypeError, ValueError):
            mult = 1.0
        if mult <= 0:
            mult = 1.0
        out.append({
            "token": f"${sym}",
            "market": "crypto",
            "entry": entry,
            "base_mult": round(mult, 4),
            "mult": round(mult, 3),
            "value": round(entry * mult, 4),
            "score": 0.0,
            "mark_src": "engine_book",
            "token_address": str(p.get("token_address") or ""),
            "agents": {},
            "unit": QUOTE,
            "opened_at": p.get("opened_at"),
        })
    out.sort(key=lambda x: float(x.get("mult") or 1.0))
    return out, max_slots


def desk_state(rows: list[dict], tick: int = 0) -> dict:
    buys = [r for r in rows if r.get("type") == "buy"]
    skips = [r for r in rows if r.get("type") == "skip"]
    closes = [r for r in rows if r.get("type") == "close"]
    actions = [r for r in rows if r.get("type") == "action"]

    # —— Unit contract: Arc native USDC (or SOL if DESK_CHAIN=solana) ——
    principal = float(STAKE)
    pnl = sum(float(r.get("pnl", 0) or 0) for r in closes)

    wins = sum(1 for r in closes if float(r.get("pnl", 0) or 0) > 0)
    losses = sum(1 for r in closes if float(r.get("pnl", 0) or 0) <= 0)
    outcomes = ["w" if float(r.get("pnl", 0) or 0) > 0 else "l" for r in closes[-48:]]
    vol = [max(4, min(18, abs(float(r.get("pnl", 0) or 0)) * 40)) for r in closes[-48:]]

    closed = set()
    for r in closes:
        if _failed_exit_row(r):
            continue
        closed.add((r.get("market"), r.get("symbol")))
    position = None
    last_exit = None
    for c in reversed(closes):
        d = c.get("detail") or {}
        if isinstance(d, dict) and d.get("event") == "EXIT" and not _failed_exit_row(c):
            last_exit = {"token": f"${c.get('symbol')}", "mult": float(d.get("mult", 1)),
                         "pnl": float(c.get("pnl", 0) or 0)}
            break
    open_rows: list[dict] = []
    seen_open: set[tuple] = set()
    for b in reversed(buys):
        key = (b.get("market"), b.get("symbol"))
        if key in closed or key in seen_open or not b.get("symbol"):
            continue
        seen_open.add(key)
        open_rows.append(b)
    slot_cap = 2
    try:
        if _IS_RH:
            slot_cap = max(1, int(os.environ.get("RH_MAX_SLOTS") or "2"))
        else:
            slot_cap = max(1, min(2, int(os.environ.get("ARC_MAX_SLOTS") or "2")))
    except ValueError:
        slot_cap = 2
    built: list[dict] = []
    for b in open_rows:
        amt = normalize_fill_amount(b.get("amount", ENTRY))
        score = float(b.get("score", 0.75) or 0.75)
        # Live book: the loop already emits the mark ("HOLD · 1.00x mark").
        # Do not invent score*10 + random — that is not a price, not leverage.
        logged_mult, logged_value, mark_src = _logged_mark(b)
        if logged_mult is not None and logged_mult > 0:
            base_mult = logged_mult
            value = logged_value if logged_value and logged_value > 0 else amt * logged_mult
        elif DESK_FF:
            base_mult = max(1.0, score * 10 + random.Random(str(b.get("ts"))).uniform(0, 18))
            value = amt * base_mult
        else:
            base_mult = 1.0
            value = amt
        built.append({
            "token": f"${b.get('symbol')}", "market": b.get("market"), "entry": amt,
            "base_mult": round(base_mult, 2),
            "mult": round(base_mult, 1), "value": round(value, 4), "score": score,
            "mark_src": mark_src,
            "agents": b.get("all_agent_scores") or {},
            "unit": QUOTE,
        })

    # RH: engine open_book wins over jsonl buy/close reconstruction.
    if _IS_RH and not DESK_FF:
        eng_built, eng_slots = _rh_positions_from_engine()
        if eng_built is not None:
            built = eng_built
            slot_cap = eng_slots

    if built and not DESK_FF:
        built.sort(key=lambda p: float(p.get("mult") or 1.0))
        marked: list[dict] = []
        for p in built:
            m = mark_to_market(p, tick)
            if m:
                marked.append(m)
        built = marked
        position = built[0] if built else None
        if position:
            others = [p["token"] for p in built[1:]]
            n = len(built)
            position["size_note"] = (
                f"{n} of {slot_cap} slots"
                + (f" · also {others[0]}" if others else "")
            )
    elif built:
        position = built[0]
        position = mark_to_market(position, tick)
    else:
        position = None

    unreal = 0.0
    if built and not DESK_FF:
        unreal = sum(float(p.get("value") or 0) - float(p.get("entry") or 0) for p in built)
    elif position:
        unreal = float(position["value"]) - float(position["entry"])
    balance = max(0.0, principal + pnl + unreal)
    multiple = (balance / principal) if principal else 1.0

    narr = liq = risk = 50
    if position and position["agents"]:
        narr = int(100 * float((position["agents"].get("narrative") or {}).get("virality", 0.7)))
        liq = int(100 * float((position["agents"].get("crypto_pulse") or {}).get("go_signal", 0.55)))
        risk = max(3, 100 - int(100 * float((position["agents"].get("auditor") or {}).get("organic_score", 0.6))))
    elif not DESK_FF:
        # No rehearsed 92 / 80 / 50 rings. Last feed note is the measurement.
        narr = liq = risk = 0
        for r in reversed(skips[-12:]):
            note = str((r.get("detail") or {}).get("note") or r.get("reason") or "")
            m = re.search(r"narrative\s+([0-9]+(?:\.[0-9]+)?)", note, re.I)
            if not m:
                continue
            val = float(m.group(1))
            narr = int(round(val * 100 if val <= 1.5 else val))
            break
    else:
        veto = sum(1 for r in skips[-25:] if "veto" in str(r.get("reason", "")) or "thin" in str(r.get("reason", "")))
        risk = min(90, 8 + veto * 8)
        narr = max(12, 92 - veto * 6)
        liq = max(20, 80 - len(skips[-12:]) * 2)

    # Single-pass event bus → feed + panel (zero lag between the two)
    feed, panel, _events = ingest_desk_bus(rows[-28:], position, last_exit)

    bot_hits = Counter()
    for r in rows[-200:]:
        d = r.get("detail") or {}
        if isinstance(d, dict) and d.get("bot"):
            bot_hits[str(d["bot"]).lower()] += 1
        for k in (r.get("all_agent_scores") or {}):
            bot_hits[str(k).lower()] += 1
        if r.get("type") == "action":
            bot_hits["exit_manager"] += 1

    total = wins + losses
    if not DESK_FF:
        win_pnls = [float(r.get("pnl") or 0) for r in closes if float(r.get("pnl") or 0) > 0]
        loss_pnls = [abs(float(r.get("pnl") or 0)) for r in closes if float(r.get("pnl") or 0) < 0]
        win_rate = (wins / total * 100) if total else 0.0
        avg_win = (sum(win_pnls) / len(win_pnls)) if win_pnls else 0.0
        avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0
        expectancy = (
            round((win_rate / 100) * avg_win - (1 - win_rate / 100) * avg_loss, 2)
            if total else 0.0
        )
        kelly = 0.0
        used = 0.0
        ruin = 0.0
    else:
        win_rate = (wins / total * 100) if total else 29.0
        avg_win = 8.9 if wins else 0.0
        avg_loss = 0.33
        expectancy = round((win_rate / 100) * avg_win - (1 - win_rate / 100) * avg_loss, 2) if total else 0.0
        kelly = round(min(38.0, max(28.0, 32.0 + expectancy * 1.8)), 1)
        used = round(kelly * 0.25, 1)
        ruin = round(max(4.5, min(12.0, 18.0 - win_rate * 0.12 - min(balance, 8) * 0.35)), 1)
    if not DESK_FF and not position and not closes:
        coh = 0.0
    else:
        coh = round(0.55 + narr / 300 + (1 - risk / 200), 2)

    return {
        "balance": balance, "multiple": multiple,
        "not_buy": len(skips) if not DESK_FF else max(len(skips), 1),
        "buys": len(buys), "closes": len(closes),
        "pnl": pnl, "unreal": unreal if position else 0.0,
        "outcomes": outcomes, "vol": vol if (vol or DESK_FF) else [],
        "position": position, "last_exit": last_exit,
        "narr": int(np.clip(narr, 0 if not DESK_FF else 5, 99)),
        "liq": int(np.clip(liq, 0 if not DESK_FF else 5, 99)),
        "risk": int(np.clip(risk, 0 if not DESK_FF else 1, 99)), "feed": feed, "panel": panel, "bot_hits": bot_hits,
        "launches": (
            sum(1 for r in rows if str(r.get("action") or "").upper() == "SCAN")
            if not DESK_FF
            else max(260, 40 + len(skips) + len(buys) * 4)
        ), "entered": len(buys),
        "win_rate": win_rate, "expectancy": expectancy, "avg_win": avg_win, "avg_loss": avg_loss,
        "kelly": kelly, "used": used, "ruin": ruin, "coh": min(0.99, coh), "n": len(rows),
        "actions": len(actions),
    }


def feed_ts(raw: str) -> str:
    """Desk feed stamp: local 24-hour clock, same zone as the footer."""
    text = str(raw or "").strip()
    if not text:
        return "--:--"
    if len(text) >= 19 and text[10] in "T ":
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone().strftime("%H:%M:%S")
        except ValueError:
            pass
    if len(text) >= 8 and text[2] == ":":
        return text[:8]
    return "--:--"


def fmt(sec: int) -> str:
    """Elapsed clock as HH:MM:SS. Used by the 8h demo tape, not live wall time."""
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def live_now() -> datetime:
    return datetime.now().astimezone()


def day_progress(now: datetime | None = None) -> float:
    """Position in the local calendar day, 0 at 00:00:00 and 1 at 24:00:00."""
    now = now or live_now()
    sec = now.hour * 3600 + now.minute * 60 + now.second
    return min(1.0, sec / 86400.0)


# ---------------------------------------------------------------------------
# Charts (inline SVG — fits CSS Grid cells, no Streamlit layout chrome)
# ---------------------------------------------------------------------------


def equity_path(
    rows: list[dict],
    live: float,
    n: int = 72,
    progress: float = 1.0,
) -> list[float]:
    """Wave equity from principal STAKE with drawdowns; ends at live (quote units)."""
    live = float(live)
    if not DESK_FF:
        # Live book is the wallet, not a rehearsed $500 wave.
        return [live, live]
    p = float(np.clip(progress, 0.02, 1.0))
    # Revealed samples for 0→progress (variable length; SVG maps across progress width)
    k = max(10, int(round(p * (n - 1))) + 1)
    t = np.linspace(0.0, 1.0, k)

    # Implied full-session finish from current mark
    final_est = STAKE + (live - STAKE) / p if p > 0.05 else max(live, STAKE)
    final_est = float(np.clip(final_est, STAKE * 0.85, STAKE * 48))

    # Steady climb + 跌幅波段 (several troughs / rebounds from $500)
    trend = STAKE + (final_est - STAKE) * np.power(t, 1.08)
    amp = max(abs(final_est - STAKE), STAKE) * 0.38
    waves = (
        -amp * 0.55 * np.sin(np.pi * t * 2.8) ** 2                 # early + mid drawdowns
        -amp * 0.28 * np.sin(np.pi * t * 4.6 + 0.7) ** 2 * t       # later dips
        +amp * 0.30 * np.sin(np.pi * t * 5.5) * (0.25 + 0.75 * t)  # rebound chop
        +amp * 0.10 * np.sin(np.pi * t * 1.6 + 0.3)
    )
    # Stable seed from trade activity so the wave doesn't jump every tick
    n_fills = sum(1 for r in rows if r.get("type") in ("buy", "close"))
    rng = np.random.default_rng(1700 + n_fills * 17)
    noise = rng.normal(0.0, amp * 0.03, size=k)

    path = trend + waves + noise
    path[0] = float(STAKE)
    # Floor ~52% of principal — visible dips off $500, never wipe to $0
    path = np.maximum(path, STAKE * 0.52)
    path[-1] = live
    if k >= 5:
        path[-2] = path[-2] * 0.35 + live * 0.65
        path[-3] = path[-3] * 0.55 + live * 0.45
        path[-4] = path[-4] * 0.75 + live * 0.25
    return [float(x) for x in path]


def trader_desk_svg(
    color: str,
    shape: str,
    screen: str,
    tag: str,
    phase: float = 0.0,
    busy: float = 0.55,
    data: dict | None = None,
) -> str:
    """Elevated front desk (ref style): perspective table + legs + live widescreen."""
    busy = float(np.clip(busy, 0.15, 1.0))
    data = data or {}
    seed = abs(int(phase * 17 + busy * 40)) % 997
    series = [
        0.28 + 0.62 * abs(math.sin(phase * 1.35 + i * 0.9 + seed * 0.01))
        for i in range(10)
    ]
    series = [float(np.clip(v * (0.5 + 0.75 * busy), 0.1, 1.0)) for v in series]

    # Monitor screen content (coords inside bezel: 24..68 x 10..30)
    sx0, sy0, sw, sh = 25.0, 11.0, 42.0, 18.0
    if screen == "bars":
        liq = float(data.get("liq", busy * 100)) / 100.0
        n = 8
        bw = sw / n * 0.62
        gap = sw / n
        parts = []
        for i in range(n):
            h = 3.5 + (sh - 5.0) * series[i] * (0.55 + 0.45 * liq)
            x = sx0 + i * gap + (gap - bw) * 0.5
            y = sy0 + sh - 1.2 - h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                f'rx="0.5" fill="{color}" opacity="{0.5 + 0.5 * series[i]:.2f}"/>'
            )
        screen_art = "".join(parts)
        hud_r = f"{int(float(data.get('liq', 0))):d}"
    elif screen == "fills":
        risk = float(data.get("risk", 40)) / 100.0
        rows = [
            (10 + 28 * series[0], "#ff3d6e"),
            (8 + 26 * (1.0 - risk) * series[1], color),
            (8 + 26 * risk * series[2], "#7CFF9A"),
        ]
        screen_art = "".join(
            f'<rect x="{sx0 + 1:.1f}" y="{sy0 + 2.2 + i * 5.2:.1f}" '
            f'width="{w:.1f}" height="3.2" rx="0.7" fill="{c}" opacity="0.92"/>'
            for i, (w, c) in enumerate(rows)
        )
        hud_r = f"{int(float(data.get('risk', 0))):d}"
    elif screen == "grid":
        entered = int(data.get("entered", 0))
        open_pos = 1 if data.get("open") else 0
        cols, rows = 6, 3
        cw, rh = sw / cols - 0.7, sh / rows - 0.8
        screen_art = "".join(
            f'<rect x="{sx0 + c * (cw + 0.7) + 0.4:.1f}" '
            f'y="{sy0 + r * (rh + 0.8) + 0.5:.1f}" '
            f'width="{cw:.1f}" height="{rh:.1f}" rx="0.45" fill="{color}" opacity="'
            f'{0.22 + 0.6 * series[(r * cols + c + entered + open_pos) % 10]:.2f}"/>'
            for r in range(rows) for c in range(cols)
        )
        hud_r = f"{entered}"
    elif screen == "flow":
        narr = float(data.get("narr", 50)) / 100.0
        pts_a, pts_b = [], []
        for i in range(8):
            x = sx0 + 1.5 + i * ((sw - 3) / 7)
            ya = sy0 + sh - 2 - (sh - 4) * series[i] * (0.45 + 0.55 * narr)
            yb = sy0 + sh - 2 - (sh - 5) * series[(i + 3) % 10] * 0.7
            pts_a.append(f"{x:.1f},{ya:.1f}")
            pts_b.append(f"{x:.1f},{yb:.1f}")
        screen_art = (
            f'<polyline points="{" ".join(pts_a)}" fill="none" stroke="{color}" '
            f'stroke-width="1.55" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<polyline points="{" ".join(pts_b)}" fill="none" stroke="#7CFF9A" '
            f'stroke-width="1.05" opacity="0.7" stroke-linejoin="round"/>'
        )
        hud_r = f"{int(float(data.get('narr', 0))):d}"
    else:
        pts = []
        for i in range(9):
            x = sx0 + 1.2 + i * ((sw - 2.4) / 8)
            y = sy0 + sh - 2.2 - (sh - 4.5) * series[i]
            pts.append(f"{x:.1f},{y:.1f}")
        tip = pts[-1].split(",")
        screen_art = (
            f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
            f'stroke-width="1.65" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{tip[0]}" cy="{tip[1]}" r="1.45" fill="{color}"/>'
        )
        hud_r = f"{int(data.get('launches', 0)) % 100:d}"

    # Corner HUD. Live desk shows the local 24h clock; the tape keeps a prop clock.
    if not DESK_FF:
        hud_l = datetime.now().astimezone().strftime("%H:%M")
    else:
        mm = int(abs(phase * 13 + seed) % 60)
        ss = int(abs(phase * 47 + busy * 20) % 60)
        hud_l = f"{mm:02d}:{ss:02d}"
    screen_art += (
        f'<text x="{sx0 + 1.6:.1f}" y="{sy0 + 3.6:.1f}" fill="#9aa3b2" '
        f'font-size="3.1" font-family="IBM Plex Mono,monospace">{hud_l}</text>'
        f'<text x="{sx0 + sw - 1.6:.1f}" y="{sy0 + 3.6:.1f}" fill="#9aa3b2" '
        f'font-size="3.1" text-anchor="end" '
        f'font-family="IBM Plex Mono,monospace">{hud_r}</text>'
    )

    # Geometric trader seated in chair (facing monitor / viewer) — oversized = cuter
    cx, cy = 46.0, 79.0
    eyes = (
        f'<circle cx="{cx - 4.0}" cy="{cy - 1.8}" r="1.7" fill="#111"/>'
        f'<circle cx="{cx + 4.0}" cy="{cy - 1.8}" r="1.7" fill="#111"/>'
        f'<circle cx="{cx - 3.45}" cy="{cy - 2.25}" r="0.55" fill="#fff" opacity="0.9"/>'
        f'<circle cx="{cx + 4.55}" cy="{cy - 2.25}" r="0.55" fill="#fff" opacity="0.9"/>'
    )
    if shape == "triangle":
        body = (
            f'<g class="trader-bob">'
            f'<polygon points="{cx},{cy - 14} {cx - 13.5},{cy + 12} {cx + 13.5},{cy + 12}" fill="{color}"/>'
            f'{eyes}</g>'
        )
    elif shape == "square":
        body = (
            f'<g class="trader-bob">'
            f'<rect x="{cx - 12}" y="{cy - 12}" width="24" height="24" rx="3.2" fill="{color}"/>'
            f'{eyes}</g>'
        )
    elif shape == "hex":
        body = (
            f'<g class="trader-bob">'
            f'<polygon points="{cx},{cy - 13.5} {cx + 11.7},{cy - 6.75} {cx + 11.7},{cy + 6.75} '
            f'{cx},{cy + 13.5} {cx - 11.7},{cy + 6.75} {cx - 11.7},{cy - 6.75}" fill="{color}"/>'
            f'{eyes}</g>'
        )
    else:
        body = (
            f'<g class="trader-bob">'
            f'<circle cx="{cx}" cy="{cy}" r="13.5" fill="{color}"/>'
            f'{eyes}</g>'
        )

    return (
        f'<svg viewBox="0 0 92 108" xmlns="http://www.w3.org/2000/svg">'
        # Layer 0 — shadows only (bottommost)
        f'<g class="desk-shadows">'
        f'<ellipse class="floor-shadow" cx="46" cy="101" rx="30" ry="4.8" fill="#000" opacity="0.22"/>'
        f'<ellipse class="chair-shadow" cx="46" cy="96.5" rx="11" ry="2.2" fill="#000" opacity="0.18"/>'
        f'<ellipse class="table-shadow" cx="46" cy="66" rx="34" ry="5" fill="#000" opacity="0.14"/>'
        f"</g>"
        # Layer 1 — chair (under desk + avatar)
        f'<g class="chair">'
        f'<line x1="46" y1="88" x2="46" y2="97" stroke="#1a2030" stroke-width="2.1"/>'
        f'<line x1="46" y1="93" x2="37" y2="98.5" stroke="#1a2030" stroke-width="1.7"/>'
        f'<line x1="46" y1="93" x2="55" y2="98.5" stroke="#1a2030" stroke-width="1.7"/>'
        f'<line x1="46" y1="93" x2="35.5" y2="92" stroke="#1a2030" stroke-width="1.5"/>'
        f'<line x1="46" y1="93" x2="56.5" y2="92" stroke="#1a2030" stroke-width="1.5"/>'
        f'<circle cx="46" cy="93" r="2" fill="#2a3344"/>'
        f'<ellipse cx="46" cy="86.5" rx="12" ry="5.8" fill="#141a24" stroke="#2a3344" stroke-width="1"/>'
        f'<rect x="34" y="70" width="24" height="15" rx="3" fill="#101620" stroke="#2a3344" stroke-width="1"/>'
        f'<rect x="31" y="78" width="4.5" height="9" rx="1.4" fill="#151c28"/>'
        f'<rect x="56.5" y="78" width="4.5" height="9" rx="1.4" fill="#151c28"/>'
        f"</g>"
        # Layer 2 — desk / monitor / hands
        f'<g class="desk">'
        f'<path d="M16,40 L20,40 L19.2,66 L15.2,66 Z" fill="#8b93a3"/>'
        f'<path d="M72,40 L76,40 L76.8,66 L72.8,66 Z" fill="#9aa3b4"/>'
        f'<rect x="15.2" y="66" width="4" height="1.6" rx="0.4" fill="#6e7686"/>'
        f'<rect x="72.8" y="66" width="4" height="1.6" rx="0.4" fill="#7a8292"/>'
        f'<path d="M20,38 L72,38 L78,54 L14,54 Z" fill="#1a2230"/>'
        f'<path d="M14,54 L78,54 L76.5,60 L15.5,60 Z" fill="#121820"/>'
        f'<path d="M15.5,60 L76.5,60 L75.8,62.5 L16.2,62.5 Z" fill="#0d1218"/>'
        f'<rect x="43.2" y="30" width="5.6" height="10" rx="0.8" fill="#2a3344"/>'
        f'<rect x="39" y="39" width="14" height="2.4" rx="0.7" fill="#343e50"/>'
        f'<rect x="22" y="8" width="48" height="24" rx="2.2" fill="#0a0e16" stroke="#2a3344" stroke-width="1.3"/>'
        f'<rect x="24" y="10" width="44" height="20" rx="1.2" fill="#05070c"/>'
        + screen_art
        + f'<rect x="30" y="48" width="26" height="6.5" rx="1.1" fill="#151c28" stroke="#2a3344" stroke-width="0.8"/>'
        f'<rect x="32" y="49.4" width="22" height="1.1" fill="#243044" opacity="0.85"/>'
        f'<rect x="32" y="51.2" width="22" height="1.1" fill="#243044" opacity="0.7"/>'
        f'<rect x="32" y="53" width="14" height="1.1" fill="#243044" opacity="0.55"/>'
        f'<ellipse cx="62.5" cy="51.5" rx="3.4" ry="2.5" fill="#d5dae3"/>'
        f'<ellipse cx="62.5" cy="50.7" rx="1.1" ry="0.7" fill="#9aa3b2" opacity="0.7"/>'
        + _typing_hands(color, busy, phase, seed)
        + f"</g>"
        # Layer 3 — avatar on topmost
        f'<g class="avatar-top">{body}</g>'
        f"</svg>"
    )


def _typing_hands(color: str, busy: float, phase: float, seed: int) -> str:
    """Two hands on the keyboard — different keys, irregular hit rhythm per agent."""
    # Per-agent + per-tick RNG so desks don't share the same finger park
    rng = random.Random((seed * 7919 + int(phase * 1000)) & 0xFFFFFFFF)
    rows_y = (49.95, 51.75, 53.55)
    # Left hand: WASD / home-left; right: JKLI / home-right — rarely same cell
    lx = 32.2 + rng.uniform(0.0, 9.5)
    ly = rows_y[rng.randrange(3)] + rng.uniform(-0.2, 0.25)
    rx = 41.0 + rng.uniform(0.0, 11.5)
    ry = rows_y[rng.randrange(3)] + rng.uniform(-0.2, 0.25)
    if lx > rx - 2.8:
        mid = (lx + rx) * 0.5
        lx, rx = mid - 2.2, mid + 2.2
    # Busy agents strike harder / idle ones rest dimmer & slower hop
    strike_l = 0.55 + 0.45 * abs(math.sin(phase * 2.15 + seed * 0.11))
    strike_r = 0.55 + 0.45 * abs(math.sin(phase * 2.73 + seed * 0.19 + 1.2))
    # Occasional "miss" beat — one hand pauses (opacity drop) while other hits
    if rng.random() > (0.35 + 0.55 * busy):
        if rng.random() < 0.5:
            strike_l *= 0.35
        else:
            strike_r *= 0.35
    lop = float(np.clip(0.22 + 0.72 * busy * strike_l, 0.15, 1.0))
    rop = float(np.clip(0.22 + 0.72 * busy * strike_r, 0.15, 1.0))
    # Slight size flicker on the active finger
    lr = 0.95 + 0.35 * strike_l * busy
    rr = 0.95 + 0.35 * strike_r * busy
    # Unique CSS timing vars baked into the hand group (overrides agent defaults)
    l_dur = 0.26 + (seed % 5) * 0.07 + (1.05 - busy) * 0.28 + rng.uniform(0, 0.08)
    r_dur = 0.31 + ((seed * 3) % 7) * 0.06 + (1.05 - busy) * 0.32 + rng.uniform(0, 0.1)
    l_del = (seed % 11) * 0.04 + rng.uniform(0, 0.12)
    r_del = ((seed * 5) % 13) * 0.05 + rng.uniform(0, 0.15)
    return (
        f'<g style="--type-l-dur:{l_dur:.2f}s;--type-r-dur:{r_dur:.2f}s;'
        f'--type-l-del:{l_del:.2f}s;--type-r-del:{r_del:.2f}s">'
        f'<circle class="trader-type lh" cx="{lx:.2f}" cy="{ly:.2f}" r="{lr:.2f}" '
        f'fill="{color}" opacity="{lop:.2f}"/>'
        f'<circle class="trader-type rh" cx="{rx:.2f}" cy="{ry:.2f}" r="{rr:.2f}" '
        f'fill="{color}" opacity="{rop:.2f}"/>'
        f"</g>"
    )


def _poly(xs: np.ndarray, ys: np.ndarray) -> str:
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys))


def svg_sparkline(
    vals: list[float],
    color: str = "#4da3ff",
    fill: bool = False,
    w: int = 120,
    h: int = 36,
) -> str:
    """Compact scoreboard sparkline (reference-style dynamic curve)."""
    if not vals:
        vals = [0.0, 0.0]
    arr = np.asarray(vals[-28:], dtype=float)
    if len(arr) < 2:
        arr = np.array([arr[0], arr[0]], dtype=float)
    lo, hi = float(arr.min()), float(arr.max())
    span = max(hi - lo, abs(hi) * 0.04, 1e-6)
    pad = 2.0
    xs = pad + (np.arange(len(arr)) / max(len(arr) - 1, 1)) * (w - 2 * pad)
    ys = pad + (1.0 - (arr - lo) / span) * (h - 2 * pad)
    line = _poly(xs, ys)
    gid = f"spk{abs(hash((color, fill, round(float(arr[-1]), 4))) % 10_000_000)}"
    parts = [
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]
    if fill:
        area = f"{xs[0]:.1f},{h - pad:.1f} " + line + f" {xs[-1]:.1f},{h - pad:.1f}"
        parts.append(
            f"<defs><linearGradient id='{gid}' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0%' stop-color='{color}' stop-opacity='0.35'/>"
            f"<stop offset='100%' stop-color='{color}' stop-opacity='0'/>"
            f"</linearGradient></defs>"
            f"<polygon points='{area}' fill='url(#{gid})'/>"
        )
    parts.append(
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="1.6" '
        f'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="2.2" fill="{color}"/>'
        f"</svg>"
    )
    return "".join(parts)


def svg_area_line(
    vals: list[float],
    color: str = "#00ff66",
    w: int = 640,
    h: int = 220,
    label: str | None = None,
    x_labels: tuple[str, str, str] | None = None,
    progress: float = 1.0,
    theme: str = "night",
    money_axis: bool = False,
    bars_html: str | None = None,
) -> str:
    """Balance equity curve: starts at principal, green↑ / red↓ waves, endpoint = last (quote)."""
    if not vals:
        vals = [float(STAKE)]
    arr = np.asarray(vals, dtype=float)
    last = float(arr[-1])
    lo = float(arr.min())
    hi = float(arr.max())
    stake_u = float(STAKE)
    if money_axis:
        # Round USDC ticks that bracket the wallet. The live amount stays on the line.
        y_floor = 0.0 if lo <= 0.0001 else lo
        span = max(hi - y_floor, 0.01)
        raw = span / 3.0
        exp = math.floor(math.log10(raw))
        base = 10 ** exp
        frac = raw / base
        step = 10.0 * base
        for n in (1, 2, 2.5, 5, 10):
            if frac <= n:
                step = n * base
                break
        plot_min = math.floor(y_floor / step) * step
        plot_max = math.ceil(hi / step) * step
        if plot_max <= hi:
            plot_max += step
        if plot_max <= plot_min:
            plot_max = plot_min + step
        y_ticks = []
        t = plot_min
        while t <= plot_max + step * 0.01:
            y_ticks.append(t)
            t += step
        y_min, y_max = plot_min, plot_max
    else:
        y_min = max(0.0, min(lo, stake_u) * 0.88)
        y_max = max(hi * 1.08, last * 1.04, stake_u * 1.15, y_min + 0.05)
        y_ticks = [y_min, (y_min + y_max) * 0.5, y_max]
        plot_min, plot_max = y_min, y_max

    def _sol_tick(v: float) -> str:
        if money_axis:
            if abs(v - round(v)) < 1e-6:
                return f"{v:.0f}"
            return f"{v:.2f}"
        if v >= 100:
            return f"{v:,.0f}"
        if v >= 10:
            return f"{v:.1f}"
        return f"{v:.2f}"

    y_tick_lbl = [_sol_tick(t) for t in y_ticks]
    n = len(arr)
    pad_l, pad_r, pad_t, pad_b = 4, 16, 34, 4
    # Curve only occupies 0→progress of the 8h width (now sits at "current time")
    p = float(np.clip(progress, 0.04, 1.0))
    span = (w - pad_l - pad_r) * p
    xs = pad_l + (np.arange(n) / max(n - 1, 1)) * span
    ys = pad_t + (1 - (arr - plot_min) / (plot_max - plot_min)) * (h - pad_t - pad_b)
    line = _poly(xs, ys)
    base_y = h - pad_b
    area = f"{xs[0]:.1f},{base_y:.1f} " + line + f" {xs[-1]:.1f},{base_y:.1f}"
    ex, ey = float(xs[-1]), float(ys[-1])
    # Badge matches curve end (live)
    badge = label or f"{_sol_tick(last)} {QUOTE}"
    badge_html = badge.replace("$", "&#36;")
    pct_x = ex / w * 100.0
    pct_y = ey / h * 100.0
    gid = "balFill"
    day = theme == "day"
    up_c, dn_c = ("#137333", "#C5221F") if day else ("#00ff66", "#ff3d6e")
    end_up = n < 2 or arr[-1] >= arr[-2]
    end_c = up_c if end_up else dn_c
    badge_cls = "bal-badge" if end_up else "bal-badge dn"
    # Day: stronger mid-gray grid for WCAG readability on white
    grid_c = "rgba(95,99,104,0.38)" if day else "rgba(0,255,102,0.06)"
    hair = (
        ("rgba(19,115,51,0.45)" if end_up else "rgba(197,34,31,0.42)")
        if day
        else ("rgba(0,255,102,0.15)" if end_up else "rgba(255,61,110,0.18)")
    )

    # Segmented stroke: green rising / red falling
    seg_parts = []
    for i in range(n - 1):
        c = up_c if arr[i + 1] >= arr[i] else dn_c
        seg_parts.append(
            f'<polyline points="{xs[i]:.2f},{ys[i]:.2f} {xs[i+1]:.2f},{ys[i+1]:.2f}" '
            f'fill="none" stroke="{c}" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round" '
            f'vector-effect="non-scaling-stroke"/>'
        )

    grid_parts = []
    for tv in y_ticks:
        gy = pad_t + (1 - (tv - plot_min) / (plot_max - plot_min)) * (h - pad_t - pad_b)
        grid_parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{w - pad_r}" y2="{gy:.1f}" '
            f'stroke="{grid_c}" stroke-width="1" stroke-dasharray="4 5" '
            f'vector-effect="non-scaling-stroke"/>'
        )
    for i in (0.0, 0.5, 1.0):
        gx = pad_l + i * (w - pad_l - pad_r)
        grid_parts.append(
            f'<line x1="{gx:.1f}" y1="{pad_t}" x2="{gx:.1f}" y2="{base_y:.1f}" '
            f'stroke="{grid_c}" stroke-width="1" stroke-dasharray="4 5" '
            f'vector-effect="non-scaling-stroke"/>'
        )

    xl = x_labels or ("00:00", "04:00", "08:00")
    y_html = "".join(f'<span>{t}</span>' for t in reversed(y_tick_lbl))
    x_html = "".join(f'<span>{t}</span>' for t in xl)
    # Bars must share the curve's revealed width (RT: 00:00→NOW, not full 24:00).
    p_bar = float(np.clip(progress, 0.04, 1.0)) * 100.0
    bars_block = ""
    if bars_html:
        bars_block = (
            f'<div class="bal-bars">'
            f'<div class="bal-bars-fill" style="width:{p_bar:.2f}%">{bars_html}</div>'
            f"</div>"
        )

    return (
        f'<div class="bal-chart">'
        f'<div class="bal-y">{y_html}</div>'
        f'<div class="bal-plot">'
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
        f'data-ymin="{float(plot_min):.6f}" data-ymax="{float(plot_max):.6f}" '
        f'data-padt="{pad_t}" data-padb="{pad_b}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f"<defs>"
        f'<linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{end_c}" stop-opacity="0.16"/>'
        f'<stop offset="100%" stop-color="{end_c}" stop-opacity="0"/>'
        f"</linearGradient>"
        f"</defs>"
        + "".join(grid_parts)
        + f'<polygon points="{area}" fill="url(#{gid})"/>'
        + "".join(seg_parts)
        + f'<line x1="{ex:.1f}" y1="{ey:.1f}" x2="{ex:.1f}" y2="{base_y:.1f}" '
        f'stroke="{hair}" stroke-width="1.2" stroke-dasharray="3 4" '
        f'vector-effect="non-scaling-stroke"/>'
        f"</svg>"
        f'<div class="bal-dot-wrap" style="left:{pct_x:.2f}%;top:{pct_y:.2f}%;--dot:{end_c}">'
        f'<span class="bal-dot-halo"></span>'
        f'<span class="bal-dot-halo delay"></span>'
        f'<span class="bal-dot"></span>'
        f"</div>"
        f'<div class="bal-badge-wrap" style="left:{pct_x:.2f}%;top:{pct_y:.2f}%">'
        f'<div class="{badge_cls}">{badge_html}</div>'
        f"</div>"
        f"</div>"
        f'<div class="bal-x"><div class="bal-x-in">{x_html}</div></div>'
        + bars_block
        + f"</div>"
    )


def svg_manifold(phase: float, w: int = 420, h: int = 148) -> str:
    """Wireframe hypercube / nested cube projection."""
    # unit cube corners
    corners = [(a, b, c) for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)]
    # rotate
    ca, sa = math.cos(phase * 0.35), math.sin(phase * 0.35)
    cb, sb = math.cos(phase * 0.22 + 0.4), math.sin(phase * 0.22 + 0.4)
    scale_base = min(w, h) * 0.42

    def project(x, y, z, scale):
        # yaw then pitch
        x, z = x * ca - z * sa, x * sa + z * ca
        y, z = y * cb - z * sb, y * sb + z * cb
        px = w * 0.58 + x * scale * 0.95
        py = h * 0.54 - y * scale * 0.78 - z * scale * 0.2
        return px, py

    def cube_edges(scale, color, width=1.55):
        pts = [project(x * scale, y * scale, z * scale, scale_base) for x, y, z in corners]
        segs = []
        for i, (x1, y1, z1) in enumerate(corners):
            for j in range(i + 1, 8):
                x2, y2, z2 = corners[j]
                if abs(x1 - x2) + abs(y1 - y2) + abs(z1 - z2) == 2:
                    p1, p2 = pts[i], pts[j]
                    segs.append(
                        f'<line x1="{p1[0]:.1f}" y1="{p1[1]:.1f}" x2="{p2[0]:.1f}" y2="{p2[1]:.1f}" '
                        f'stroke="{color}" stroke-width="{width}" opacity="0.95"/>'
                    )
        dots = "".join(
            f'<circle cx="{p[0]:.1f}" cy="{p[1]:.1f}" r="2.4" fill="{color}"/>' for p in pts
        )
        return "".join(segs) + dots

    # outer + inner cube + connecting spokes (tesseract-ish)
    outer = cube_edges(1.0, "#7CFF9A", 1.65)
    inner = cube_edges(0.48, "#39ff14", 1.3)
    spokes = []
    for (x, y, z) in corners:
        p1 = project(x, y, z, scale_base)
        p2 = project(x * 0.48, y * 0.48, z * 0.48, scale_base)
        spokes.append(
            f'<line x1="{p1[0]:.1f}" y1="{p1[1]:.1f}" x2="{p2[0]:.1f}" y2="{p2[1]:.1f}" '
            f'stroke="#3a8a3a" stroke-width="1.15" opacity="0.75"/>'
        )
    # graphic only — titles live in HTML .ph (left-aligned)
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
        + "".join(spokes) + outer + inner
        + "</svg>"
    )


def svg_embed(
    seed: int,
    accepted: int,
    rejected: int,
    w: int = 420,
    h: int = 148,
    *,
    cluster_label: str = "ROBINHOOD CLUSTER",
) -> str:
    rng = np.random.default_rng(seed)
    n, m = max(40, min(100, rejected // 4 + 30)), max(10, min(42, accepted * 4 + 8))
    # rejected brown cluster (lower-left)
    rx = rng.normal(0.15, 0.42, n)
    ry = rng.normal(0.25, 0.38, n)
    # accepted green robinhood cluster (upper-right)
    ax = rng.normal(1.15, 0.16, m)
    ay = rng.normal(1.05, 0.14, m)
    # faint background noise
    nx, ny = rng.uniform(-0.4, 1.7, 25), rng.uniform(-0.2, 1.5, 25)

    def map_xy(x, y):
        sx = (x + 0.5) / 2.4 * (w - 20) + 8
        sy = (1.55 - y) / 2.1 * (h - 20) + 10
        return sx, sy

    def dots(xs, ys, color, r, op=0.85):
        out = []
        for x, y in zip(xs, ys):
            px, py = map_xy(x, y)
            out.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{r}" fill="{color}" opacity="{op}"/>')
        return "".join(out)

    # cluster circle around accepted mean
    acx, acy = map_xy(1.15, 1.05)
    cr = min(w, h) * 0.2
    label_x, label_y = acx - cr * 0.35, max(16, acy - cr - 12)
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
        + dots(nx, ny, "#4a2020", 2.0, 0.35)
        + dots(rx, ry, "#8a6a3a", 2.8, 0.8)
        + dots(ax, ay, "#00ff66", 3.2, 0.95)
        + f'<circle cx="{acx:.1f}" cy="{acy:.1f}" r="{cr:.1f}" fill="none" stroke="#e8ffe8" '
        f'stroke-width="1.25" opacity="0.7"/>'
        f'<text x="{label_x:.1f}" y="{label_y:.1f}" fill="#7CFF9A" font-size="12" '
        f'font-family="IBM Plex Mono,monospace" letter-spacing="1.2">{cluster_label}</text>'
        + "</svg>"
    )


def svg_wallet(seed: int, w: int = 420, h: int = 120) -> str:
    rng = np.random.default_rng(seed)
    cx, cy = w * 0.38, h * 0.48
    nodes = [(cx, cy, 9, "#c8ff33", 1.0)]
    for i in range(12):
        ang = (i / 12) * 2 * math.pi + seed * 0.04
        r = 28 + (i % 4) * 10 + float(rng.uniform(-4, 6))
        nodes.append((
            cx + r * math.cos(ang) * 1.15,
            cy + r * math.sin(ang) * 0.85,
            3.2 + (i % 3),
            "#7cff9a" if i % 3 else "#b8e050",
            0.75 + (i % 4) * 0.05,
        ))
    edges = []
    for i in range(1, len(nodes)):
        x, y, *_ = nodes[i]
        edges.append(
            f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{x:.1f}" y2="{y:.1f}" '
            f'stroke="#1a4a1a" stroke-width="1"/>'
        )
    dots = []
    for x, y, rad, color, op in nodes:
        glow = (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad * 2.2:.1f}" fill="{color}" opacity="0.12"/>'
            if rad > 6 else ""
        )
        dots.append(
            f'{glow}<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.1f}" fill="{color}" opacity="{op:.2f}"/>'
        )
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">'
        + "".join(edges)
        + "".join(dots)
        + "</svg>"
    )


def svg_survival(ruin: float, kelly: float, w: int = 420, h: int = 110) -> str:
    """Rising survival curve (kelly vs survival) — green fill, plateau right."""
    x = np.linspace(0, 1, 72)
    # soft logistic rise → plateau (matches ref shape)
    k = 4.2 + kelly / 40.0
    y = 1.0 / (1.0 + np.exp(-k * (x - 0.32)))
    y = 0.08 + y * (0.78 - ruin / 400.0)
    y = np.clip(y, 0.06, 0.92)
    pad_x, pad_t, pad_b = 8, 8, 6
    sx = pad_x + x * (w - pad_x * 2)
    sy = pad_t + (1 - y) * (h - pad_t - pad_b)
    line = _poly(sx, sy)
    area = f"{pad_x:.1f},{h - pad_b:.1f} " + line + f" {w - pad_x:.1f},{h - pad_b:.1f}"
    grids = "".join(
        f'<line x1="{pad_x}" y1="{yy:.1f}" x2="{w - pad_x}" y2="{yy:.1f}" '
        f'stroke="#1a2a1a" stroke-width="1"/>'
        for yy in (h * 0.25, h * 0.48, h * 0.72)
    )
    gid = "szFill"
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f"<defs><linearGradient id=\"{gid}\" x1=\"0\" y1=\"0\" x2=\"0\" y2=\"1\">"
        f'<stop offset="0%" stop-color="#7CFF9A" stop-opacity="0.28"/>'
        f'<stop offset="100%" stop-color="#7CFF9A" stop-opacity="0.02"/>'
        f"</linearGradient></defs>"
        f"{grids}"
        f'<polygon points="{area}" fill="url(#{gid})"/>'
        f'<polyline points="{line}" fill="none" stroke="#7CFF9A" stroke-width="1.8" '
        f'stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>'
        f"</svg>"
    )


def volume_bars(hist: list[float], n: int = 56, *, match_curve: bool = False) -> str:
    """Volatility ticks under THE BALANCE — green=up, red=down.

    match_curve=True: one bar per curve sample (日线 sync), full-width track.
    Default: pad to n for the dense paper/demo strip.
    """
    arr = list(hist) if hist else [float(STAKE)]
    if len(arr) < 2:
        arr = [float(arr[0] if arr else STAKE), float(arr[-1] if arr else STAKE)]
    if match_curve:
        # Keep curve length — do not invent 56 padded ticks.
        arr = [float(v) for v in arr]
    else:
        while len(arr) < n:
            arr = [arr[0]] + arr
        arr = arr[-n:]
    parts = []
    for i, v in enumerate(arr):
        prev = arr[i - 1] if i else v
        delta = float(v) - float(prev)
        mag = abs(delta)
        if match_curve:
            # Day-scale: small PnL still readable; flat day stays a hairline.
            hh = max(6, min(18, int(6 + mag * 40))) if mag > 1e-9 else 5
        else:
            hh = max(4, min(18, int(4 + mag * 14)))
        cls = "up" if delta >= 0 else "dn"
        parts.append(f'<i class="ob {cls}" style="height:{hh}px"></i>')
    track_cls = "ob-track sync" if match_curve else "ob-track"
    return f'<div class="{track_cls}">{"".join(parts)}</div>'


# ---------------------------------------------------------------------------
# Live UI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Desk event bus — one ingest pass → feed lines + OPEN POSITION panel
# Live path: WS client → DeskBus.drain() → write_log → ingest_desk_bus (same tick)
# ---------------------------------------------------------------------------

PANEL_THEMES = {
    "HOLD_OFF": {
        "panel": "pos-panel theme-hold",
        "btn": "pos-btn",
        "btn_label": "HOLD OFF",
        "bar": "pos-bar",
        "bar_label": "FLAT · REBUILDING RULES",
        "show_sig": False,
    },
    "VOTING": {
        "panel": "pos-panel theme-voting",
        "btn": "pos-btn",
        "btn_label": "NOT BUY",
        "bar": "pos-bar",
        "bar_label": "VOTING",
        "show_sig": False,
    },
    "BUY": {
        "panel": "pos-panel theme-buy",
        "btn": "pos-btn press",
        "btn_label": "BUY",
        "bar": "pos-bar",
        "bar_label": "OPEN",
        "show_sig": True,
    },
    "ARMING": {
        "panel": "pos-panel theme-arming",
        "btn": "pos-btn press",
        "btn_label": "BUY",
        "bar": "pos-bar",
        "bar_label": "OPEN TERMINAL",
        "show_sig": True,
    },
    "STOPPED_OUT": {
        "panel": "pos-panel theme-stopped",
        "btn": "pos-btn",
        "btn_label": "NOT BUY",
        "bar": "pos-bar",
        "bar_label": "STOPPED OUT",
        "show_sig": True,
    },
}


def _panel_hold() -> dict:
    return {
        "state": "HOLD_OFF",
        "token": "—",
        "sig": "",
        "entry": "—",
        "value": "—",
        "value_cls": "dim",
    }


def update_panel_state(ev: dict, position: dict | None = None) -> dict:
    """Pure panel reducer — called in the same ingest tick as feed append."""
    state = ev.get("panel")
    tok = f"${ev['symbol']}" if ev.get("symbol") else "—"
    amt = normalize_fill_amount(ev.get("amount") or ENTRY)

    if state == "STOPPED_OUT":
        # A veto is not a fill. Do not invent a −45% mark from 0.55 × size.
        return {
            "state": "STOPPED_OUT",
            "token": tok,
            "sig": "NOT BUY",
            "entry": "—",
            "value": "—",
            "value_cls": "dim",
        }
    if state == "VOTING":
        return {
            "state": "VOTING",
            "token": tok if tok != "—" else "$HOODAI",
            "sig": "",
            "entry": "queued",
            "value": "—",
            "value_cls": "dim",
        }
    if state == "BUY":
        mult = float((position or {}).get("mult") or ev.get("mult") or 1.0)
        entry = float((position or {}).get("entry") or amt)
        value = float((position or {}).get("value") or (entry * mult))
        return {
            "state": "BUY",
            "token": (position or {}).get("token") or tok,
            "sig": f"{mult:.1f}x",
            "entry": fmt_quote_html(entry, 2),
            "value": fmt_quote_html(value, 2),
            "value_cls": "",
            "size_note": (position or {}).get("size_note") or "",
        }
    if state == "ARMING":
        entry = float((position or {}).get("entry") or amt)
        # Prefill live quote while arming so the book never looks frozen
        if position and position.get("value") is not None:
            mult = float(position.get("mult") or 1.0)
            value = float(position["value"])
            return {
                "state": "ARMING",
                "token": position.get("token") or tok,
                "sig": f"{mult:.1f}x",
                "entry": fmt_quote_html(entry, 2),
                "value": fmt_quote_html(value, 2),
                "value_cls": "",
            }
        return {
            "state": "ARMING",
            "token": (position or {}).get("token") or tok,
            "sig": "● SIGNAL",
            "entry": fmt_quote_html(entry, 2),
            "value": '<span class="arming">arming<span class="cursor">_</span></span>',
            "value_cls": "",
        }
    if state == "HOLD_OFF":
        return _panel_hold()
    return _panel_hold()


def expand_log_record(r: dict) -> list[dict]:
    """Turn one jsonl row into structured desk events (feed + panel hints)."""
    ts = feed_ts(r.get("ts", ""))
    d = r.get("detail") if isinstance(r.get("detail"), dict) else {}
    typ = r.get("type")
    sym = str(r.get("symbol") or "")
    agent = str(d.get("agent_type") or "").upper()
    events: list[dict] = []

    def push(kind: str, msg: str, agent_type: str, panel: str | None = None, **extra):
        events.append({
            "ts": ts, "kind": kind, "msg": msg, "agent": agent_type,
            "panel": panel, "symbol": sym, "amount": extra.get("amount"),
            "mult": extra.get("mult"), "raw": r,
        })

    if typ == "buy":
        score = float(r.get("score", 0) or 0)
        amt = normalize_fill_amount(r.get("amount", ENTRY))
        prefer = str(d.get("panel") or "").upper()
        # WS short-circuit: single-frame BUY/OPEN without replaying the whole arming chain
        if prefer == "BUY" and d.get("source") == "ws":
            push("buy", d.get("note") or f"BUY ${sym} — live fill", agent or "TIMING",
                 panel="BUY", amount=amt)
            return events
        push("scan", f"SCAN fresh launch ${sym}", "SCANNER")
        push("score", f"SCORE narrative {score:.2f} theme match", "NARRATIVE", panel="VOTING")
        push("buy", f"BUY ${sym} — liquidity doubled, veto gone", "TIMING", panel="BUY", amount=amt)
        push("buy", f"ENTRY ${sym} opened {amt:.2f} {QUOTE}", "TIMING", panel="ARMING", amount=amt)
        if prefer == "BUY":
            push("buy", d.get("note") or f"BUY ${sym} open", agent or "TIMING",
                 panel="BUY", amount=amt)
    elif typ == "skip":
        note = d.get("note") or r.get("reason")
        note_l = str(note).lower()
        skip_agent = agent or (
            "NARRATIVE" if "narrative" in note_l
            else "RISK" if ("thin" in note_l or "veto" in note_l) else "RISK"
        )
        panel_force = str(d.get("panel") or "").upper() or None
        push("stop", f"NOT BUY ${sym} — {note}", skip_agent,
             panel=panel_force if panel_force == "STOPPED_OUT" else None)
        if panel_force == "STOPPED_OUT" or "veto" in note_l or "thin" in note_l or "stopped" in note_l:
            push("stop", f"STOPPED OUT ${sym} — risk veto", "RISK",
                 panel="STOPPED_OUT", amount=normalize_fill_amount(r.get("amount") or ENTRY))
        elif "narrative" in note_l:
            push("stop", f"NOT BUY ${sym} — {note}", "NARRATIVE", panel="VOTING")
    elif typ == "close":
        mult = (d or {}).get("mult", "")
        note = d.get("note")
        msg = note if note else (
            f"EXIT ${sym} closed +{mult}x" if mult else f"EXIT ${sym} pnl ${r.get('pnl')}"
        )
        push(
            "buy",
            msg,
            agent or "EXIT",
            panel=str(d.get("panel") or "HOLD_OFF"),
            mult=mult,
        )
    elif typ == "action":
        act = r.get("action", "HOLD")
        panel_force = str(d.get("panel") or "").upper() or None
        if act == "WATCH":
            push("watch", f"WATCH {r.get('reason')}", agent or "TIMING")
        elif act == "SCORE" or d.get("event") == "SCORE" or panel_force == "VOTING":
            note = d.get("note") or r.get("reason") or f"SCORE ${sym}"
            push("score", str(note), agent or "NARRATIVE", panel=panel_force or "VOTING")
        elif act == "SCAN" or d.get("event") == "SCAN":
            note = d.get("note") or r.get("reason") or ""
            if note:
                push("scan", f"SCAN ${sym} · {note}", agent or "SCANNER")
            else:
                push("scan", f"SCAN ${sym}", agent or "SCANNER")
        elif act == "ARM" or d.get("event") == "BOOT":
            push("scan", f"ARM ${sym} · {r.get('reason')}", agent or "EXIT",
                 panel=panel_force or "HOLD_OFF")
        else:
            push("scan", f"{act} ${sym} · {r.get('reason')}", agent or "EXIT",
                 panel=panel_force)
    return events


def ingest_desk_bus(
    rows: list[dict],
    position: dict | None,
    last_exit: dict | None,
) -> tuple[list[tuple], dict, list[dict]]:
    """
    Single synchronous ingest: append feed lines and reduce panel state
    from the same DeskEvent stream (zero visual lag between feed ↔ panel).
    """
    feed: list[tuple] = []
    panel = _panel_hold()
    all_events: list[dict] = []

    for r in rows[-28:]:
        batch = expand_log_record(r)
        for ev in batch:
            all_events.append(ev)
            feed.append((ev["ts"], ev["kind"], ev["msg"], ev["agent"]))
            if ev.get("panel"):
                # Live position context for BUY/ARMING amounts
                pos_ctx = position
                if ev["panel"] == "STOPPED_OUT":
                    pos_ctx = None
                panel = update_panel_state(ev, pos_ctx)

    # Open book still live → push fresh mark into panel every ingest
    if position and panel["state"] in ("BUY", "ARMING", "HOLD_OFF", "VOTING"):
        newest_panel = None
        for ev in reversed(all_events):
            if ev.get("panel"):
                newest_panel = ev["panel"]
                break
        live_pos = dict(position)
        # Always re-reduce from current mark so value/x tick with the market
        if newest_panel == "ARMING":
            panel = update_panel_state(
                {"panel": "ARMING", "symbol": str(position["token"]).lstrip("$"),
                 "amount": position.get("entry"), "mult": position.get("mult")},
                live_pos,
            )
        elif newest_panel != "STOPPED_OUT":
            panel = update_panel_state(
                {"panel": "BUY", "symbol": str(position["token"]).lstrip("$"),
                 "amount": position.get("entry"), "mult": position.get("mult")},
                live_pos,
            )

    if last_exit and panel["state"] == "HOLD_OFF" and not position and not all_events:
        pass

    return feed, panel, all_events


def render_pos_panel(payload: dict, link_status: str = "connected") -> str:
    theme = PANEL_THEMES[payload["state"]]
    sig = (payload.get("sig") or "").strip()
    # Image-2 hero: token left + pct/signal right (same visual weight)
    if theme["show_sig"] and sig:
        pct_html = f'<span class="pct">{sig}</span>'
    else:
        pct_html = '<span class="pct hidden">—</span>'
    v_cls = f'v {payload.get("value_cls") or ""}'.strip()
    entry = payload.get("entry") or "—"
    value = payload.get("value") or "—"
    bar_cls = theme["bar"]
    bar_label = theme["bar_label"]
    # Link resilience: overlay footer when WS is down (panel state kept)
    if link_status in ("disconnected", "connecting", "boot"):
        bar_cls = f'{theme["bar"]} bar-dc'
        bar_label = "DISCONNECTED · RETRYING..."
    has_book = bool(payload.get("token") and payload.get("token") not in ("—", "", "HOLD OFF", "$—"))
    # Enable panic when there is a live mark / buy / arming book
    panic_on = payload.get("state") in ("BUY", "ARMING") or (
        has_book and payload.get("state") not in ("HOLD_OFF", "STOPPED_OUT")
        and entry not in ("—", "queued", "")
    )
    if _tx_locked("panic") or _tx_locked("sell") or _tx_locked("buy"):
        panic_on = False
    panic_cls = "pos-panic" if panic_on else "pos-panic is-disabled"
    panic_href = "?panic=1" if panic_on else "#"
    # Keep feed filter across panic navigation when present
    try:
        _ff = str(st.query_params.get("ff", "") or "")
        if panic_on and _ff in ("all", "entries", "skipped", "errors"):
            panic_href = f"?panic=1&ff={_ff}"
    except Exception:
        pass
    return (
        f'<div class="{theme["panel"]}">'
        f'<div class="pos-label">OPEN POSITION</div>'
        f'<div class="pos-hero">'
        f'<span class="tok">{payload["token"]}</span>'
        f'{pct_html}'
        f'</div>'
        f'<div class="pos-actions">'
        f'<div class="{theme["btn"]}">{theme["btn_label"]}</div>'
        f'<a class="{panic_cls}" href="{panic_href}" title="Market-flatten + halt auto-buy">'
        f'PANIC SELL / EXIT</a>'
        f'</div>'
        f'<div class="pos-rows">'
        f'<div class="pos-row"><span class="k">entry</span>'
        f'<span class="v entry">{entry}</span></div>'
        f'<div class="pos-row"><span class="k">value</span>'
        f'<span class="{v_cls}">{value}</span></div>'
        f'<div class="pos-row"><span class="k">size</span>'
        f'<span class="v dim">{payload.get("size_note") or "one position at a time"}</span></div>'
        f'</div>'
        f'<div class="pos-foot"><div class="{bar_cls}">{bar_label}</div></div>'
        f'</div>'
    )


def drain_live_bus_to_log() -> int:
    """Pull WS/mock packets into jsonl — same tick as feed+panel ingest."""
    rows = DeskBus.drain()
    for row in rows:
        # Strip bus metadata before persist
        clean = {k: v for k, v in row.items() if not str(k).startswith("_")}
        write_log(**clean)
    return len(rows)


def _init() -> None:
    ss = st.session_state
    if DESK_FF:
        # Recording mode: never open a live WS — tape only
        DeskBus.set_status("connected", "fastforward tape")
        DeskBus.set_metrics({"desk_mode": "FF×100", "valve_gate": "OPEN"})
    else:
        ensure_ws_client(DESK_WS_URL)
    if ss.get("run_key") == RUN_KEY:
        return
    # Fresh 8h mission: principal STAKE in quote units, clock 00:00 → 08:00
    ss.run_key = RUN_KEY
    ss.boot = time.time()
    ss.elapsed = 0
    ss.hist = [STAKE]
    ss.tick = 0
    ss.grid = ["off"] * 80
    ss.agent_bubbles = {}
    ss.bubble_seen_keys = []
    ss.ws_flash_risk_until = 0
    ss.mock_stage_i = 0
    ss.ff_done = False
    ss.ff_i = 0
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("", encoding="utf-8")
    if DESK_FF:
        ensure_tape()
        player = TapePlayer()
        ss.ff_total = player.total
        ss.ff_events = player.events
        write_log(
            type="action",
            symbol="DESK",
            action="ARM",
            reason=f"FASTFORWARD · {STAKE:.2f} {QUOTE} · 8h→~5min · {player.total} events",
            market="crypto",
            detail={"event": "BOOT", "stake": STAKE, "unit": QUOTE, "session_h": 8,
                    "panel": "HOLD_OFF", "ff": True},
        )
    else:
        ss.ff_events = []
        ss.ff_total = 0
        write_log(
            type="action",
            symbol="DESK",
            action="ARM",
            reason=f"session open · live book · 24h clock · {QUOTE}",
            market="crypto",
            detail={"event": "BOOT", "stake": STAKE, "unit": QUOTE, "session_h": 24, "panel": "HOLD_OFF"},
        )


def _fastforward_tick(ss) -> int:
    """Inject next tape event(s); advance mission clock by sim_elapsed. Returns count."""
    events = ss.get("ff_events") or []
    i = int(ss.get("ff_i") or 0)
    if i >= len(events):
        ss.ff_done = True
        ss.elapsed = SESSION
        return 0
    # One primary feed line per UI frame (smooth 100× look)
    # If tape stored paired SCAN+BUY at same beat, flush the pair in one frame
    batch = [events[i]]
    i += 1
    if i < len(events):
        a = batch[0]
        b = events[i]
        if (
            a.get("type") == "action"
            and str((a.get("detail") or {}).get("event") or "").upper() == "SCAN"
            and b.get("type") == "buy"
            and a.get("symbol") == b.get("symbol")
        ):
            batch.append(b)
            i += 1
    # Extra pace in FF: pull more lines per frame so ~5 min still finishes at slower refresh
    while i < len(events) and len(batch) < 4:
        nxt = events[i]
        if nxt.get("symbol") == "ZZZ" and len(batch) >= 1:
            break
        batch.append(nxt)
        i += 1
    for ev in batch:
        payload = {k: v for k, v in ev.items() if k != "sim_elapsed"}
        write_log(**payload)
        ss.elapsed = min(SESSION, float(ev.get("sim_elapsed") or ss.elapsed))
    ss.ff_i = i
    if i >= len(events):
        ss.ff_done = True
        ss.elapsed = SESSION
    return len(batch)


@st.fragment(**_FRAGMENT_KW)
def trencher() -> None:
    _init()
    ss = st.session_state
    # Panic exit via query link from OPEN POSITION panel
    try:
        _panic = str(st.query_params.get("panic", "") or "")
    except Exception:
        _panic = ""
    if _panic == "1":
        if _tx_locked("panic"):
            try:
                del st.query_params["panic"]
            except Exception:
                pass
        else:
            _lock_tx("panic")
            _lock_tx("sell")
            rows_pre = read_log()
            st_pre = desk_state(rows_pre, tick=int(ss.get("tick") or 0))
            execute_panic_exit(st_pre.get("position"))
            try:
                del st.query_params["panic"]
            except Exception:
                st.query_params.clear()
            st.rerun()

    theme = resolve_desk_theme(ss)
    theme_name = theme.upper()
    inject_theme_vars(theme)
    ss.tick += 1

    live_n = 0
    if DESK_FF:
        # Tape drive — no WS / paper noise / RPC
        live_n = _fastforward_tick(ss)
        link = "connected"
        DeskBus.set_status("connected", "fastforward")
        dc_on = False
        dc_banner = ""
        if not ss.get("ff_done"):
            # Mission clock already set from sim_elapsed inside _fastforward_tick
            pass
        else:
            ss.elapsed = SESSION
    else:
        ss.elapsed = min(SESSION, ss.elapsed + 1)
        # Live WS/mock → jsonl BEFORE desk_state so feed+panel+bubbles share one tick
        live_n = drain_live_bus_to_log()
        link = DeskBus.status()
        dc_on = link in ("disconnected", "connecting", "boot")
        dc_banner = (
            '<div class="dc-crit">CRITICAL: BOT DISCONNECTED — RETRYING...</div>'
        )

    ss.ws_link = link
    desk_dc_cls = "desk dc-on" if dc_on else "desk"
    if DESK_FF:
        desk_dc_cls += " ff-mode"
    pos_glass_cls = "glass pos-dc" if dc_on else "glass"
    try:
        feed_ff = str(st.query_params.get("ff", "all") or "all").lower()
    except Exception:
        feed_ff = "all"
    if feed_ff not in ("all", "entries", "skipped", "errors"):
        feed_ff = "all"
    feed_ff_cls = f"feed-card ff-{feed_ff}"
    # THE BALANCE: 1D=日线 from funding · RT=实时 denser tape (Dexscreener-like).
    try:
        bal_mode = str(st.query_params.get("bal", "") or "").lower()
    except Exception:
        bal_mode = ""
    # Compat with older TIME/TICK links.
    if bal_mode in ("time", "1d", "d", "day", "daily"):
        bal_mode = "1d"
    elif bal_mode in ("tick", "rt", "live", "realtime", "real"):
        bal_mode = "rt"
    else:
        bal_mode = str(ss.get("bal_mode") or "1d").lower()
        if bal_mode in ("time",):
            bal_mode = "1d"
        elif bal_mode in ("tick",):
            bal_mode = "rt"
        if bal_mode not in ("1d", "rt"):
            bal_mode = "1d"
    ss["bal_mode"] = bal_mode

    # Paper noise is demo-only. Live Arc/RH never invents fills when the socket blips.
    use_paper = (
        (not DESK_FF)
        and DESK_CHAIN not in ("arc", "robinhood", "rh", "rhchain")
        and (
            DESK_PAPER == "1"
            or (DESK_PAPER == "auto" and link != "connected" and live_n == 0)
        )
    )
    if use_paper:
        paper_tick(ss.tick)

    rows = read_log()
    state = desk_state(rows, tick=int(ss.tick))

    target = float(state["balance"])
    if DESK_FF:
        now_local = None
        progress = min(1.0, float(ss.elapsed) / float(SESSION)) if SESSION else 1.0
        clock_face = fmt(int(ss.elapsed))
        clock_sub = "08 HOUR TAPE"
        span_tag = "8H"
        clock_axis = ("0h", "4h", "8h")
        footer_time = f"{clock_face} / {fmt(SESSION)}"
    else:
        now_local = live_now()
        progress = day_progress(now_local)
        clock_face = now_local.strftime("%H:%M:%S")
        clock_sub = now_local.strftime("%Y-%m-%d") + " · LOCAL"
        span_tag = "24H"
        clock_axis = ("00:00", "12:00", "24:00")
        footer_time = ""
    # RH THE BALANCE: day-count from funding epoch (not calendar 00:00→24:00).
    if _IS_RH and not DESK_FF and now_local is not None:
        try:
            from desk_realtime.rh_net import funding_mark as _rh_fund_axis

            _fm = _rh_fund_axis(float(ss.get("rh_usdg") or 0.0) or None)
            _fa = float(_fm.get("funded_at") or 0.0)
            if _fa > 0:
                _fund_dt = datetime.fromtimestamp(_fa).astimezone()
                # Calendar day index from funding date (D1 = fund day).
                _dn = 1 + max(0, (now_local.date() - _fund_dt.date()).days)
                _bal = str(ss.get("bal_mode") or "1d")
                if _bal == "rt":
                    # RT = dense equity from funding → NOW (full session, not calendar today).
                    span_tag = "RT"
                    _d0 = _fund_dt.strftime("%m-%d")
                    _d1 = now_local.strftime("%m-%d")
                    if _dn <= 1:
                        clock_axis = (
                            _fund_dt.strftime("%H:%M"),
                            "·",
                            "NOW",
                        )
                    elif _dn == 2:
                        clock_axis = (_d0, _d1, "NOW")
                    else:
                        _mid = _fund_dt.date().toordinal() + (_dn - 1) // 2
                        from datetime import date as _date_cls

                        _dm = _date_cls.fromordinal(_mid).strftime("%m-%d")
                        clock_axis = (_d0, _dm, "NOW")
                    progress = 1.0
                    clock_sub = (
                        _fund_dt.strftime("%m-%d %H:%M")
                        + f" FUND → NOW · {_dn}D · RT"
                    )
                else:
                    # 1D = one point per calendar day since funding.
                    span_tag = "1D"
                    _d0 = _fund_dt.strftime("%m-%d")
                    _d1 = now_local.strftime("%m-%d")
                    if _dn <= 1:
                        clock_axis = (_d0, "·", "NOW")
                    elif _dn == 2:
                        clock_axis = (_d0, _d1, "NOW")
                    else:
                        _mid = _fund_dt.date().toordinal() + (_dn - 1) // 2
                        from datetime import date as _date_cls

                        _dm = _date_cls.fromordinal(_mid).strftime("%m-%d")
                        clock_axis = (_d0, _dm, "NOW")
                    progress = 1.0
                    clock_sub = (
                        _fund_dt.strftime("%m-%d %H:%M")
                        + f" FUND · {_dn}D · 1D"
                    )
        except Exception:
            pass
    # Rebuild path from trades — X maps 0→1 across the 8h window
    ss.hist = equity_path(rows, target, n=72, progress=progress)

    rng = random.Random(ss.tick)
    grid = list(ss.grid)
    if len(grid) != 80:
        grid = (grid + ["off"] * 80)[:80]
    for _ in range(4):
        grid[rng.randrange(80)] = rng.choice(["on", "mid", "hot", "bad", "off", "off"])
    # denser active band — 10×8 fits the mid-row height
    for i in rng.sample(range(80), min(28, 12 + state["entered"] * 2)):
        if grid[i] == "off":
            grid[i] = "mid"
    ss.grid = grid

    up = state["multiple"] >= 1
    if _IS_RH and not DESK_FF:
        if state["position"]:
            phase, blurb = f"05 {state['position']['token']}", "hold · stop · take · max-hold"
        else:
            phase, blurb = "01 SCAN", "RH allowlist · uni-v3"
    elif state["position"]:
        phase, blurb = f"05 {state['position']['token']}", "vote · veto · entry · exit"
    elif state["last_exit"] and state["last_exit"]["mult"] >= 5:
        phase, blurb = "06 ROTATION", "profit into related launches"
    elif state["not_buy"] > 30:
        phase, blurb = "04 NEW FILTER", "rebuild entry gates"
    else:
        phase, blurb = "01 SCANNING", "every launch in the trench"

    bal_cls = "g" if up else "r"
    bal_tone = "up" if up else "dn"
    chart_color = ("#137333" if theme == "day" else "#00ff66") if up else (
        "#C5221F" if theme == "day" else "#ff3d6e"
    )

    # DESK FEED → robot head bubbles (ephemeral unless EXIT lock sticky)
    bubbles = sync_agent_bubbles(
        ss, state["feed"], int(ss.tick), state.get("position"), state.get("last_exit"),
    )
    feed_kinds = [k for _ts, k, _m, *_rest in state["feed"][-16:]]
    scan_hit = sum(1 for k in feed_kinds if k in ("scan", "score"))
    buy_hit = sum(1 for k in feed_kinds if k == "buy")
    stop_hit = sum(1 for k in feed_kinds if k == "stop")
    watch_hit = sum(1 for k in feed_kinds if k == "watch")
    tick_pulse = 0.35 + 0.25 * math.sin(ss.tick / 3.0)
    busy_map = {
        "SCANNER": float(np.clip(0.4 + scan_hit * 0.12 + tick_pulse * 0.35, 0.2, 1.0)),
        "NARRATIVE": float(np.clip(0.35 + state["narr"] / 160 + tick_pulse * 0.25, 0.2, 1.0)),
        "RISK": float(np.clip(0.35 + state["risk"] / 140 + stop_hit * 0.1, 0.2, 1.0)),
        "TIMING": float(np.clip(0.35 + state["liq"] / 150 + watch_hit * 0.12, 0.2, 1.0)),
        "EXIT": float(np.clip(0.4 + buy_hit * 0.18 + (0.25 if bubbles.get("EXIT") else 0), 0.2, 1.0)),
    }
    for _name, _slot in bubbles.items():
        if _name in busy_map and not _slot.get("fading"):
            busy_map[_name] = float(np.clip(busy_map[_name] + 0.22, 0.2, 1.0))

    def _bubble_html(name: str) -> str:
        slot = bubbles.get(name)
        if not slot:
            return ""
        txt = (
            str(slot.get("text") or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        cls = "bubble"
        if slot.get("sticky"):
            cls += " sticky"
        if slot.get("fading"):
            cls += " out"
        return f'<div class="{cls}">{txt}</div>'

    goplus_foot = "goplus pending"
    if _IS_RH and not DESK_FF:
        goplus_foot = "intel-only"
        try:
            from desk_realtime.rhc_intel import read_intel as _rhc_read

            _rhc = _rhc_read()
            hot_n = len(_rhc.get("hot") or [])
            if _rhc.get("ok") and hot_n:
                goplus_foot = f"rhc hot {hot_n}"
            elif _rhc.get("reason"):
                goplus_foot = "rhc " + str(_rhc.get("reason") or "")[:28]
        except Exception:
            goplus_foot = "rhc unread"
    else:
        try:
            from desk_realtime.arc_goplus import read_scan

            _gp = read_scan()
            if _gp.get("honeypot"):
                goplus_foot = "honeypot 100%"
                state["risk"] = 100
            elif _gp.get("line"):
                src = "open" if str(_gp.get("is_open_source")) == "1" else "closed" if str(_gp.get("is_open_source")) == "0" else "n/a"
                holders = _gp.get("holder_count") or "—"
                goplus_foot = f"src {src} · holders {holders}"
            else:
                goplus_foot = "goplus unread"
        except Exception:
            goplus_foot = "goplus unread"
    scan_seen = int(state["launches"])
    try:
        from desk_realtime.engine_state import read_engine_state

        _eng_early = read_engine_state()
        if not DESK_FF and _eng_early.get("scouted_count") is not None:
            scan_seen = int(_eng_early["scouted_count"])
    except Exception:
        _eng_early = {}
    specs = [
        ("SCANNER", "#1967D2" if theme == "day" else "#7aaaff", "circle", "line", "SCAN", f"seen {scan_seen}"),
        ("NARRATIVE", "#B06000" if theme == "day" else "#ffcc33", "triangle", "flow", "NRTVE", f"match {state['narr']}%"),
        ("RISK", "#C5221F" if theme == "day" else "#ff3d6e", "square", "fills", "RISK", f"veto {state['risk']}%"),
        ("TIMING", "#7B1FA2" if theme == "day" else "#c77dff", "circle", "bars", "FLOW", f"liq {state['liq']}%"),
        ("EXIT", "#137333" if theme == "day" else "#00ff66", "hex", "grid", "EXIT", "exit lane"),
    ]
    if _IS_RH and not DESK_FF:
        _rh_hot = len(_eng_early.get("rhc_hot") or []) if _eng_early else 0
        _rh_skips = int(_eng_early.get("not_buy") or _eng_early.get("net_out") or state.get("not_buy") or 0)
        _rh_slots = int(_eng_early.get("slots_open") or 0)
        _rh_max = int(_eng_early.get("max_slots") or 1)
        specs = [
            ("SCANNER", "#1967D2" if theme == "day" else "#7aaaff", "circle", "line", "SCAN", f"board {scan_seen}"),
            ("NARRATIVE", "#B06000" if theme == "day" else "#ffcc33", "triangle", "flow", "INTEL", f"rhc { _rh_hot}"),
            ("RISK", "#C5221F" if theme == "day" else "#ff3d6e", "square", "fills", "VETO", f"skip {_rh_skips}"),
            ("TIMING", "#7B1FA2" if theme == "day" else "#c77dff", "circle", "bars", "M5M15", "gate"),
            ("EXIT", "#137333" if theme == "day" else "#00ff66", "hex", "grid", "BOOK", f"{_rh_slots}/{_rh_max}"),
        ]
    agents_parts = []
    screen_data = {
        "narr": state["narr"],
        "risk": state["risk"],
        "liq": state["liq"],
        "launches": scan_seen,
        "entered": state["entered"],
        "open": bool(state.get("position")),
    }
    for i, (name, color, shape, screen, tag, ds) in enumerate(specs):
        busy = busy_map[name]
        lvl = "busy-hi" if busy >= 0.72 else ("busy-lo" if busy < 0.4 else "busy-mid")
        flash = ""
        if name == "RISK":
            pstate = (state.get("panel") or {}).get("state")
            if pstate == "STOPPED_OUT":
                ss.ws_flash_risk_until = int(ss.tick) + 4
            if int(ss.tick) <= int(ss.get("ws_flash_risk_until") or 0):
                flash = " flash-risk"
        agents_parts.append(
            f'<div class="agent {lvl}{flash}" style="--c:{color};--busy:{busy:.2f}">'
            f'{_bubble_html(name)}'
            f'<div class="station">'
            f'{trader_desk_svg(color, shape, screen, tag, ss.tick / 5 + i, busy, screen_data)}'
            f'</div>'
            f'<div class="nm">{name}</div><div class="ds">{ds}</div></div>'
        )
    agents_html = "".join(agents_parts)

    pos = state["position"]
    # Panel already reduced in desk_state via ingest_desk_bus (same tick as feed)
    panel_payload = state.get("panel") or _panel_hold()
    buy_hits = [
        msg for _ts, kind, msg, *_rest in state["feed"][-12:]
        if kind == "buy" and (msg.startswith("BUY ") or msg.startswith("ENTRY "))
    ]
    if buy_hits:
        sig_key = buy_hits[-1]
        if ss.get("last_buy_sig") != sig_key:
            ss.last_buy_sig = sig_key
            ss.buy_flash_until = int(ss.tick) + 10
    pos_body = render_pos_panel(panel_payload, link_status=link)

    _c_narr = "#137333" if theme == "day" else "#00ff66"
    _c_liq = "#7B1FA2" if theme == "day" else "#c77dff"
    _c_risk = "#C5221F" if theme == "day" else "#ff3d6e"
    consensus_html = (
        f'<div class="ac-list">'
        f'<div class="ac-row">'
        f'<div class="ring" style="--p:{state["narr"]};--c:{_c_narr}">{state["narr"]}%</div>'
        f'<div class="ac-txt"><div class="ac-title">NARRATIVE MATCH</div>'
        f'<div class="ac-sub">theme match</div></div></div>'
        f'<div class="ac-row">'
        f'<div class="ring" style="--p:{state["liq"]};--c:{_c_liq}">{state["liq"]}%</div>'
        f'<div class="ac-txt"><div class="ac-title">LIQUIDITY DEPTH</div>'
        f'<div class="ac-sub">book absorbs size</div></div></div>'
        f'<div class="ac-row">'
        f'<div class="ring" style="--p:{state["risk"]};--c:{_c_risk}">{state["risk"]}%</div>'
        f'<div class="ac-txt"><div class="ac-title">RISK VETO</div>'
        f'<div class="ac-sub">blocks the entry</div></div></div>'
        f'</div>'
    )

    def _feed_tag(kind: str, msg: str) -> tuple[str, str]:
        m = (msg or "").upper()
        k = (kind or "").lower()
        if (
            "ERROR" in m
            or "ALERT" in m
            or "PANIC" in m
            or "CLUSTER WARN" in m
            or "RED ·" in m
            or "老鼠仓" in (msg or "")
            or "LIQ VETO" in m
        ):
            return "ERROR", "t-err"
        if m.startswith("ENTRY"):
            return "ENTRY", "t-entry"
        if m.startswith("EXIT") or " EXIT" in f" {m}":
            return "EXIT", "t-exit"
        if m.startswith("BUY") or k == "buy":
            return "BUY", "t-buy"
        if "HONEYPOT" in m or "GOPLUS DETECTED" in m:
            return "STOP", "t-stop"
        if "NOT BUY" in m or "VETO" in m or "REJECT" in m or k == "risk":
            return "RISK", "t-risk"
        if k == "score" or m.startswith("SCORE"):
            return "SCORE", "t-score"
        if k == "watch" or m.startswith("WATCH"):
            return "WATCH", "t-watch"
        if k == "scan" or m.startswith("SCAN"):
            return "SCAN", "t-scan"
        return (k.upper()[:5] or "EVT"), "t-dim"

    def _feed_cat(kind: str, msg: str, tag: str) -> str:
        m = (msg or "").upper()
        if tag in ("BUY", "ENTRY", "EXIT") or kind == "buy":
            return "entry"
        if (
            tag in ("STOP", "ERROR")
            or "ERROR" in m
            or "ALERT" in m
            or "PANIC" in m
            or "CLUSTER WARN" in m
            or "RED ·" in m
            or "老鼠仓" in (msg or "")
        ):
            return "err"
        if tag in ("RISK", "WATCH", "SCAN", "SCORE") or "NOT BUY" in m or "REJECT" in m:
            return "skip"
        return "skip"

    def _esc(s: str) -> str:
        return (
            str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    feed_rows = list(state["feed"][-64:])
    feed_event_n = max(len(feed_rows), int(state.get("n") or 0), int(state.get("actions") or 0))
    # Pin: prefer latest BUY/ENTRY, else newest event
    pin = None
    for ts, kind, msg, *_rest in reversed(feed_rows):
        if kind == "buy" or str(msg).upper().startswith(("BUY ", "ENTRY ")):
            pin = (ts, kind, msg)
            break
    if pin is None and feed_rows:
        ts, kind, msg, *_rest = feed_rows[-1]
        pin = (ts, kind, msg)

    feed_lines = []
    # Newest first in DOM (pairs with CSS column-reverse → latest at bottom)
    for ts, kind, msg, *_rest in reversed(feed_rows):
        tag, tag_cls = _feed_tag(kind, msg)
        cat = _feed_cat(kind, msg, tag)
        hit = kind == "buy" and str(msg).upper().startswith(("BUY ", "ENTRY "))
        line_cls = f"line cat-{cat}"
        if hit or (cat == "entry" and tag in ("BUY", "ENTRY")):
            line_cls += " buy-hit"
        star = "*" if (hit or (cat == "entry" and tag in ("BUY", "ENTRY"))) else "·"
        feed_lines.append(
            f"<div class='{line_cls}' data-cat='{cat}'>"
            f"<span class='ts'>{_esc(ts)}</span>"
            f"<span class='tag {tag_cls}'>{tag}</span>"
            f"<span class='msg'>{_esc(msg)}</span>"
            f"<span class='star'>{star}</span>"
            f"</div>"
        )
    if pin:
        p_ts, p_kind, p_msg = pin
        p_tag, p_cls = _feed_tag(p_kind, p_msg)
        p_cat = _feed_cat(p_kind, p_msg, p_tag)
        feed_pin_html = (
            f"<div class='feed-pin' data-cat='{p_cat}'>"
            f"<span class='ts'>{_esc(p_ts)}</span>"
            f"<span class='tag {p_cls}'>{p_tag}</span>"
            f"<span class='msg'>{_esc(p_msg)}</span>"
            f"<span class='star'>*</span>"
            f"</div>"
        )
    else:
        feed_pin_html = (
            "<div class='feed-pin'>"
            "<span class='ts'>--:--</span>"
            "<span class='tag t-dim'>IDLE</span>"
            "<span class='msg'>waiting for desk events</span>"
            "<span class='star'>·</span>"
            "</div>"
        )
    intel_line = desk_intel_line() if _IS_RH and not DESK_FF else ""
    if intel_line and not pin:
        feed_pin_html = (
            f"<div class='feed-pin' data-cat='scan'>"
            f"<span class='ts'>intel</span>"
            f"<span class='tag t-dim'>RHC</span>"
            f"<span class='msg'>{_esc(intel_line)}</span>"
            f"<span class='star'>·</span>"
            f"</div>"
        )
    agents_ph = (
        "SCANNER · NARRATIVE · RISK · TIMING · EXIT"
        if not _IS_RH
        else "BOARD · PUMP INTEL · RHC INTEL · TIMING · EXIT"
    )
    scan_sub = (
        f"{scan_seen} launches seen · {goplus_foot}"
        if not _IS_RH
        else f"RH allowlist · pump+rhc read-only · {goplus_foot or 'intel'}"
    )
    narr_sub = (
        "only one cluster survives"
        if not _IS_RH
        else "pump themes · RH KOL hot · not buy triggers"
    )
    manifold_sub = (
        "4D · theme × liquidity × timing × risk"
        if not _IS_RH
        else "4D · m5 × m15 × liq × risk"
    )
    emb_title = "NARRATIVE EMBEDDING" if not _IS_RH else "INTEL SURFACE"
    emb_accepted_lbl = "accepted" if not _IS_RH else "entered"
    emb_rejected_lbl = "rejected" if not _IS_RH else "skipped"
    cluster_lbl = "ROBINHOOD CLUSTER" if not _IS_RH else "ALLOWLIST PASS"
    edge_sub = (
        "expectancy per trade · rolling 40"
        if not _IS_RH
        else "RH closes · win/loss from engine"
    )
    sig_tag = "DARKPOOL · LIVE FEED" if not _IS_RH else "RH UNI-V3 · LIVE FEED"
    brand_sub = "grok trencher" if not _IS_RH else "rh short desk"
    grid_cells = "".join(f'<div class="sg {c}"></div>' for c in ss.grid)
    if DESK_FF:
        flagged = max(0, 3 - state["risk"] // 30)
        exit_pressure = int(np.clip(18 + state["risk"] * 0.55, 8, 92))
    else:
        flagged = 0
        exit_pressure = 0
    full_kelly = float(state["kelly"])
    used_kelly = float(state["used"])
    pct = progress * 100.0

    bal_x = clock_axis
    bal_hist = list(ss.hist or [STAKE])
    if bal_hist:
        bal_hist[0] = float(STAKE)
    bal_svg = svg_area_line(
        bal_hist,
        chart_color,
        label=f"{fmt_quote(state['balance'], 2)} {QUOTE}",
        x_labels=bal_x,
        progress=progress,
        theme=theme,
    )
    man_svg = svg_manifold(time.time() / 3.5)
    emb_svg = svg_embed(
        ss.tick // 2,
        state["entered"],
        max(0, scan_seen - state["entered"]),
        cluster_label=cluster_lbl,
    )
    wal_svg = svg_wallet(ss.tick % 50)
    ruin_svg = svg_survival(state["ruin"], full_kelly)
    bars = volume_bars(ss.hist)
    # SIGNAL INTERCEPT rhythmic bars — seeded so streamlit re-renders stay coherent
    _sig_rng = random.Random(ss.tick // 2)
    _sig_wave = [28, 55, 72, 48, 90, 62, 40, 78, 95, 58, 35, 68, 82, 50, 30]
    sig_parts = []
    for i in range(160):
        base = _sig_wave[i % len(_sig_wave)]
        h = int(np.clip(base + _sig_rng.randint(-18, 22) + (state["risk"] % 17), 18, 100))
        dur = 0.85 + (i % 7) * 0.11
        delay = (i % 11) * -0.09
        sig_parts.append(
            f'<i style="--h:{h};--dur:{dur:.2f}s;--d:{delay:.2f}s"></i>'
        )
    sig_bars = "".join(sig_parts)
    feed_html = "".join(feed_lines) or (
        "<div class='line'>"
        "<span class='ts'>--:--</span>"
        "<span class='tag t-dim'>WAIT</span>"
        "<span class='msg'>waiting…</span>"
        "<span class='star'>·</span>"
        "</div>"
    )

    edge_n = max(0, int(state["closes"]))
    if not DESK_FF and edge_n == 0:
        edge_tag = "no live closes"
    else:
        edge_tag = "edge accepted" if state["expectancy"] > 0 else "edge rejected"
    exp_cls = "pos" if state["expectancy"] >= 0 else "neg"
    wr_cls = "pos" if state["win_rate"] > 0 else "neg"
    wr_bar = max(0.0, min(100.0, float(state["win_rate"])))
    wr_bar_cls = "g" if state["win_rate"] > 0 else "r"
    aw_bar = 0.0 if float(state["avg_win"]) <= 0 else max(4.0, min(100.0, float(state["avg_win"]) / 10.0 * 100.0))
    al_bar = 0.0 if float(state["avg_loss"]) <= 0 else max(4.0, min(100.0, float(state["avg_loss"]) / 1.0 * 100.0))

    # Scoreboard metrics (quote-native) — live wallet when available
    bal_sol = float(state["balance"])
    stake_sol = float(STAKE) if STAKE > 0 else float(state.get("balance") or 0)
    wallet_note = ""
    if DESK_CHAIN == "arc" and not DESK_FF:
        try:
            from desk_realtime.arc_hunt import capped_entry_usdc
            from desk_realtime.arc_net import fetch_wallet_usdc

            wbal = fetch_wallet_usdc()
            raw = float(wbal.get("raw_usdc") or 0.0)
            if not wbal.get("faked"):
                # Real on-chain USDC. Curve is today's historical balances, not a flat copy.
                stake_sol = raw
                bal_sol = raw
                from desk_realtime.arc_net import wallet_day_curve

                curve = wallet_day_curve()
                pts = list(curve.get("points") or [])
                series = [raw, raw]
                if now_local is not None and pts:
                    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
                    t0 = start.timestamp()
                    t1 = now_local.timestamp()
                    span = max(1.0, t1 - t0)
                    n = 64

                    def _at(ts: float) -> float:
                        val = float(pts[0].get("usdc") or 0.0)
                        for p in pts:
                            if float(p.get("ts") or 0) <= ts + 0.5:
                                val = float(p.get("usdc") or 0.0)
                        return val

                    series = [_at(t0 + span * i / (n - 1)) for i in range(n)]
                    series[-1] = raw
                    bal_svg = svg_area_line(
                        series,
                        chart_color,
                        label=f"{fmt_quote(raw, 2)} {QUOTE}",
                        x_labels=bal_x,
                        progress=progress,
                        theme=theme,
                        money_axis=True,
                    )
                wallet_note = ""
            else:
                stake_sol = float(wbal.get("fake_floor") or STAKE or 0)
                bal_sol = max(float(state["balance"]), float(wbal["display_usdc"]))
                wallet_note = " · FAKE FLOOR"
            ss["arc_entry_cap"] = capped_entry_usdc(raw if raw > 0 else float(wbal.get("display_usdc") or 0))
        except Exception:
            pass
    elif _IS_RH and not DESK_FF:
        # Prefer rh_loop → engine_state wallet cache (no UI→RPC). Fallback RPC rare.
        try:
            from desk_realtime.rh_uniswap import hard_cap

            _eng_w = _eng_early or {}
            _w_ts = float(_eng_w.get("wallet_ts") or 0.0)
            _w_age = time.time() - _w_ts if _w_ts > 0 else 1e9
            _cache_ttl = float(os.environ.get("RH_UI_ENGINE_TTL", "90") or 90)
            if _w_ts > 0 and _w_age <= _cache_ttl and (
                float(_eng_w.get("wallet_usdg") or 0) > 0
                or float(_eng_w.get("wallet_eth") or 0) > 0
            ):
                ss["rh_usdg"] = float(_eng_w.get("wallet_usdg") or 0.0)
                ss["rh_eth"] = float(_eng_w.get("wallet_eth") or 0.0)
                ss["rh_wallet_ts"] = _w_ts
                if int(_eng_w.get("block") or 0) > 0:
                    ss["rh_block"] = int(_eng_w["block"])
                    ss["rh_block_ts"] = _w_ts
                ss["rh_wallet_bootstrapped"] = True
            else:
                _rh_ttl = float(os.environ.get("RH_UI_RPC_TTL", "30") or 30)
                _rh_age = time.time() - float(ss.get("rh_wallet_ts") or 0)
                _have = bool(ss.get("rh_wallet_ts"))
                # First frame: skip RPC so the desk chrome paints; next tick fills wallet.
                if _have and _rh_age <= _rh_ttl:
                    pass
                elif _have or ss.get("rh_wallet_bootstrapped"):
                    from desk_realtime.rh_net import USDG, fetch_eth_balance, wallet as rh_wallet
                    from desk_realtime.rh_uniswap import erc20_balance

                    who = rh_wallet()
                    # Short UI timeout — never block the desk on a dead RPC hop.
                    raw_usdg = (
                        erc20_balance(USDG, who, timeout=1.2) / 1_000_000 if who else 0.0
                    )
                    eth = (
                        float(fetch_eth_balance(who, timeout=1.0).get("eth") or 0.0)
                        if who
                        else 0.0
                    )
                    ss["rh_usdg"] = raw_usdg
                    ss["rh_eth"] = eth
                    ss["rh_wallet_ts"] = time.time()
                else:
                    ss["rh_wallet_bootstrapped"] = True
            raw_usdg = float(ss.get("rh_usdg") or 0.0)
            eth = float(ss.get("rh_eth") or 0.0)
            _day_net = 0.0
            try:
                from desk_realtime.rh_strategy import realized_snapshot

                _day_net = float(realized_snapshot().get("day_net_usdg") or 0.0)
            except Exception:
                _day_net = float((_eng_early or {}).get("day_net_usdg") or 0.0)
            # Hard-cap stays in CAP KPI; THE BALANCE is NAV (cash + open marks).
            _cap_only = float(hard_cap())
            # Prefer live wallet / engine cache. Never use funded_usdg as the live tip —
            # that re-anchors to the first-day mark and sawtooths the chart (12.58↔12.48).
            cash_u = raw_usdg if raw_usdg > 0 else float(ss.get("rh_usdg") or 0.0)
            if cash_u <= 0:
                cash_u = float((_eng_early or {}).get("wallet_usdg") or 0.0)
            from desk_realtime.rh_net import funding_mark as _rh_fm_bal, wallet_day_curve

            _fm_bal = _rh_fm_bal(cash_u or None)
            funded_u_only = float(_fm_bal.get("funded_usdg") or 0.0)
            # Cold start only: no wallet sample yet.
            if cash_u <= 0 and not ss.get("rh_wallet_bootstrapped"):
                cash_u = funded_u_only or _cap_only
            # Open-book mark → one equity line (PnL visible without a second curve).
            open_mtm = 0.0
            for _p in (_eng_early or {}).get("open_book") or []:
                if not isinstance(_p, dict):
                    continue
                try:
                    _e = float(_p.get("entry_usdg") or 0.0)
                    _m = float(_p.get("live_mult") or 0.0)
                except (TypeError, ValueError):
                    continue
                if _e > 0 and _m > 0:
                    open_mtm += _e * _m
            show_u = cash_u + open_mtm  # NAV
            if show_u > 0:
                curve = wallet_day_curve(show_u, day_net=_day_net, nav=True)
                pts = list(curve.get("points") or [])
                daily = list(curve.get("daily") or [])
                day_open = float(curve.get("open_usdg") or show_u)
                funded_u = float(curve.get("funded_usdg") or 0.0)
                fund_ts = float(curve.get("funded_at") or 0.0)
                # Stake / tone baseline = first funded USDG (session), not midnight open.
                stake_sol = (
                    funded_u
                    if funded_u > 0
                    else (day_open if day_open > 0 else show_u)
                )
                bal_sol = show_u
                wallet_note = f" · ETH {eth:.4f} gas"
                _bal = str(ss.get("bal_mode") or "1d")

                def _at_rh(ts: float) -> float:
                    if not pts:
                        return show_u
                    val = float(pts[0].get("usdg") or 0.0)
                    for p in pts:
                        if float(p.get("ts") or 0) <= ts + 0.5:
                            val = float(p.get("usdg") or 0.0)
                    return val

                if _bal == "1d" and daily:
                    # 1D: one close per calendar day since funding.
                    series = [float(b.get("usdg") or 0.0) for b in daily]
                    if series:
                        series[-1] = show_u
                    if len(series) == 1:
                        series = [series[0], show_u]
                    up = show_u >= stake_sol
                else:
                    # RT: NAV samples only — skip legacy cash "live" cliffs in the series.
                    nav_pts = [
                        p
                        for p in pts
                        if str(p.get("tag") or "") not in ("buy", "sell", "fill", "live")
                    ] or pts
                    if len(nav_pts) >= 3:
                        series = [float(p.get("usdg") or 0.0) for p in nav_pts]
                        # Keep chart readable: downsample only if huge.
                        if len(series) > 160:
                            step = max(1, len(series) // 160)
                            head = series[:1]
                            mid = series[1:-1:step]
                            series = head + mid + series[-1:]
                        series[-1] = show_u
                    else:
                        t0 = fund_ts if fund_ts > 0 else (
                            float(pts[0].get("ts") or 0.0) if pts else 0.0
                        )
                        if t0 <= 0 and now_local is not None:
                            t0 = now_local.replace(
                                hour=0, minute=0, second=0, microsecond=0
                            ).timestamp()
                        t1 = (
                            now_local.timestamp()
                            if now_local is not None
                            else time.time()
                        )
                        span = max(1.0, t1 - t0)
                        n = 64
                        series = [_at_rh(t0 + span * i / (n - 1)) for i in range(n)]
                        series[-1] = show_u
                    up = show_u >= stake_sol
                bal_cls = "g" if up else "r"
                bal_tone = "up" if up else "dn"
                chart_color = ("#137333" if theme == "day" else "#00ff66") if up else (
                    "#b91c1c" if theme == "day" else "#ff3355"
                )
                bal_x = clock_axis
                # Bars share the same series/time axis (TradingView volume sync).
                _bars_html = volume_bars(series, match_curve=True)
                bal_svg = svg_area_line(
                    series,
                    chart_color,
                    label=f"{fmt_quote(show_u, 2)} {QUOTE}",
                    x_labels=bal_x,
                    progress=progress,
                    theme=theme,
                    money_axis=True,
                    bars_html=_bars_html,
                )
                bars = ""  # embedded under shared time axis
                ss["rh_bal_tag"] = "NAV" if open_mtm > 0 else ("WALLET" if cash_u > 0 else "CUMULATIVE")
                ss["rh_series"] = series
                ss["rh_cash_usdg"] = cash_u
                ss["rh_open_mtm"] = open_mtm
            else:
                stake_sol = _cap_only
                wallet_note = f" · ETH {eth:.4f} · bridge USDG"
                ss["rh_bal_tag"] = "CUMULATIVE"
        except Exception:
            pass
    bal_subline = f"{state['multiple']:.1f}x the stake · {FEE_BLURB}"
    if DESK_CHAIN == "arc" and not DESK_FF:
        bal_subline = f"on-chain wallet · {FEE_BLURB}"
    elif _IS_RH and not DESK_FF:
        _nav_tag = str(ss.get("rh_bal_tag") or "")
        if _nav_tag == "NAV":
            bal_subline = f"NAV · cash+marks · {FEE_BLURB}"
        else:
            bal_subline = f"RH Uniswap · {FEE_BLURB}"
    realized_sol = float(state.get("pnl") or 0.0)
    # Prefer chain ledger when bots have closed books
    try:
        if _IS_RH:
            from desk_realtime.rh_strategy import realized_snapshot

            snap = realized_snapshot()
            if int(snap.get("closes") or 0) > 0:
                realized_sol = float(snap.get("realized_net_usdg") or realized_sol)
        else:
            from desk_realtime.arc_strategy import realized_snapshot

            snap = realized_snapshot()
            if int(snap.get("closes") or 0) > 0:
                realized_sol = float(snap.get("realized_net_usdc") or realized_sol)
    except Exception:
        pass
    unreal_sol = float(state.get("unreal") or 0.0)
    # Legacy total (equity − stake) kept for spark context only
    pnl_sol = bal_sol - stake_sol
    rz_cls = "g" if realized_sol >= 0 else "r"
    rz_sign = "+" if realized_sol >= 0 else "−"
    ur_cls = "g" if unreal_sol >= 0 else "r"
    ur_sign = "+" if unreal_sol >= 0 else "−"
    ur_sub = "OPEN MARK" if state.get("position") else "FLAT"
    mission_clock = clock_face
    clock_window = clock_sub
    gate_on = max(1, min(10, int(round(float(state["risk"]) / 10.0))))
    gate_pips = "".join(
        f'<i class="{"on" if i < gate_on else ""}"></i>' for i in range(10)
    )
    gate_check = int(state["not_buy"])
    try:
        _gm = DeskBus.metrics() or {}
        if not DESK_FF and _gm.get("not_buy") is not None:
            gate_check = int(_gm["not_buy"])
    except Exception:
        pass
    gate_mode = "VETO / RETEST" if state["risk"] >= 40 else "PASS / ARM"

    # Scoreboard sparklines (right-side dynamic curves)
    spark_bal_vals = [float(v) for v in (ss.get("rh_series") or ss.hist or [STAKE])]
    if _IS_RH and not DESK_FF and ss.get("rh_series"):
        spark_bal_vals = [float(v) for v in ss["rh_series"]]
    spark_rz_vals = []
    _rz_acc = 0.0
    for r in rows:
        if r.get("type") == "close":
            _rz_acc += float(r.get("pnl", 0) or 0)
            spark_rz_vals.append(_rz_acc)
    if not spark_rz_vals:
        spark_rz_vals = [0.0, realized_sol]
    spark_ur_vals = [0.0, unreal_sol] if abs(unreal_sol) > 1e-9 else [0.0, 0.0, 0.0]
    if DESK_FF:
        _clk = random.Random(ss.tick // 3 + 11)
        spark_clock_vals = [
            40 + 28 * math.sin(i / 3.2 + ss.tick / 9) + _clk.uniform(-6, 6)
            for i in range(24)
        ]
    else:
        # Clock spark is the day so far, not a rehearsed oscillator.
        spark_clock_vals = [day_progress() * (i + 1) / 24.0 * 100.0 for i in range(24)]
    spark_bal = svg_sparkline(spark_bal_vals, "#2196F3" if theme == "day" else "#4da3ff", fill=False)
    spark_rz = svg_sparkline(
        spark_rz_vals,
        ("#50A88E" if theme == "day" else "#22c55e") if realized_sol >= 0 else (
            "#D64550" if theme == "day" else "#ff4d5e"
        ),
        fill=True,
    )
    spark_ur = svg_sparkline(
        spark_ur_vals,
        ("#4da3ff" if theme != "day" else "#2196F3") if unreal_sol >= 0 else (
            "#D64550" if theme == "day" else "#ff4d5e"
        ),
        fill=True,
    )
    spark_clock = svg_sparkline(spark_clock_vals, "#c8d0d8", fill=False)

    # Topbar denser KPIs — bind WS METRICS when present, else desk_state fallback
    live_m = DeskBus.metrics()
    if live_m.get("realized_net_usdc") is not None:
        try:
            realized_sol = float(live_m["realized_net_usdc"])
        except Exception:
            pass
    if live_m.get("realized_net_usdg") is not None:
        try:
            realized_sol = float(live_m["realized_net_usdg"])
        except Exception:
            pass
    if _eng_early.get("realized_net_usdg") is not None and _IS_RH:
        try:
            realized_sol = float(_eng_early["realized_net_usdg"])
        except Exception:
            pass
    boot_ts = float(ss.get("boot") or time.time())
    up_sec = int(live_m["uptime_sec"]) if live_m.get("uptime_sec") is not None else max(0, int(time.time() - boot_ts))
    funded_at = 0.0
    funded_usdc = 0.0
    if not DESK_FF and DESK_CHAIN == "arc":
        try:
            from desk_realtime.arc_net import funding_mark

            mark = funding_mark()
            funded_at = float(mark.get("funded_at") or 0)
            funded_usdc = float(mark.get("funded_usdc") or 0)
        except Exception:
            funded_at = 0.0
    elif not DESK_FF and _IS_RH:
        try:
            from desk_realtime.rh_net import funding_mark as rh_funding_mark

            mark = rh_funding_mark(float(ss.get("rh_usdg") or 0.0) or None)
            funded_at = float(mark.get("funded_at") or 0)
            funded_usdc = float(mark.get("funded_usdg") or 0)
        except Exception:
            funded_at = 0.0
    if funded_at > 0:
        funded_elapsed = max(0, int(time.time() - funded_at))
        day_n = 1 + funded_elapsed // 86400
    else:
        day_n = int(live_m["day"]) if live_m.get("day") is not None else max(1, 1 + up_sec // 86400)
        funded_elapsed = 0
    up_h, up_m = up_sec // 3600, (up_sec % 3600) // 60
    uptime_txt = f"{up_h}h {up_m:02d}m"
    # scouted_count → SCAN (RH) / TRENCH (legacy); books → BOOKS; net_out → NOT BUY
    trench_n = int(live_m.get("scouted_count", live_m.get("trench", state.get("entered") or 0)))
    books_n = int(live_m.get("books", state.get("closes") or 0))
    wr_raw = live_m.get("win_rate", state.get("win_rate") or 0)
    not_buy_n = int(live_m.get("not_buy", live_m.get("net_out", state.get("not_buy") or 0)))
    if not DESK_FF and _eng_early:
        if _eng_early.get("scouted_count") is not None:
            trench_n = int(_eng_early["scouted_count"])
            scan_seen = trench_n
        if _eng_early.get("net_out") is not None:
            not_buy_n = int(_eng_early["net_out"])
        if _eng_early.get("not_buy") is not None:
            not_buy_n = int(_eng_early["not_buy"])
        if _eng_early.get("win_rate") is not None:
            wr_raw = _eng_early["win_rate"]
        if _IS_RH and _eng_early.get("slots_open") is not None:
            books_n = int(_eng_early.get("slots_open") or 0)
        if _IS_RH and _eng_early.get("wins") is not None and _eng_early.get("losses") is not None:
            _cw = int(_eng_early.get("wins") or 0) + int(_eng_early.get("losses") or 0)
            if _cw > 0 and _eng_early.get("win_rate") is None:
                wr_raw = 100.0 * float(_eng_early.get("wins") or 0) / _cw
    wr_txt = f"{float(wr_raw):.1f}%"
    if DESK_FF:
        footer_pct = pct
    elif funded_at > 0:
        fh, fm = funded_elapsed // 3600, (funded_elapsed % 3600) // 60
        when = datetime.fromtimestamp(funded_at).astimezone()
        rz_sign_f = "+" if realized_sol >= 0 else "−"
        if _IS_RH and not DESK_FF:
            _day = float((_eng_early or {}).get("day_net_usdg") or realized_sol or 0)
            _send = "SEND ON" if (_eng_early or {}).get("send_armed") else "SEND OFF"
            rz_day = "+" if _day >= 0 else "−"
            fd = funded_elapsed // 86400
            fh, fm = (funded_elapsed % 86400) // 3600, (funded_elapsed % 3600) // 60
            run_txt = f"{fd}d {fh}h {fm:02d}m" if fd > 0 else f"{fh}h {fm:02d}m"
            # Total elapsed since first RH USDG — no hard-cap (shown in SIZE panel).
            footer_time = (
                f"FUNDED {when.strftime('%m-%d %H:%M')} · {fmt_quote(funded_usdc, 2)} {QUOTE}"
                f" · run {run_txt} · {_send}"
                f" · scan {trench_n} · skip {not_buy_n}"
                f" · day {rz_day}{fmt_quote(abs(_day), 2)}"
            )
        else:
            footer_time = (
                f"FUNDED {when.strftime('%m-%d %H:%M')} · {fmt_quote(funded_usdc, 2)} {QUOTE}"
                f" · {fh}h {fm:02d}m · {trench_n} scans · {not_buy_n} not buy"
                f" · pnl {rz_sign_f}{fmt_quote(abs(realized_sol), 2)}"
            )
        footer_pct = (funded_elapsed % 86400) / 86400 * 100.0
    elif _IS_RH and not DESK_FF:
        _w = str(_eng_early.get("wallet") or "")
        _day = float(_eng_early.get("day_net_usdg") or realized_sol or 0)
        _send = "SEND ON" if _eng_early.get("send_armed") else "SEND OFF"
        rz_sign_f = "+" if _day >= 0 else "−"
        footer_time = (
            f"RH {_w or 'wallet —'} · {_send}"
            f" · scan {trench_n} · skip {not_buy_n}"
            f" · day {rz_sign_f}{fmt_quote(abs(_day), 2)}"
            f" · awaiting USDG fund"
        )
        footer_pct = pct
    else:
        footer_time = "FUNDED — · no balance seen yet"
        footer_pct = pct
    agents_live = int(live_m["agents_live"]) if live_m.get("agents_live") is not None else sum(
        1 for v in busy_map.values() if v >= 0.45
    )
    if not DESK_FF and _eng_early.get("agents_live") is not None:
        agents_live = int(_eng_early["agents_live"])
    if not DESK_FF and str(_eng_early.get("desk_mode") or "") == "LIVE" and str(_eng_early.get("valve_gate") or "") == "OPEN":
        if not _IS_RH:
            agents_live = 5
    agents_txt = f"{agents_live}/5"
    wr_val_cls = "g" if float(wr_raw) >= 50 else ("y" if float(wr_raw) > 0 else "r")
    mult_n = float(live_m.get("multiple", state.get("multiple") or 0))
    mult_txt = f"{mult_n:.1f}x" if _eng_early.get("is_position_open") else "—"
    if _IS_RH and not DESK_FF:
        # MULTIPLE slot → hard cap / send (RH has no meme multiple)
        _cap_v = float(_eng_early.get("hard_cap") or stake_sol or STAKE or 0)
        mult_txt = f"{fmt_quote(_cap_v, 2)}"
    kpi_scan_lbl = "SCAN" if _IS_RH else "TRENCH"
    kpi_book_lbl = "SLOTS" if _IS_RH else "BOOKS"
    kpi_mult_lbl = "CAP" if _IS_RH else "MULTIPLE"
    kpi_skip_lbl = "SKIP" if _IS_RH else "NOT BUY"
    from desk_realtime.engine_state import read_engine_state, valve_closed

    eng = read_engine_state()
    if eng.get("valve_gate"):
        live_m = {**live_m, "valve_gate": eng["valve_gate"]}
    halted = (
        DeskBus.halted()
        or HALT_FLAG.exists()
        or valve_closed()
        or bool(ss.get("halted"))
    )
    live_lbl = "HALT" if halted else "LIVE"
    live_cls = "live halted" if halted else "live"
    desk_mode = str(live_m.get("desk_mode") or ("HALTED" if halted else "LIVE"))
    if DESK_FF:
        desk_mode = "FF×100"
        live_lbl = "FF"
        live_cls = "live"
    valve_lbl = str(live_m.get("valve_gate") or ("CLOSED" if halted else "OPEN"))
    if valve_lbl == "CLOSED" or halted:
        gate_mode = "VALVE CLOSED"
    if _IS_RH and not DESK_FF and not state.get("position"):
        if valve_lbl == "CLOSED" or halted:
            phase, blurb = "00 VALVE", "closed · no new buys"
        elif not_buy_n > 0:
            phase, blurb = "02 FILTER", "m5/m15 · liq · veto"

    # RH dock panels — same chrome as paper desk (wallet cluster + survival curve)
    if _IS_RH and not DESK_FF:
        _ob = _eng_early.get("open_book") or []
        _slots_txt = f"{books_n}/{int(_eng_early.get('max_slots') or 1)}"
        _cap_s = float(_eng_early.get("hard_cap") or STAKE or 0.3)
        _day_s = float(_eng_early.get("day_net_usdg") or realized_sol or 0)
        _day_lim = float(os.environ.get("RH_DAY_LOSS_USDG", "10") or 10)
        _send_s = "ON" if _eng_early.get("send_armed") else "OFF"
        _day_cls = "g" if _day_s >= 0 else "r"
        _valve_pct = 100 if valve_lbl == "OPEN" else 8
        # Survival curve keyed to day-loss burn (same visual language as paper ruin).
        _burn = min(100.0, abs(min(0.0, _day_s)) / max(_day_lim, 0.01) * 100.0)
        _rh_wal = svg_wallet((int(ss.tick) + int(trench_n)) % 97)
        _rh_ruin = svg_survival(_burn, max(8.0, min(40.0, _cap_s * 80.0)))
        if _ob:
            _syms = [
                f"${str(p.get('symbol') or '?')}"
                for p in _ob[:3]
                if isinstance(p, dict)
            ]
            _flag_lbl, _flag_val = "open", " · ".join(_syms) if _syms else "book"
        else:
            _flag_lbl, _flag_val = "book", "0 open"
        wc_panel_inner = (
            f"<div class='ph'>RH BOOK</div>"
            f"<div class='wc-sub'>open slot · wallet · valve</div>"
            f"<div class='wc-body'>"
            f"<div class='chart-slot'>{_rh_wal}</div>"
            f"<div class='wc-flag'><div class='lbl'>{_flag_lbl}</div>"
            f"<div class='val'>{_flag_val}</div></div></div>"
            f"<div class='wc-pressure'><span class='lbl'>valve {_esc(valve_lbl)}</span>"
            f"<div class='ebar'><i style='width:{_valve_pct}%'></i></div></div>"
        )
        sz_panel_inner = (
            f"<div class='ph'>SIZE · HARD CAP</div>"
            f"<div class='sz-sub'><span>USDG notional</span>"
            f"<span>day halt {_day_lim:g}</span></div>"
            f"<div class='chart-slot'>{_rh_ruin}</div>"
            f"<div class='sz-stats'>"
            f"<div class='cell'><span class='lbl'>cap</span>"
            f"<span class='val y'>{fmt_quote(_cap_s, 2)}</span></div>"
            f"<div class='cell'><span class='lbl'>day net</span>"
            f"<span class='val {_day_cls}'>{_day_s:+.2f}</span></div>"
            f"<div class='cell'><span class='lbl'>send</span>"
            f"<span class='val'>{_send_s}</span></div>"
            f"</div>"
        )
        emb_foot_a = int(_eng_early.get("wins") or state.get("entered") or 0)
        emb_foot_r = int(_eng_early.get("not_buy") or not_buy_n)
        edge_n = int(_eng_early.get("wins") or 0) + int(_eng_early.get("losses") or 0)
    else:
        wc_panel_inner = (
            f"<div class='ph'>WALLET CLUSTER</div>"
            f"<div class='wc-sub'>linked buyers · exit pressure</div>"
            f"<div class='wc-body'>"
            f"<div class='chart-slot'>{wal_svg}</div>"
            f"<div class='wc-flag'><div class='lbl'>linked</div>"
            f"<div class='val'>{flagged} flagged</div></div></div>"
            f"<div class='wc-pressure'><span class='lbl'>exit pressure</span>"
            f"<div class='ebar'><i style='width:{exit_pressure}%'></i></div></div>"
        )
        sz_panel_inner = (
            f"<div class='ph'>SIZING · RISK OF RUIN</div>"
            f"<div class='sz-sub'><span>kelly vs survival</span><span>survival · 1000 sims</span></div>"
            f"<div class='chart-slot'>{ruin_svg}</div>"
            f"<div class='sz-stats'>"
            f"<div class='cell'><span class='lbl'>full kelly</span>"
            f"<span class='val y'>{full_kelly:.1f}%</span></div>"
            f"<div class='cell'><span class='lbl'>used</span>"
            f"<span class='val g'>{used_kelly:.1f}%</span></div>"
            f"<div class='cell'><span class='lbl'>risk of ruin</span>"
            f"<span class='val r'>{state['ruin']:.1f}%</span></div></div>"
        )
        emb_foot_a = state["entered"]
        emb_foot_r = max(0, scan_seen - state["entered"])

    # Edge model from RH engine closes when available
    if _IS_RH and not DESK_FF:
        _ew = int(_eng_early.get("wins") or 0)
        _el = int(_eng_early.get("losses") or 0)
        edge_n = _ew + _el
        if edge_n == 0:
            edge_tag = "no live closes"
        else:
            edge_tag = "edge accepted" if float(wr_raw) >= 50 else "edge rejected"
        # reuse state expectancy bars but seed wr from engine
        state["win_rate"] = float(wr_raw)
        wr_bar = max(0.0, min(100.0, float(wr_raw)))
        wr_cls = "pos" if float(wr_raw) > 0 else "neg"
        wr_bar_cls = "g" if float(wr_raw) > 0 else "r"

    # Chain block height; smooth via last-good
    block_txt = "—"
    chain_meta = "solana · 5 agents"
    if DESK_CHAIN == "arc" and not DESK_FF:
        try:
            from desk_realtime.arc_net import fetch_arc_block_number, format_block_label
            from desk_realtime.foundry_bin import ARC_CHAIN_ID

            arc_info = fetch_arc_block_number()
            prev = int(ss.get("arc_block") or 0)
            cur = int(arc_info.get("block") or 0)
            if cur > 0:
                ss["arc_block"] = cur
            elif prev > 0:
                arc_info = {**arc_info, "ok": True, "block": prev}
            block_txt = format_block_label(arc_info)
            # Badge already shows Arc · env — meta keeps only chain id / agents
            chain_meta = f"{ARC_CHAIN_ID} · 5 agents"
        except Exception:
            prev_b = ss.get("arc_block")
            block_txt = f"{int(prev_b):,}" if prev_b else "—"
            chain_meta = "5 agents"
    elif DESK_CHAIN == "arc":
        chain_meta = "ff · 5 agents"
    elif _IS_RH and not DESK_FF:
        try:
            from desk_realtime.rh_net import chain_id as rh_cid, preflight as rh_preflight

            _blk_ttl = float(os.environ.get("RH_UI_RPC_TTL", "30") or 30)
            _blk_age = time.time() - float(ss.get("rh_block_ts") or 0)
            prev = int(ss.get("rh_block") or 0)
            cur = prev
            _have_blk = bool(ss.get("rh_block_ts"))
            # Prefer engine_state block from rh_loop; RPC only if cache cold.
            _eng_blk = int((_eng_early or {}).get("block") or 0)
            _eng_wts = float((_eng_early or {}).get("wallet_ts") or 0)
            if _eng_blk > 0 and _eng_wts > 0 and (time.time() - _eng_wts) <= 90:
                ss["rh_block"] = _eng_blk
                ss["rh_block_ts"] = _eng_wts
                cur = _eng_blk
            elif _have_blk and _blk_age <= _blk_ttl:
                pass
            elif _have_blk or ss.get("rh_block_bootstrapped"):
                pf = rh_preflight(timeout=1.0)
                cur = int(pf.get("block") or 0)
                if cur > 0:
                    ss["rh_block"] = cur
                elif prev > 0:
                    cur = prev
                ss["rh_block_ts"] = time.time()
            else:
                ss["rh_block_bootstrapped"] = True
            cur = int(ss.get("rh_block") or cur or 0)
            block_txt = f"{cur:,}" if cur else "—"
            chain_meta = f"{rh_cid()} · RH short"
            send_on = bool(_eng_early.get("send_armed"))
            if send_on:
                chain_meta += " · SEND"
        except Exception:
            prev_b = ss.get("rh_block")
            block_txt = f"{int(prev_b):,}" if prev_b else "—"
            chain_meta = f"{CHAIN_ID} · RH short"
    elif _IS_RH:
        chain_meta = "ff · RH"

    if _IS_RH:
        _net_env, _net_title = rh_network_label()
        arc_badge_html = (
            f'<span class="net-badge" title="{_net_title}">'
            f'<span class="net-name">Miki Crypto Bot</span>'
            f"</span>"
        )
    else:
        _arc_env, _arc_title = arc_network_label()
        _arc_mark = arc_mark_data_uri()
        _env_cls = "is-main" if _arc_env == "mainnet" else "is-test"
        if _arc_mark:
            arc_badge_html = (
                f'<span class="net-badge" title="{_arc_title}">'
                f'<img src="{_arc_mark}" alt="Arc" width="18" height="18"/>'
                f'<span class="net-name">Arc</span>'
                f'<span class="net-env {_env_cls}">{_arc_env}</span>'
                f"</span>"
            )
        else:
            arc_badge_html = (
                f'<span class="net-badge" title="{_arc_title}">'
                f'<span class="net-name">Arc</span>'
                f'<span class="net-env {_env_cls}">{_arc_env}</span>'
                f"</span>"
            )

    _miki_mark = miki_mark_data_uri()
    _rh_mark = rh_mark_data_uri()
    if _IS_RH:
        brand_title = "Robinhood"
        if _rh_mark:
            miki_logo_html = (
                f'<span class="logo is-rh"><img src="{_rh_mark}" alt="Robinhood" width="28" height="28"/></span>'
            )
        else:
            miki_logo_html = '<span class="logo is-rh">◆</span>'
        brand_title_html = f'<span class="title is-rh">{brand_title}</span>'
    else:
        brand_title = "miki crypto bot"
        if _miki_mark:
            miki_logo_html = f'<span class="logo"><img src="{_miki_mark}" alt="miki" width="28" height="28"/></span>'
        else:
            miki_logo_html = '<span class="logo">⚡</span>'
        brand_title_html = f'<span class="title">{brand_title}</span>'

    desk_html = f"""
<div class="{desk_dc_cls}" data-theme="{theme}">
  {dc_banner}
  <div class="glass topbar">
    <div class="brand">{miki_logo_html}{brand_title_html}</div>
    {arc_badge_html}
    <div class="vdiv"></div>
    <div class="id">
      <div class="sub">{brand_sub}</div>
      <div class="meta">{chain_meta} · {desk_mode}</div>
    </div>
    <div class="vdiv"></div>
    <div class="metrics">
      <div class="metrics-grid">
        <div class="kv"><span class="lbl">BLOCK</span><span class="val g">{block_txt}</span></div>
        <div class="kv"><span class="lbl">DAY</span><span class="val">{day_n}</span></div>
        <div class="kv"><span class="lbl">UPTIME</span><span class="val">{uptime_txt}</span></div>
        <div class="kv"><span class="lbl">{kpi_scan_lbl}</span><span class="val">{trench_n}</span></div>
        <div class="kv"><span class="lbl">{kpi_book_lbl}</span><span class="val">{books_n}</span></div>
        <div class="kv"><span class="lbl">WIN RATE</span><span class="val {wr_val_cls}">{wr_txt}</span></div>
        <div class="kv"><span class="lbl">AGENTS</span><span class="val g">{agents_txt}</span></div>
        <div class="kv"><span class="lbl">STAKE</span><span class="val">{fmt_quote(stake_sol, 2)} {QUOTE}</span></div>
        <div class="kv"><span class="lbl">{kpi_mult_lbl}</span><span class="val y">{mult_txt}</span></div>
        <div class="kv"><span class="lbl">{kpi_skip_lbl}</span><span class="val r">{not_buy_n}</span></div>
      </div>
      <div class="{live_cls}">{live_lbl}</div>
      <span class="theme-chip" title="auto by local clock · 07:00–19:00 DAY">
        <span>THEME</span><b>{theme_name}</b>
      </span>
    </div>
  </div>

  <div class="w-full my-5">
  <div class="desk-score">
    <div class="glass scoreboard-cell">
      <div class="sb-lbl">BALANCE / {QUOTE}</div>
      <div class="sb-val">{fmt_quote(bal_sol, 2)}</div>
      <div class="sb-sub">FROM {fmt_quote(stake_sol, 2)} {QUOTE}{wallet_note}</div>
      <div class="sb-spark">{spark_bal}</div>
    </div>
    <div class="glass scoreboard-cell">
      <div class="sb-lbl">REALIZED PNL</div>
      <div class="sb-val {rz_cls}">{rz_sign}{fmt_sol(abs(realized_sol), 2)}</div>
      <div class="sb-sub {rz_cls}">CLOSED · BANKED</div>
      <div class="sb-spark">{spark_rz}</div>
    </div>
    <div class="glass scoreboard-cell">
      <div class="sb-lbl">UNREALIZED PNL</div>
      <div class="sb-val {ur_cls}">{ur_sign}{fmt_sol(abs(unreal_sol), 2)}</div>
      <div class="sb-sub {ur_cls}">{ur_sub}</div>
      <div class="sb-spark">{spark_ur}</div>
    </div>
    <div class="glass scoreboard-cell">
      <div class="sb-lbl">CLOCK</div>
      <div class="sb-val clock">{mission_clock}</div>
      <div class="sb-sub">{clock_window}</div>
      <div class="sb-spark">{spark_clock}</div>
    </div>
    <div class="glass scoreboard-cell">
      <div class="sb-gate-top">
        <div class="sb-lbl alert">APPROVAL GATE</div>
        <span class="sb-dot"></span>
      </div>
      <div class="sb-gate-row">
        <span class="sb-gate-name">MIKI</span>
        <span class="sb-gate-tag">{gate_mode}</span>
      </div>
      <div class="sb-pips">{gate_pips}</div>
      <div class="sb-sub r">CHECK {gate_check} / EXIT DEPTH</div>
    </div>
  </div>
  </div>

  <div class="w-full my-5">
  <div class="desk-top">
    <div class="glass">
      <div class="ph ph-green"><span>THE BALANCE</span>
        <span class="tag">{ss.get("rh_bal_tag") or "CUMULATIVE"} · {span_tag} · {fmt_quote(stake_sol, 2)} {QUOTE} → NOW</span>
        <span class="bal-mode-row">
          <a class="{"on" if ss.get("bal_mode") == "1d" else ""}" href="?bal=1d" title="日线 · 入金日起按天">1D</a>
          <a class="{"on" if ss.get("bal_mode") == "rt" else ""}" href="?bal=rt" title="实时 · 入金→现在全程密曲线">RT</a>
        </span></div>
      <div class="bal-head">
        <div>
          <div class="bal-big {bal_tone}">{fmt_quote(bal_sol, 2)} <span class="unit">{QUOTE}</span></div>
          <div class="bal-sub">{bal_subline}</div>
        </div>
        <div>
          <span class="phase">{phase}</span>
          <div class="bal-sub">{blurb}</div>
        </div>
      </div>
      <div class="chart-slot">{bal_svg}</div>
      {f'<div class="outcome-row">{bars}</div>' if bars else ""}
    </div>
    <div class="desk-top-right">
      <div class="pair pair-top">
        <div class="glass {feed_ff_cls}">
          <div class="ph"><span>DESK FEED</span><span class="tag">{feed_event_n} EVENTS</span></div>
          <div class="panel-sub">live event stream · activity log</div>
          <div class="feed-tools">
            <div class="feed-filters">
              <a class="{"on" if feed_ff == "all" else ""}" href="?ff=all">All</a>
              <a class="{"on" if feed_ff == "entries" else ""}" href="?ff=entries">Entries</a>
              <a class="{"on" if feed_ff == "skipped" else ""}" href="?ff=skipped">Skipped</a>
              <a class="{"on" if feed_ff == "errors" else ""}" href="?ff=errors">Errors</a>
            </div>
          </div>
          <div class="glass-body">
            {feed_pin_html}
            <div class="feed">{feed_html}</div>
          </div>
        </div>
        <div class="{pos_glass_cls}">
          <div class="glass-body">{pos_body}</div>
        </div>
      </div>
    </div>
  </div>
  </div>

  <div class="w-full my-5">
    <div class="desk-sig">
    <div class="glass sig-module">
      <div class="sig-head">
        <span class="sig-title">◆ SIGNAL INTERCEPT</span>
        <span class="sig-tag">{sig_tag}</span>
      </div>
      <div class="sig-wave">{sig_bars}</div>
    </div>
  </div>
  </div>

  <div class="desk-body">
    <div class="desk-left">
      <div class="glass">
        <div class="agents-panel">
          <div class="ph ph-green">{agents_ph}</div>
          <div class="stage"><div class="agents floor">{agents_html}</div></div>
        </div>
      </div>

      <div class="dock">
        <div class="glass">
          <div class="mf-panel">
            <div class="ph">STRATEGY MANIFOLD</div>
            <div class="mf-sub">{manifold_sub}</div>
            <div class="chart-slot">{man_svg}</div>
            <div class="mf-foot">
              <span class="g">dims 4/4</span>
              <span class="dim">coh {0.0 if (not DESK_FF and int(state["closes"]) == 0) else state["coh"]:.2f}</span>
            </div>
          </div>
        </div>
        <div class="glass">
          <div class="emb-panel">
            <div class="ph">{emb_title}</div>
            <div class="emb-sub">{narr_sub}</div>
            <div class="chart-slot">{emb_svg}</div>
            <div class="emb-foot">
              <span class="g">{emb_accepted_lbl} {emb_foot_a}</span>
              <span class="r">{emb_rejected_lbl} {emb_foot_r}</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="desk-right">
      <div class="pair pair-mid">
        <div class="glass">
          <div class="ac-wrap">
            <div class="ph">AGENT CONSENSUS</div>
            <div class="glass-body">{consensus_html}</div>
          </div>
        </div>
        <div class="glass">
          <div class="panel-name">SCAN GRID</div>
          <div class="panel-sub">{scan_sub}</div>
          <div class="glass-body"><div class="scan-wrap">
            <div class="scan-grid">{grid_cells}</div>
            <div class="glass-foot">seen {scan_seen} · entered {state["entered"]}</div>
          </div></div>
        </div>
      </div>

      <div class="glass edge-card">
        <div class="edge-head">
          <div class="edge-title">EDGE MODEL</div>
          <div class="edge-sub">{edge_sub}</div>
        </div>
        <div class="edge-body">
          <div class="edge-left">
            <div class="edge-formula">E[R] = p·W − (1−p)·L</div>
            <div class="edge-meta">sample {edge_n} trades · {edge_tag}</div>
            <div class="edge-exp {exp_cls}">expectancy {state["expectancy"]:+.2f}R</div>
          </div>
          <div class="edge-stats">
            <div class="edge-stat">
              <div class="lbl">win rate</div>
              <div class="val {wr_cls}">{state["win_rate"]:.0f}%</div>
              <div class="ebar"><i class="{wr_bar_cls}" style="width:{wr_bar:.0f}%"></i></div>
            </div>
            <div class="edge-stat">
              <div class="lbl">avg win</div>
              <div class="val pos">{state["avg_win"]:.1f}&nbsp;R</div>
              <div class="ebar"><i class="g" style="width:{aw_bar:.0f}%"></i></div>
            </div>
            <div class="edge-stat">
              <div class="lbl">avg loss</div>
              <div class="val neg">{state["avg_loss"]:.2f}&nbsp;R</div>
              <div class="ebar"><i class="r" style="width:{al_bar:.0f}%"></i></div>
            </div>
          </div>
        </div>
      </div>

      <div class="pair pair-bot">
        <div class="glass">
          <div class="wc-panel">
            {wc_panel_inner}
          </div>
        </div>
        <div class="glass">
          <div class="sz-panel">
            {sz_panel_inner}
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="w-full my-5">
  <div class="glass footer">
    <span class="who">● desk @cryptoart_miki</span>
    <span class="time">{footer_time}</span>
    <span class="bar"><i style="width:{footer_pct:.1f}%"></i></span>
  </div>
  </div>
</div>
"""
    # Streamlit markdown: leading 4+ spaces = code fence; "$...$" = KaTeX. Neutralize both.
    desk_html = "\n".join(line.lstrip() for line in desk_html.splitlines())
    st.markdown(desk_html, unsafe_allow_html=True)

    # FF: fragment run_every drives the next frame — do NOT st.rerun() (full remount = black flash)
    if DESK_FF and ss.get("ff_done"):
        st.caption(
            f"FASTFORWARD complete · {int(ss.get('ff_total') or 0)} events · "
            f"mission clock 08:00 · restart UI to replay"
        )

    # Debug / mock controls — off by default (avoids duplicate STOP·DEPLOY chrome)
    if os.environ.get("DESK_DEBUG", "0").strip() in ("1", "true", "True"):
        ca, cb, cc = st.columns([6, 1, 1])
        with cb:
            if st.button("⏹ STOP", key="desk_emergency_stop", help="Halt scanning"):
                from desk_realtime.engine_state import set_valve
                ss.halted = True
                DeskBus.set_halted(True)
                set_valve(True)
                inject_local([{
                    "status": "HOLD_OFF",
                    "agent_type": "EXIT",
                    "token_name": "$DESK",
                    "log_text": "EMERGENCY STOP · scanning paused · valve CLOSED",
                }])
                DeskBus.set_metrics({
                    "desk_mode": "HALTED",
                    "agents_live": 0,
                    "valve_gate": "CLOSED",
                })
                st.rerun()
        with cc:
            if st.button("⬆ DEPLOY", key="desk_deploy", help="Hot-deploy signal"):
                DEPLOY_FLAG.parent.mkdir(parents=True, exist_ok=True)
                DEPLOY_FLAG.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
                DeskBus.set_metrics({"desk_mode": "DEPLOY", "deploy_at": time.time()})
                inject_local([{
                    "status": "HOLD_OFF",
                    "agent_type": "EXIT",
                    "token_name": "$DESK",
                    "log_text": "DEPLOY queued · reload strategy rules",
                }])
                st.rerun()
        with ca:
            if halted and st.button("▶ RESUME", key="desk_resume"):
                from desk_realtime.engine_state import set_valve
                ss.halted = False
                DeskBus.set_halted(False)
                set_valve(False)
                DeskBus.set_metrics({"desk_mode": "LIVE", "valve_gate": "OPEN"})
                inject_local([{
                    "status": "INIT",
                    "agent_type": "EXIT",
                    "token_name": "$DESK",
                    "log_text": "RESUME · scanning armed · valve OPEN",
                }])
                st.rerun()

        st.caption(f"LIVE SYNC · {link} · {DESK_WS_URL}")
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        stages = stage_packets("ARCMEME")
        labels = ["1 INIT", "2 VOTING", "3 VETO", "4 BUY", "5 EXIT", "▶ ALL"]
        cols = [c1, c2, c3, c4, c5]
        for i, col in enumerate(cols):
            if col.button(labels[i], key=f"mock_stage_{i}"):
                inject_local([stages[i]])
                st.rerun()
        if c6.button(labels[5], key="mock_all"):
            inject_local(stages)
            st.rerun()


def install_balance_zoom() -> None:
    """Balance wheel-zoom disabled — MutationObserver fought Streamlit and froze the tab.

    Re-enable later with a wheel-only handler (no document.body observer).
    """
    return


trencher()
# Zoom installer intentionally no-op (see install_balance_zoom).
