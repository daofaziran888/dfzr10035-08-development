"""端到端验证：分段回执归并 → 投递发布清单 → 分段消费登记。

场景（题目要求）：
1. 采集数据分 A、B、C 三段送到同一订阅主题，建立波次与发布清单；
2. 收到 A、B 签收，让 C 超时；
3. 订阅管理尝试确认消费，核对可实际消费的段（A、B）与暂未开放的段（C）；
4. C 补交并签收后再次确认，同时重送 A 的原回执（幂等：只认第一次签收）；
5. 检查三个段的版本、行数与首次消费记录。
"""
from __future__ import annotations

import os
import sys

# -I 隔离模式下 cwd 不在 sys.path，显式补上项目根目录以导入 app 包。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.delivery_manifest import (
    RC_ACKED,
    RC_TIMEOUT,
    ST_CONFIRMED,
    ST_CONSUMABLE,
    ST_DELIVERING,
    manifest_service,
)
from app.services.subscriber import SubscriberService
from app.store import store

TODAY = "2026-11-22"
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def main() -> int:
    topic = "订单事实主题"
    subscriber_id = 2
    wave_no = "WAVE-ABC-01"

    # ---- 1. 投递结束建档：A/B/C 三分段，申报行数 ----
    opened = manifest_service.open_wave(
        topic=topic,
        subscriber_id=subscriber_id,
        wave_no=wave_no,
        segments=[
            {"segment": "A", "rows": 100},
            {"segment": "B", "rows": 200},
            {"segment": "C", "rows": 300},
        ],
    )
    manifest = opened["manifest"]
    check("建档后清单为交付中", manifest["status"] == ST_DELIVERING, manifest["status"])
    check("建档后3段均待回执、无已交付",
          all(s["回执状态"] == "待回执" for s in manifest["segments"]))
    check("待回执分段=3", manifest["待回执分段"] == 3, str(manifest["待回执分段"]))
    check("数据版本按段落库3条",
          len([v for v in store.rows("data_version") if v.get("波次编号") == wave_no]) == 3)
    seg_versions = {s["分段编号"]: s["版本编号"] for s in manifest["segments"]}

    # ---- 2. A、B 签收，C 超时 ----
    r_a = manifest_service.receive_receipt(
        wave_no=wave_no, segment="A", result=RC_ACKED, rows=100,
        received_at="2026-11-22 10:00")
    r_b = manifest_service.receive_receipt(
        wave_no=wave_no, segment="B", result=RC_ACKED, rows=198,
        received_at="2026-11-22 10:01")
    r_c_timeout = manifest_service.receive_receipt(
        wave_no=wave_no, segment="C", result=RC_TIMEOUT,
        received_at="2026-11-22 10:05")

    check("A 首次签收被接受", r_a["accepted"] and not r_a["duplicate"])
    check("B 实际行数按回执回填198",
          manifest_service.find_segment_receipt(wave_no, "B")["实际行数"] == 198)
    check("C 超时后不被写成已交付",
          manifest_service.find_segment_receipt(wave_no, "C")["回执状态"] == RC_TIMEOUT)
    manifest = manifest_service.get_manifest(wave_no)
    check("C 超时期间清单仍为交付中", manifest["status"] == ST_DELIVERING,
          manifest["status"])
    check("已签收=2 失败=1 待回执=0",
          (manifest["已签收分段"], manifest["失败分段"], manifest["待回执分段"])
          == (2, 1, 0), str((manifest["已签收分段"], manifest["失败分段"])))
    check("签收总行数=298", manifest["签收总行数"] == 298, str(manifest["签收总行数"]))

    consumable = manifest_service.consumable_segments(wave_no)
    check("可消费段恰好是A、B，且带版本与确切行数",
          {s["分段编号"] for s in consumable} == {"A", "B"}
          and all(s["实际行数"] is not None and s["版本编号"] for s in consumable),
          str([(s["分段编号"], s["版本编号"], s["实际行数"]) for s in consumable]))

    # 数据版本状态核对：A/B 可用，C 仍待消费
    versions = {v["分段编号"]: v for v in store.rows("data_version")
                if v.get("波次编号") == wave_no}
    check("A/B 版本可用且实际行数落库",
          versions["A"]["status"] == "可用" and versions["B"]["实际行数"] == 198)
    check("C 版本未开放（待消费+异常标记）",
          versions["C"]["status"] == "待消费" and versions["C"]["abnormal"] is True)

    # ---- 3. 订阅管理确认消费（通过 SubscriberService 动作联动）----
    # 用同主题订阅方
    sub = next(s for s in store.rows("subscriber") if int(s["id"]) == subscriber_id)
    sub["数据主题"] = topic
    sub_entry, msg = SubscriberService().run_action(subscriber_id, "确认消费")
    print("    联动消息：", msg)
    # 动作已联动登记 A、B，再直接调一次确认：不应产生重复登记（幂等）
    result1 = manifest_service.confirm_consumption(
        wave_no=wave_no, subscriber_id=subscriber_id)
    check("订阅动作已登记A、B两段，再次确认不重复登记",
          result1["newly_consumed"] == []
          and len(manifest_service.list_consumption(wave_no=wave_no)) == 2,
          f"重复调用新登记={len(result1['newly_consumed'])} 累计消费登记="
          f"{len(manifest_service.list_consumption(wave_no=wave_no))}")
    check("C 被拦截为暂未开放",
          [b["分段编号"] for b in result1["not_available"]] == ["C"],
          str(result1["not_available"]))
    check("C 未开放时清单保持交付中",
          manifest_service.get_manifest(wave_no)["status"] == ST_DELIVERING)
    first_consume = {
        r["分段编号"]: r["首次消费时间"]
        for r in manifest_service.list_consumption(wave_no=wave_no)
    }
    check("A、B 首次消费时间已登记",
          set(first_consume) == {"A", "B"}, str(first_consume))

    # ---- 4a. C 补交并签收 ----
    r_c = manifest_service.receive_receipt(
        wave_no=wave_no, segment="C", result=RC_ACKED, rows=300,
        received_at="2026-11-22 11:00")
    check("C 补交首次签收被接受", r_c["accepted"] and not r_c["duplicate"])
    manifest = manifest_service.get_manifest(wave_no)
    check("三段齐签收后清单切换为可消费",
          manifest["status"] == ST_CONSUMABLE, manifest["status"])
    check("可消费分段=3", manifest["可消费分段"] == 3)

    # ---- 4b. 重送 A 的原回执（相同确认），必须只认第一次 ----
    r_a_dup = manifest_service.receive_receipt(
        wave_no=wave_no, segment="A", result=RC_ACKED, rows=100,
        received_at="2026-11-22 12:00")
    check("重送A回执判定为重复、未被接受",
          r_a_dup["duplicate"] and not r_a_dup["accepted"], r_a_dup["message"])
    seg_a = next(s for s in manifest_service.get_manifest(wave_no)["segments"]
                 if s["分段编号"] == "A")
    check("A 首次签收时间保持10:00未被覆盖",
          seg_a["首次签收时间"] == "2026-11-22 10:00", seg_a["首次签收时间"])
    check("A 重复签收次数=1", seg_a["重复签收次数"] == 1)
    receipt_a = manifest_service.find_segment_receipt(wave_no, "A")
    check("按段可查到A全部回执且仅一条首次有效",
          len(receipt_a["全部回执"]) == 2
          and receipt_a["首次有效回执"]["接收时间"] == "2026-11-22 10:00")

    # ---- 5. 再次确认消费 ----
    result2 = manifest_service.confirm_consumption(
        wave_no=wave_no, subscriber_id=subscriber_id)
    check("第二次确认只新登记C",
          [r["分段编号"] for r in result2["newly_consumed"]] == ["C"])
    check("A、B 作为已消费被跳过、不重算",
          {s["分段编号"] for s in result2["already_consumed"]} == {"A", "B"})
    check("无未开放分段", result2["not_available"] == [])
    manifest = manifest_service.get_manifest(wave_no)
    check("全段消费后清单转为已确认",
          manifest["status"] == ST_CONFIRMED, manifest["status"])
    check("已消费分段=3", manifest["已消费分段"] == 3)

    # ---- 6. 三段版本、行数、首次消费记录总核对 ----
    all_consume = manifest_service.list_consumption(wave_no=wave_no)
    by_seg = {r["分段编号"]: r for r in all_consume}
    expected_rows = {"A": 100, "B": 198, "C": 300}
    ok_rows = all(
        by_seg[k]["版本编号"] == seg_versions[k]
        and by_seg[k]["实际行数"] == expected_rows[k]
        for k in ("A", "B", "C")
    )
    check("三段消费记录均带确切版本与实际行数", ok_rows,
          str({k: (by_seg[k]["版本编号"], by_seg[k]["实际行数"]) for k in by_seg}))
    check("消费登记总数仍为3（重复确认未产生重复登记）",
          len(all_consume) == 3, str(len(all_consume)))
    check("A 首次消费时间未因重试改变",
          by_seg["A"]["首次消费时间"] == first_consume["A"],
          by_seg["A"]["首次消费时间"])
    check("C 的消费记录为第二次确认时新建，A/B 首次消费记录保持第一轮原值",
          by_seg["C"]["首次消费时间"] is not None
          and by_seg["B"]["首次消费时间"] == first_consume["B"])

    # ---- 7. 失败分段场景：接收失败的分段永远不能进可消费清单 ----
    manifest_service.open_wave(
        topic="失败主题", subscriber_id=subscriber_id, wave_no="WAVE-FAIL-01",
        segments=[{"segment": "X", "rows": 10}, {"segment": "Y", "rows": 20}],
    )
    manifest_service.receive_receipt(
        wave_no="WAVE-FAIL-01", segment="X", result="接收失败")
    manifest_service.receive_receipt(
        wave_no="WAVE-FAIL-01", segment="Y", result=RC_ACKED, rows=20)
    mf = manifest_service.get_manifest("WAVE-FAIL-01")
    check("存在失败分段时清单停留交付中", mf["status"] == ST_DELIVERING, mf["status"])
    check("失败段X不可消费",
          {s["分段编号"] for s in manifest_service.consumable_segments("WAVE-FAIL-01")}
          == {"Y"})
    rf = manifest_service.confirm_consumption(
        wave_no="WAVE-FAIL-01", subscriber_id=subscriber_id)
    check("失败段不能登记消费",
          [b["分段编号"] for b in rf["not_available"]] == ["X"])

    print("\n================ 结果 ================")
    if FAILURES:
        print(f"存在 {len(FAILURES)} 项失败：")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("全部断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
