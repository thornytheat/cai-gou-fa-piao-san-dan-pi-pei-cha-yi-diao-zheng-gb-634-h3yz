"""M2 路由：列契约与单格试算。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from . import service

router = APIRouter(prefix="/api/contract", tags=["amount"])


@router.get("")
def get_contract(request: Request) -> dict:
    settings = request.app.state.settings
    payload = service.contract_payload(settings)
    payload["samples"] = [
        {"kind": "amount", "raw": "1,234.50", "unit": "元"},
        {"kind": "amount", "raw": "2.35万元", "unit": "万元"},
        {"kind": "amount", "raw": "１２３４．５０", "unit": "元"},
        {"kind": "amount", "raw": "N/A", "unit": "元"},
        {"kind": "rate", "raw": "13%", "unit": None},
        {"kind": "rate", "raw": "0.13", "unit": None},
        {"kind": "qty", "raw": "1,200", "unit": None},
        {"kind": "date", "raw": "2026/11/03", "unit": None},
    ]
    return payload


@router.get("/preview")
def get_preview(request: Request, kind: str = "amount", value: str = "",
                unit: str | None = None) -> dict:
    settings = request.app.state.settings
    return service.preview(settings, kind, value, unit)
