"""投递发布清单领域服务。

把分段投递采集回来的回执归并成投递发布清单，关联实际签收的数据段与待消费
数据版本，支撑订阅方按可消费版本分段登记实际消费。

核心约束：
- 同一分段重复收到相同确认（成功签收）时只认第一次，后续签收判为重复回执。
- 只有回执成功（已签收）的分段才能进入可消费清单；接收失败或尚未回执的
  分段永远不会被写成已交付。
- 清单状态沿实际回执解释：交付中 → 可消费 → 已确认，失败重试不会重算
  已经消费的分段（已登记消费的分段保持首次消费记录不变）。
- 每个可消费分段带确切的数据版本编号和实际签收行数。
"""
from __future__ import annotations

from typing import Any

from app.store import store

# 清单/分段状态
ST_DELIVERING = "交付中"          # 仍有分段未拿到成功回执
ST_CONSUMABLE = "可消费"         # 全部分段成功签收，等待订阅方确认
ST_CONFIRMED = "已确认"          # 订阅方已完成全部分段消费登记

# 回执/分段交付状态
RC_PENDING = "待回执"
RC_ACKED = "已签收"
RC_FAILED = "接收失败"
RC_TIMEOUT = "超时"

# 消费登记状态
C_OPEN = "待消费"
C_DONE = "已消费"

TODAY = "2026-11-22"


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


