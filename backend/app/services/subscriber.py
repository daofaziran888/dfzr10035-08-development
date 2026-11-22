"""订阅管理领域服务：管理数据订阅，与 webhook_router、data_version、call_log、data_cache 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class SubscriberService(BaseModuleService):
    MODULE = "subscriber"
    ENTITY = "订阅方"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["订阅编号", "订阅方", "数据主题", "推送地址", "推送频率", "消费状态", "通知方式", "最后推送"]
    REQUIRED_FIELDS = ["订阅编号", "订阅方", "数据主题"]
    STATUS_ORDER = ["待激活", "推送中", "消费确认", "已停订"]
    ACTION_RULES = {"激活订阅": "推送中", "确认消费": "消费确认", "停订服务": "已停订"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """激活订阅时创建webhook规则和数据版本；确认消费时更新调用日志；停订时清理缓存。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "激活订阅":
            self._create_webhook_rule(entry)
            self._create_data_version(entry)
            message += "，已创建Webhook路由规则和数据版本"
        elif action == "确认消费":
            linked = self._confirm_manifest_consumption(entry)
            self._log_consume_event(entry)
            if linked is not None:
                message += (
                    f"，已按发布清单登记实际消费（新消费{linked['new']}段、"
                    f"未开放{linked['blocked']}段、已消费不重算{linked['skipped']}段）"
                )
            else:
                message += "，已记录消费日志"
        elif action == "停订服务":
            self._cleanup_cache_refs(entry)
            message += "，已清理关联缓存引用"
        return entry, message

    def _create_webhook_rule(self, sub: dict):
        """订阅激活时创建对应Webhook路由规则。"""
        wh_rows = store.rows("webhook_router")
        sid = str(sub["id"]).zfill(4)
        wh_rows.append({
            "id": max((int(r.get("id", 0)) for r in wh_rows), default=0) + 1,
            "规则编号": f"WEBH-SUB-{sid}",
            "触发事件": sub.get("数据主题", ""),
            "目标URL": sub.get("推送地址", ""),
            "签名方式": "HMAC-SHA256",
            "重试上限": 3,
            "超时秒数": 30,
            "投递状态": "就绪",
            "最后触发": "",
            "status": "草稿", "pending": True, "abnormal": False,
        })

    def _create_data_version(self, sub: dict):
        """订阅激活时创建数据版本追溯。"""
        ver_rows = store.rows("data_version")
        sid = str(sub["id"]).zfill(4)
        ver_rows.append({
            "id": max((int(r.get("id", 0)) for r in ver_rows), default=0) + 1,
            "版本编号": f"DATA-SUB-{sid}",
            "数据来源": sub.get("数据主题", ""),
            "行数统计": 0,
            "版本标签": "sub_init",
            "Schema摘要": sub.get("推送频率", ""),
            "创建原因": f"订阅{sid}激活",
            "依赖版本": "v0",
            "创建时间": "2026-11-17",
            "status": "创建中", "pending": True, "abnormal": False,
        })

    def _confirm_manifest_consumption(self, sub: dict) -> dict | None:
        """确认消费联动投递发布清单：按可消费版本分段登记实际消费。

        只处理该订阅主题下尚未最终确认的波次；没有关联波次时返回 None，
        保留原有消费日志行为。失败/超时/待回执分段不会被登记。
        """
        from app.services.delivery_manifest import manifest_service

        topic = sub.get("数据主题", "")
        waves = [
            w for w in store.rows("delivery_wave")
            if w.get("数据主题") == topic and w.get("status") != "已确认"
        ]
        if not waves:
            return None
        # 一个主题同时只归并最新一个在途波次
        wave = max(waves, key=lambda w: int(w.get("id", 0)))
        result = manifest_service.confirm_consumption(
            wave_no=wave["波次编号"], subscriber_id=int(sub["id"]),
        )
        sub["消费状态"] = "消费确认" if not result["not_available"] else "部分消费"
        return {
            "new": len(result["newly_consumed"]),
            "blocked": len(result["not_available"]),
            "skipped": len(result["already_consumed"]),
            "manifest_status": result["manifest_status"],
        }

    def _log_consume_event(self, sub: dict):
        """消费确认时写调用日志。"""
        log_rows = store.rows("call_log")
        sid = str(sub["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-SUB-{sid}",
            "接口端点": sub.get("数据主题", ""),
            "请求参数": sub.get("订阅方", ""),
            "响应状态": "200",
            "响应耗时(ms)": 50,
            "返回行数": 1,
            "错误信息": "",
            "调用时间": "2026-11-17",
            "status": "成功", "pending": False, "abnormal": False,
        })

    def _cleanup_cache_refs(self, sub: dict):
        """停订时清理关联缓存策略引用标记。"""
        topic = sub.get("数据主题", "")
        for cache in store.rows("data_cache"):
            if cache.get("目标端点") == topic:
                cache["命中次数"] = str(max(0, int(cache.get("命中次数", 0)) - 1))

    def check_subscription_chain(self, topic: str) -> dict:
        """跨模块订阅链路检查：订阅方→Webhook路由→数据版本→缓存→投递管道。"""
        subs = [s for s in store.rows("subscriber") if s.get("数据主题") == topic]
        whs = [w for w in store.rows("webhook_router") if w.get("触发事件") == topic]
        vers = [v for v in store.rows("data_version") if v.get("数据来源") == topic]
        caches = [c for c in store.rows("data_cache") if c.get("目标端点") == topic]
        deliveries = [d for d in store.rows("data_delivery") if d.get("数据来源") == topic]
        return {
            "topic": topic,
            "subscribers": len(subs),
            "webhook_rules": len(whs),
            "data_versions": len(vers),
            "cache_policies": len(caches),
            "delivery_pipelines": len(deliveries),
            "chain_complete": len(whs) > 0 and len(vers) > 0,
        }