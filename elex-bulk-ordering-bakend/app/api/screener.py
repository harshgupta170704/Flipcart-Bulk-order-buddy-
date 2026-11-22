"""Product Screener Watchlist API (BOB parity — up to 20 tracked Flipkart products).

Config lives in ``app_settings`` under ``screener.config`` (zero hardcoding):
- ``master_email``: optional Flipkart account email used for offer checks
- ``banks``: list of bank names you hold cards for (e.g. ["HDFC", "ICICI", "SBI"])
- ``check_every_minutes``: automatic check interval in minutes (0 = manual only)
- ``supercoin_offers``: whether to factor SuperCoin discounts
- ``coupon_offers``: whether to factor coupon/deal offers
- ``enabled``: master on/off switch

Tracked items live in ``screener_items`` (up to ``MAX_SLOTS = 20``).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.api.products import is_flipkart_link
from app.automation.product import ProductPreview, add_screener_details, fetch_product_preview
from app.core import app_settings
from app.core.browser import browser_page
from app.core.security import require_user
from app.db.base import get_db
from app.db.models import ScreenerItem, Setting, naive_utcnow

log = logging.getLogger("ordering.api.screener")

router = APIRouter(prefix="/screener", tags=["screener"], dependencies=[Depends(require_user)])

CONFIG_KEY = "screener.config"
MAX_SLOTS = 20

DEFAULT_CONFIG: dict = {
    "master_email": "",
    "banks": [],
    "check_every_minutes": 15,
    "supercoin_offers": True,
    "coupon_offers": True,
    "enabled": False,
}

_RUPEE_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d{1,2})\s*%", re.IGNORECASE)


class ScreenerConfigOut(BaseModel):
    master_email: str = ""
    banks: list[str] = Field(default_factory=list)
    check_every_minutes: int = 15
    supercoin_offers: bool = True
    coupon_offers: bool = True
    enabled: bool = False


class ScreenerConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    master_email: str | None = None
    banks: list[str] | None = None
    check_every_minutes: int | None = Field(default=None, ge=0, le=1440)
    supercoin_offers: bool | None = None
    coupon_offers: bool | None = None
    enabled: bool | None = None


class ScreenerItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_url: str = Field(min_length=10, max_length=2048)
    product_title: str | None = Field(default=None, max_length=255)
    target_price: float | None = Field(default=None, ge=0)
    platform: str = Field(default="flipkart", max_length=32)


class ScreenerItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_title: str | None = Field(default=None, max_length=255)
    target_price: float | None = Field(default=None, ge=0)
    offer_applied: str | None = Field(default=None, max_length=255)
    net_price: float | None = Field(default=None, ge=0)


class ScreenerItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    platform: str
    product_url: str
    product_title: str | None
    target_price: float | None
    listed_price: float | None
    mrp: float | None
    best_offer: str | None
    offer_applied: str | None
    offer_combos: list | None
    net_price: float | None
    in_stock: bool | None
    stock_text: str | None
    last_checked_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _load_config(db: DbSession) -> dict:
    row = db.get(Setting, CONFIG_KEY)
    raw = row.value if row is not None and isinstance(row.value, dict) else {}
    return {**DEFAULT_CONFIG, **raw}


def _save_config(db: DbSession, updates: dict) -> dict:
    merged = {**_load_config(db), **updates}
    merged["master_email"] = str(merged.get("master_email") or "").strip().lower()
    banks_raw = merged.get("banks") or []
    merged["banks"] = list(dict.fromkeys(str(b).strip() for b in banks_raw if str(b).strip()))
    row = db.get(Setting, CONFIG_KEY)
    if row is None:
        db.add(Setting(key=CONFIG_KEY, value=merged))
    else:
        row.value = merged
    db.commit()
    return merged


@router.get("/config", response_model=ScreenerConfigOut)
def get_screener_config(db: DbSession = Depends(get_db)) -> ScreenerConfigOut:
    return ScreenerConfigOut(**_load_config(db))


@router.put("/config", response_model=ScreenerConfigOut)
def update_screener_config(payload: ScreenerConfigIn, db: DbSession = Depends(get_db)) -> ScreenerConfigOut:
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    return ScreenerConfigOut(**_save_config(db, data))


@router.get("/items", response_model=list[ScreenerItemOut])
def list_screener_items(db: DbSession = Depends(get_db)) -> list[ScreenerItemOut]:
    rows = db.scalars(select(ScreenerItem).order_by(ScreenerItem.id.asc())).all()
    return [ScreenerItemOut.model_validate(r) for r in rows]


@router.post("/items", response_model=ScreenerItemOut, status_code=201)
def add_screener_item(payload: ScreenerItemIn, db: DbSession = Depends(get_db)) -> ScreenerItemOut:
    url = payload.product_url.strip()
    if not url.lower().startswith(("http://", "https://")) or not is_flipkart_link(url):
        raise HTTPException(status_code=422, detail="Enter a valid Flipkart product URL (flipkart.com).")
    count = int(db.scalar(select(func.count(ScreenerItem.id))) or 0)
    if count >= MAX_SLOTS:
        raise HTTPException(status_code=409, detail=f"Watchlist is full (maximum {MAX_SLOTS} products).")
    existing = db.scalar(select(ScreenerItem).where(ScreenerItem.product_url == url))
    if existing is not None:
        raise HTTPException(status_code=409, detail="This product URL is already in the screener watchlist.")
    item = ScreenerItem(
        platform=payload.platform.strip().lower() or "flipkart",
        product_url=url,
        product_title=(payload.product_title or "").strip() or None,
        target_price=payload.target_price,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return ScreenerItemOut.model_validate(item)


@router.patch("/items/{item_id}", response_model=ScreenerItemOut)
def update_screener_item(item_id: int, payload: ScreenerItemPatch, db: DbSession = Depends(get_db)) -> ScreenerItemOut:
    item = db.get(ScreenerItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Screener item not found.")
    sent = payload.model_fields_set
    if "product_title" in sent:
        item.product_title = (payload.product_title or "").strip() or None
    if "target_price" in sent:
        item.target_price = payload.target_price
    if "offer_applied" in sent:
        item.offer_applied = (payload.offer_applied or "").strip() or None
    if "net_price" in sent:
        item.net_price = payload.net_price
    db.commit()
    db.refresh(item)
    return ScreenerItemOut.model_validate(item)


@router.delete("/items/{item_id}", status_code=204)
def delete_screener_item(item_id: int, db: DbSession = Depends(get_db)) -> Response:
    item = db.get(ScreenerItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Screener item not found.")
    db.delete(item)
    db.commit()
    return Response(status_code=204)


def _offer_combos_from_preview(preview: ProductPreview, cfg: dict) -> tuple[str | None, float | None, list[dict]]:
    listed = preview.price
    raw_offers: list[str] = list(getattr(preview, "offers", None) or [])
    banks = [b.lower() for b in (cfg.get("banks") or [])]
    use_coins = bool(cfg.get("supercoin_offers", True))
    use_coupons = bool(cfg.get("coupon_offers", True))
    combos: list[dict] = []
    if listed is not None:
        combos.append({"label": "Listed price (no extra offer)", "discount": 0.0, "net_price": round(listed, 2)})

    for text in raw_offers:
        low = text.lower()
        is_coin = "supercoin" in low or "coin" in low
        is_coupon = "coupon" in low or "extra" in low or "special price" in low
        if is_coin and not use_coins:
            continue
        if is_coupon and not use_coupons:
            continue
        if banks and not is_coin and not is_coupon:
            if not any(b in low for b in banks):
                continue
        disc = 0.0
        m_rupee = _RUPEE_RE.search(text)
        m_pct = _PCT_RE.search(text)
        if m_rupee:
            try:
                disc = float(m_rupee.group(1).replace(",", ""))
            except ValueError:
                disc = 0.0
        elif m_pct and listed is not None:
            pct = float(m_pct.group(1))
            disc = round(listed * pct / 100.0, 2)
        if listed is not None and 0 < disc < listed:
            combos.append({
                "label": text[:180],
                "discount": round(disc, 2),
                "net_price": round(max(0.0, listed - disc), 2),
            })

    best_label = None
    best_net = listed
    if combos:
        best = min(combos, key=lambda c: c["net_price"])
        if best["discount"] > 0:
            best_label = best["label"]
            best_net = best["net_price"]
    return best_label, best_net, combos[:8]


async def _check_one(db: DbSession, item: ScreenerItem, cfg: dict) -> ScreenerItem:
    eff = app_settings.get_effective(db)
    try:
        async with browser_page(
            headless=bool(eff.get("browser_headless", True)),
            proxy=str(eff.get("proxy_server") or "") or None,
        ) as (_ctx, page):
            preview = await fetch_product_preview(page, item.product_url)
            if not preview.error:
                await add_screener_details(page, preview)
    except Exception as exc:  # noqa: BLE001
        log.warning("screener check failed for item %s: %s", item.id, exc)
        item.last_checked_at = naive_utcnow()
        item.last_error = "Could not load product page right now."
        db.commit()
        db.refresh(item)
        return item

    item.last_checked_at = naive_utcnow()
    if preview.error:
        item.last_error = preview.error[:500]
        db.commit()
        db.refresh(item)
        return item

    item.last_error = None
    if preview.title:
        item.product_title = preview.title[:255]
    item.listed_price = preview.price
    item.mrp = getattr(preview, "mrp", None)
    item.in_stock = preview.in_stock
    item.stock_text = "In stock" if preview.in_stock else ("Out of stock" if preview.in_stock is False else "Unknown")
    best_label, best_net, combos = _offer_combos_from_preview(preview, cfg)
    item.best_offer = best_label
    item.offer_applied = best_label or "Listed price"
    item.offer_combos = combos
    item.net_price = best_net
    db.commit()
    db.refresh(item)
    return item


@router.post("/items/{item_id}/check", response_model=ScreenerItemOut)
async def check_screener_item(item_id: int, db: DbSession = Depends(get_db)) -> ScreenerItemOut:
    item = db.get(ScreenerItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Screener item not found.")
    cfg = _load_config(db)
    updated = await _check_one(db, item, cfg)
    return ScreenerItemOut.model_validate(updated)


@router.post("/check-all", response_model=list[ScreenerItemOut])
async def check_all_screener_items(db: DbSession = Depends(get_db)) -> list[ScreenerItemOut]:
    cfg = _load_config(db)
    items = list(db.scalars(select(ScreenerItem).order_by(ScreenerItem.id.asc())).all())
    out: list[ScreenerItemOut] = []
    for item in items:
        updated = await _check_one(db, item, cfg)
        out.append(ScreenerItemOut.model_validate(updated))
    return out
