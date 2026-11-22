# 网关工坊 · API接口采集与数据网关平台

面向第三方API接入注册、接口鉴权编排、限流熔断、响应解析、schema映射、结果缓存、重放恢复与交付订阅的API接口采集与数据网关平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 接口提供方 | `api_provider` | API提供方 | 提供方编号、提供方名称、认证方式 |
| 鉴权配置 | `auth_profile` | 鉴权方案 | 方案编号、提供方、鉴权类型 |
| 接口定义 | `endpoint_def` | 接口端点 | 端点编号、提供方、请求路径 |
| 限流熔断 | `rate_limit` | 限流策略 | 策略编号、目标端点、QPS上限 |
| 请求批次 | `request_batch` | 调用批次 | 批次编号、关联端点、参数模板 |
| 响应解析 | `response_parse` | 解析规则 | 规则编号、目标端点、JSON路径 |
| Schema差异 | `schema_diff` | 差异记录 | 记录编号、关联端点、变更字段 |
| 结果缓存 | `data_cache` | 缓存策略 | 策略编号、目标端点、缓存TTL |
| 重放队列 | `retry_queue` | 重放任务 | 任务编号、失败端点、失败原因 |
| 数据投递 | `data_delivery` | 投递管道 | 管道编号、数据来源、目标地址 |
| 订阅管理 | `subscriber` | 订阅方 | 订阅编号、订阅方、数据主题 |
| 接口监控 | `api_monitor` | 监控指标 | 指标编号、目标端点、监控类型 |
| Webhook路由 | `webhook_router` | 回调规则 | 规则编号、触发事件、目标URL |
| 传输加密 | `data_encrypt` | 加密配置 | 配置编号、数据范围、加密算法 |
| 调用日志 | `call_log` | 调用记录 | 记录编号、接口端点、请求参数 |
| 数据版本 | `data_version` | 数据快照 | 版本编号、数据来源、行数统计 |
| 管道审计 | `pipeline_audit` | 审计记录 | 记录编号、管道名称、操作人 |
| 治理报告 | `govern_report` | 治理报告 | 报告编号、评估范围、合规指标 |
| 增量追踪 | `incr_tracker` | 增量标记 | 标记编号、目标端点、增量字段 |
| 抽样校验 | `sample_check` | 抽样计划 | 计划编号、目标端点、抽样比例 |
| 用量计费 | `cost_account` | 用量账单 | 账单编号、提供方、计费周期 |
| 任务调度 | `task_schedule` | 调度计划 | 计划编号、关联端点、Cron表达式 |

### 投递发布清单（分段回执归并）

数据投递结束只生成版本记录，是否真正交付要以分段接收回执为准。
`delivery_manifest` 领域把分段投递采集回来的回执归并成**投递发布清单**，
关联实际签收的数据段与待消费数据版本，独立于通用资源路由，接口前缀
`/api/delivery-manifest`：

| 接口 | 作用 |
| --- | --- |
| `POST /waves` | 投递结束建立波次、发布清单，并按分段预登记待消费数据版本（初始均为「待回执」，不预设已交付） |
| `POST /receipts` | 采集分段回执（已签收/接收失败/超时），同一分段重复成功签收只认第一次，判为重复回执且清单不变 |
| `GET /waves/{wave_no}/manifest` | 查发布清单与各分段版本、申报/实际行数、回执与消费状态 |
| `GET /waves/{wave_no}/consumable` | 查当前实际可消费分段（必须已签收，带确切版本和行数） |
| `GET /waves/{wave_no}/segments/{segment}/receipt` | 按段查回执、首次有效签收与实际行数 |
| `GET /waves/{wave_no}/receipts` | 查波次全部回执 |
| `POST /consumption` | 订阅确认：只登记可消费分段；失败/超时/待回执分段拦截，已消费分段不重算 |
| `GET /consumption` | 按订阅方/波次查分段实际消费登记 |

规则要点：

- 清单状态沿实际回执流转：`交付中 → 可消费 → 已确认`。任一分段接收失败、
  超时或尚未回执，清单都停留在「交付中」，该分段不会被写成已交付。
- 每个可消费分段带确切的数据版本编号和实际签收行数，回执结果同步回
  `data_version`（含波次编号、分段编号、回执状态、实际行数），可按段查询。
- 订阅管理（`subscriber`）的「确认消费」动作会自动联动同主题在途波次，
  按可消费版本分段登记实际消费；失败后重试不会重算已经消费的分段，
  首次消费记录保持不变。
- 后端逻辑在 `backend/app/services/delivery_manifest.py`，前端领域封装在
  `frontend/src/domain/delivery_manifest.ts`，端到端验证脚本在
  `backend/scripts/verify_delivery_manifest.py`。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
