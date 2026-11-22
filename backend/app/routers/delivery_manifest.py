"""投递发布清单路由：波次建档、分段回执采集、清单归并查询与分段消费登记。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.delivery_manifest import manifest_service

router = APIRouter(prefix="/api/delivery-manifest", tags=["投递发布清单"])


class SegmentSpec(BaseModel):
    segment: str
    rows: int = 0
    version: str | None = None


class OpenWavePayload(BaseModel):
    wave_no: str | None = None
    topic: str
    subscriber_id: int
    delivery_id: int | None = None
    segments: list[SegmentSpec] = Field(default_factory=list)


class ReceiptPayload(BaseModel):
    wave_no: str
    segment: str
    result: str  # 已签收 / 接收失败 / 超时
    rows: int | None = None
    received_at: str | None = None


class ConsumePayload(BaseModel):
    wave_no: str
    subscriber_id: int
    consumed_at: str | None = None


@router.post("/waves")
def open_wave(payload: OpenWavePayload) -> dict[str, Any]:
    try:
        result = manifest_service.open_wave(
            topic=payload.topic,
            subscriber_id=payload.subscriber_id,
            delivery_id=payload.delivery_id,
            wave_no=payload.wave_no,
            segments=[s.model_dump() for s in payload.segments],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "投递波次与发布清单已建立", **result}


@router.get("/waves")
def list_waves() -> dict[str, Any]:
    waves = manifest_service.list_waves()
    return {"items": waves, "total": len(waves)}


@router.post("/receipts")
def receive_receipt(payload: ReceiptPayload) -> dict[str, Any]:
    try:
        result = manifest_service.receive_receipt(
            wave_no=payload.wave_no,
            segment=payload.segment,
            result=payload.result,
            rows=payload.rows,
            received_at=payload.received_at,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.get("/waves/{wave_no}/manifest")
def get_manifest(wave_no: str) -> dict[str, Any]:
    try:
        return manifest_service.get_manifest(wave_no)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/waves/{wave_no}/receipts")
def list_receipts(wave_no: str) -> dict[str, Any]:
    items = manifest_service.list_receipts(wave_no)
    return {"items": items, "total": len(items)}


@router.get("/waves/{wave_no}/segments/{segment}/receipt")
def segment_receipt(wave_no: str, segment: str) -> dict[str, Any]:
    try:
        return manifest_service.find_segment_receipt(wave_no, segment)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/waves/{wave_no}/consumable")
def consumable_segments(wave_no: str) -> dict[str, Any]:
    try:
        items = manifest_service.consumable_segments(wave_no)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"items": items, "total": len(items)}


@router.post("/consumption")
def confirm_consumption(payload: ConsumePayload) -> dict[str, Any]:
    try:
        result = manifest_service.confirm_consumption(
            wave_no=payload.wave_no,
            subscriber_id=payload.subscriber_id,
            consumed_at=payload.consumed_at,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.get("/consumption")
def list_consumption(subscriber_id: int | None = None,
                     wave_no: str | None = None) -> dict[str, Any]:
    items = manifest_service.list_consumption(
        subscriber_id=subscriber_id, wave_no=wave_no)
    return {"items": items, "total": len(items)}