class DeliveryManifestService:
    """分段回执采集、清单归并与分段消费登记的领域服务。"""

    MODULE = "delivery_manifest"

    # ---------- 波次与清单建立 ----------

    def open_wave(
        self,
        *,
        topic: str,
        subscriber_id: int,
        segments: list[dict[str, Any]],
        wave_no: str | None = None,
        delivery_id: int | None = None,
    ) -> dict[str, Any]:
        """数据投递结束时建立波次、发布清单与待消费版本，并预登记各分段。

        segments: [{"segment": "A", "rows": 120, "version": "可选显式版本号"}, ...]
        每个分段落一条 data_version（待消费，行数为投递申报行数），清单分段
        初始为「待回执」，绝不预设为已交付。
        """
        topic = str(topic).strip()
        if not topic:
            raise ValueError("数据主题不能为空")
        if not segments:
            raise ValueError("至少需要一个投递分段")

        waves = store.rows("delivery_wave")
        wave_seq = _next_id(waves)
        wave_code = wave_no or f"WAVE-{str(wave_seq).zfill(4)}"

        wave = {
            "id": wave_seq,
            "波次编号": wave_code,
            "数据主题": topic,
            "订阅方ID": subscriber_id,
            "投递管道ID": delivery_id,
            "分段总数": len(segments),
            "创建时间": TODAY,
            "status": ST_DELIVERING,
            "pending": True,
            "abnormal": False,
        }
        waves.append(wave)

        manifest_rows = store.rows("delivery_manifest")
        manifest_id = _next_id(manifest_rows)
        manifest = {
            "id": manifest_id,
            "清单编号": f"MANIFEST-{str(manifest_id).zfill(4)}",
            "波次编号": wave_code,
            "数据主题": topic,
            "订阅方ID": subscriber_id,
            "分段总数": len(segments),
            "已签收分段": 0,
            "失败分段": 0,
            "待回执分段": len(segments),
            "可消费分段": 0,
            "已消费分段": 0,
            "签收总行数": 0,
            "创建时间": TODAY,
            "status": ST_DELIVERING,
            "pending": True,
            "abnormal": False,
            # 分段明细：每段带版本、申报/实际行数、回执与消费状态
            "segments": [],
        }
        manifest_rows.append(manifest)

        version_rows = store.rows("data_version")
        for idx, seg in enumerate(segments, start=1):
            seg_code = str(seg["segment"]).strip()
            declared_rows = int(seg.get("rows", 0))
            version_no = seg.get("version") or (
                f"DATA-{wave_code}-{seg_code}"
            )
            # 数据版本落库：此时分段尚未签收，版本为待消费，行数先记申报值，
            # 实际行数等回执签收后回填。
            version_rows.append({
                "id": _next_id(version_rows),
                "版本编号": version_no,
                "数据来源": topic,
                "行数统计": declared_rows,
                "实际行数": None,
                "版本标签": "segment_pending_ack",
                "Schema摘要": f"波次{wave_code}分段{seg_code}",
                "创建原因": f"投递波次{wave_code}分段{seg_code}预登记",
                "依赖版本": wave_code,
                "创建时间": TODAY,
                "波次编号": wave_code,
                "分段编号": seg_code,
                "清单编号": manifest["清单编号"],
                "回执状态": RC_PENDING,
                "status": "待消费",
                "pending": True,
                "abnormal": False,
            })
            manifest["segments"].append({
                "分段编号": seg_code,
                "序号": idx,
                "版本编号": version_no,
                "申报行数": declared_rows,
                "实际行数": None,
                "回执状态": RC_PENDING,
                "交付状态": RC_PENDING,
                "消费状态": C_OPEN,
                "首次签收时间": None,
                "首次消费时间": None,
                "签收次数": 0,
                "重复签收次数": 0,
            })

        return {
            "wave": wave,
            "manifest": manifest,
        }

    # ---------- 回执采集 ----------

    def receive_receipt(
        self,
        *,
        wave_no: str,
        segment: str,
        result: str,
        rows: int | None = None,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        """采集一个分段投递的接收回执并归并到发布清单。

        result: 已签收 / 接收失败 / 超时
        幂等规则：同一分段两次收到相同的成功确认，只认第一次有效签收，
        第二次记为重复回执，清单与版本不发生任何变化。
        """
        segment = str(segment).strip()
        received_at = received_at or TODAY
        manifest, wave = self._require_manifest(wave_no)
        seg_row = self._find_segment(manifest, segment)

        receipt_rows = store.rows("delivery_receipt")
        receipt_id = _next_id(receipt_rows)

        # 已成功签收过的分段：再来相同成功确认一律判重复，只认第一次
        already_acked = seg_row["回执状态"] == RC_ACKED
        is_success = result == RC_ACKED
        duplicate = already_acked and is_success

        receipt = {
            "id": receipt_id,
            "回执编号": f"RCPT-{str(receipt_id).zfill(4)}",
            "波次编号": wave_no,
            "清单编号": manifest["清单编号"],
            "分段编号": segment,
            "版本编号": seg_row["版本编号"],
            "回执结果": result,
            "回执行数": rows,
            "接收时间": received_at,
            "是否首次有效签收": bool(is_success and not already_acked),
            "是否重复回执": bool(duplicate),
            "status": result,
            "pending": not is_success,
            "abnormal": result in (RC_FAILED, RC_TIMEOUT),
        }
        receipt_rows.append(receipt)

        if duplicate:
            seg_row["重复签收次数"] += 1
            return {
                "accepted": False,
                "duplicate": True,
                "message": (
                    f"分段{segment}已于{seg_row['首次签收时间']}首次签收，"
                    "重复确认只认第一次，清单未变更"
                ),
                "manifest": manifest,
                "receipt": receipt,
            }

        if is_success:
            actual_rows = int(rows) if rows is not None else seg_row["申报行数"]
            seg_row["回执状态"] = RC_ACKED
            seg_row["交付状态"] = RC_ACKED
            seg_row["实际行数"] = actual_rows
            seg_row["首次签收时间"] = received_at
            seg_row["签收次数"] += 1
            self._sync_version(
                seg_row, manifest, acked=True, actual_rows=actual_rows,
                received_at=received_at,
            )
            message = f"分段{segment}首次签收成功，版本{seg_row['版本编号']}实际{actual_rows}行"
        else:
            # 接收失败 / 超时：分段保持不可交付，绝不写成已交付
            seg_row["回执状态"] = result
            seg_row["交付状态"] = result
            self._sync_version(seg_row, manifest, acked=False, received_at=received_at)
            message = f"分段{segment}回执为{result}，不纳入可消费范围"

        self._recompute_manifest(manifest, wave)
        return {
            "accepted": True,
            "duplicate": False,
            "message": message,
            "manifest": manifest,
            "receipt": receipt,
        }

    # ---------- 订阅方消费确认（按可消费版本分段登记） ----------

    def confirm_consumption(
        self,
        *,
        wave_no: str,
        subscriber_id: int,
        consumed_at: str | None = None,
    ) -> dict[str, Any]:
        """订阅管理确认消费：只对当前可消费（已签收且未消费）的分段登记。

        - 接收失败/超时/待回执的分段不开放，不能登记消费。
        - 已消费分段不重算（失败后重试场景下首次消费记录保持不变）。
        - 全部分段完成消费后清单与波次转为「已确认」。
        """
        consumed_at = consumed_at or TODAY
        manifest, wave = self._require_manifest(wave_no)
        consume_rows = store.rows("subscriber_consumption")

        opened: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for seg in manifest["segments"]:
            if seg["回执状态"] != RC_ACKED:
                blocked.append({
                    "分段编号": seg["分段编号"],
                    "版本编号": seg["版本编号"],
                    "原因": f"分段{seg['回执状态']}，尚未开放消费",
                })
                continue
            if seg["消费状态"] == C_DONE:
                # 重试/再次确认：已消费分段不重算，保留首次消费记录
                skipped.append({
                    "分段编号": seg["分段编号"],
                    "版本编号": seg["版本编号"],
                    "首次消费时间": seg["首次消费时间"],
                })
                continue

            seg["消费状态"] = C_DONE
            seg["首次消费时间"] = consumed_at
            record = {
                "id": _next_id(consume_rows),
                "消费编号": f"CONS-{str(len(consume_rows) + 1).zfill(4)}",
                "波次编号": wave_no,
                "清单编号": manifest["清单编号"],
                "订阅方ID": subscriber_id,
                "分段编号": seg["分段编号"],
                "版本编号": seg["版本编号"],
                "实际行数": seg["实际行数"],
                "首次消费时间": consumed_at,
                "status": C_DONE,
                "pending": False,
                "abnormal": False,
            }
            consume_rows.append(record)
            opened.append(record)

        self._recompute_manifest(manifest, wave)
        return {
            "wave_no": wave_no,
            "manifest_status": manifest["status"],
            "newly_consumed": opened,
            "already_consumed": skipped,
            "not_available": blocked,
            "consumable_segments": self.consumable_segments(wave_no),
        }

    # ---------- 查询 ----------

    def get_manifest(self, wave_no: str) -> dict[str, Any]:
        manifest, _ = self._require_manifest(wave_no)
        return manifest

    def list_receipts(self, wave_no: str) -> list[dict[str, Any]]:
        """数据版本落库后按波次/分段查到的全部回执与实际行数。"""
        return [
            r for r in store.rows("delivery_receipt")
            if r["波次编号"] == wave_no
        ]

    def find_segment_receipt(self, wave_no: str, segment: str) -> dict[str, Any]:
        """按段查回执与实际行数（含首次有效签收标记）。"""
        manifest, _ = self._require_manifest(wave_no)
        seg = self._find_segment(manifest, segment)
        receipts = [
            r for r in store.rows("delivery_receipt")
            if r["波次编号"] == wave_no and r["分段编号"] == segment
        ]
        first_ack = next((r for r in receipts if r["是否首次有效签收"]), None)
        return {
            "波次编号": wave_no,
            "分段编号": segment,
            "版本编号": seg["版本编号"],
            "回执状态": seg["回执状态"],
            "实际行数": seg["实际行数"],
            "首次签收时间": seg["首次签收时间"],
            "首次有效回执": first_ack,
            "全部回执": receipts,
        }

    def consumable_segments(self, wave_no: str) -> list[dict[str, Any]]:
        """当前实际可消费的分段：必须已签收，带确切版本与实际行数。"""
        manifest, _ = self._require_manifest(wave_no)
        return [
            {
                "分段编号": s["分段编号"],
                "版本编号": s["版本编号"],
                "实际行数": s["实际行数"],
                "消费状态": s["消费状态"],
                "首次签收时间": s["首次签收时间"],
            }
            for s in manifest["segments"]
            if s["回执状态"] == RC_ACKED
        ]

    def list_consumption(self, *, subscriber_id: int | None = None,
                         wave_no: str | None = None) -> list[dict[str, Any]]:
        """订阅管理按可消费版本分段登记的实际消费记录。"""
        rows = store.rows("subscriber_consumption")
        if subscriber_id is not None:
            rows = [r for r in rows if r["订阅方ID"] == subscriber_id]
        if wave_no is not None:
            rows = [r for r in rows if r["波次编号"] == wave_no]
        return rows

    def list_waves(self) -> list[dict[str, Any]]:
        return list(store.rows("delivery_wave"))

    # ---------- 内部归并逻辑 ----------

    def _recompute_manifest(self, manifest: dict[str, Any], wave: dict[str, Any]):
        """沿实际回执重算清单计数与状态；已消费分段不被重算。"""
        segs = manifest["segments"]
        acked = [s for s in segs if s["回执状态"] == RC_ACKED]
        failed = [s for s in segs if s["回执状态"] in (RC_FAILED, RC_TIMEOUT)]
        pending = [s for s in segs if s["回执状态"] == RC_PENDING]
        consumed = [s for s in segs if s["消费状态"] == C_DONE]

        manifest["已签收分段"] = len(acked)
        manifest["失败分段"] = len(failed)
        manifest["待回执分段"] = len(pending)
        manifest["可消费分段"] = len(acked)
        manifest["已消费分段"] = len(consumed)
        manifest["签收总行数"] = sum(int(s["实际行数"] or 0) for s in acked)
        manifest["abnormal"] = bool(failed)

        if len(consumed) == len(segs) and segs:
            target = ST_CONFIRMED
        elif not pending and not failed and acked:
            # 全部成功签收：交付中 → 可消费
            target = ST_CONSUMABLE
        else:
            # 仍有分段未拿到成功回执，保持交付中
            target = ST_DELIVERING
        manifest["status"] = target
        manifest["pending"] = target != ST_CONFIRMED
        wave["status"] = target
        wave["pending"] = manifest["pending"]
        wave["abnormal"] = manifest["abnormal"]

    def _sync_version(self, seg: dict[str, Any], manifest: dict[str, Any],
                      *, acked: bool, actual_rows: int | None = None,
                      received_at: str | None = None):
        """把接收确认结果同步到已落库的数据版本。"""
        for ver in store.rows("data_version"):
            if ver.get("版本编号") != seg["版本编号"]:
                continue
            if acked:
                ver["回执状态"] = RC_ACKED
                ver["实际行数"] = actual_rows
                ver["行数统计"] = actual_rows
                ver["版本标签"] = "segment_consumable"
                ver["status"] = "可用"
                ver["pending"] = False
                ver["签收时间"] = received_at
            else:
                ver["回执状态"] = seg["回执状态"]
                ver["版本标签"] = "segment_unavailable"
                ver["status"] = "待消费"
                ver["pending"] = True
                ver["abnormal"] = True

    def _require_manifest(self, wave_no: str) -> tuple[dict[str, Any], dict[str, Any]]:
        wave = next(
            (w for w in store.rows("delivery_wave") if w["波次编号"] == wave_no),
            None,
        )
        if wave is None:
            raise KeyError(f"投递波次 {wave_no} 不存在")
        manifest = next(
            (m for m in store.rows("delivery_manifest") if m["波次编号"] == wave_no),
            None,
        )
        if manifest is None:
            raise KeyError(f"波次 {wave_no} 的投递发布清单不存在")
        return manifest, wave

    def _find_segment(self, manifest: dict[str, Any], segment: str) -> dict[str, Any]:
        seg = next(
            (s for s in manifest["segments"] if s["分段编号"] == segment),
            None,
        )
        if seg is None:
            raise KeyError(
                f"清单{manifest['清单编号']}不存在分段{segment}"
            )
        return seg


manifest_service = DeliveryManifestService()
