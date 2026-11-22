"""业务路由汇总：通用模块共用一套资源路由，投递发布清单使用独立领域路由。"""
from __future__ import annotations

from app.routers import delivery_manifest, resource

ROUTERS = [delivery_manifest, resource]
