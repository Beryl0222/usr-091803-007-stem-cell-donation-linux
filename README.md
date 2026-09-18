# 造血干细胞捐献协同后端

服务于中华骨髓库非血缘造血干细胞捐献，贯穿 **配型 → 筛查 → 知情同意 → 排期/采集 →
样本交接 → 温控运输 → 回输** 的全流程协同。协调员、捐献者、采集医院、冷链承运方、
受者医院围绕同一份**仅追加事件台账**协作。

## 设计如何回应业务约束

| 业务痛点 | 系统保证 |
| --- | --- |
| 短信与中心回调重复到达，人工台账选错版本 | 外部事件携带 `external_source + external_event_id`，业务命令携带 `idempotency_key`；**同一外部事件只推进一次状态、只通知一次**，重复到达返回首次结果 |
| 已确认的同意被改写、事后说不清 | 同意**只追加版本不可改写**；撤回是新增一个 `withdraw` 版本，原授予版本原样保留。再次同意是再下一版 |
| 各中心按所在地时间解释窗口 | 内部一律存 UTC 绝对时刻；窗口以"当地挂钟 + IANA 时区"申报后换算。排期通知按**每个参与方自己的时区**呈现同一窗口（如乌鲁木齐 08:00 = 上海 10:00） |
| 改期、取消、替代供者、途中温控异常时通知泛滥 | 通知对象 = **因该事件产生待办动作的参与方**。例如途中温控异常只通知承运方（处置）、协调员（决策）、受者医院（暂缓清髓），供者与采集医院不被打扰；取消通知排除原因方本人 |
| 通知需要重发并知道是否送达 | 通知可重发，每次尝试留痕；短信网关送达回执反查消息，重复回执只推进一次送达状态 |
| 敏感身份泄露 | 字段级可见性矩阵 + 供-患双盲：受者医院/承运方只见供者代号，反之亦然；监管只见去标识时间线 |
| 监管要可复核的完整时间线 | 任一采集物可生成按序时间线，明确**每个关键节点采用的同意版本/文书哈希、交出人与接收人、温控异常的最终处置结果**，末尾附哈希链完整性校验 |
| 台账可能被绕过系统篡改 | 每条事件含前驱哈希形成 **SHA-256 哈希链**；`verify_chain` 可独立发现任一行改动 |

状态不由可变主记录保存，而完全由事件流重放得到（event sourcing）。

## 模块结构

```
coordination/
  clock.py         # UTC 时刻、当地挂钟<->绝对时刻、跨时区窗口
  models.py        # 角色、病例/同意/运输状态枚举、身份值对象
  events.py        # 仅追加台账：幂等去重、哈希链、JSONL 持久化、完整性校验
  projection.py    # 事件重放：病例快照、同意版本链、排期版本、交接与异常
  notifications.py # "谁有待办"路由、通知投影（尝试/送达）
  service.py       # 应用服务：命令、状态机守卫、同意规则、通知副作用
  privacy.py       # 字段级身份可见性矩阵、供/患双盲代号
  views.py         # 角色工作台视图、参与方通知收件箱
  audit.py         # 监管可复核时间线（JSON 与纯文本）
  api.py           # HTTP/JSON 适配层与角色鉴权
service.py         # 运行入口：/health、--check、--timeline、启动服务
test_*.py          # 62 个用例，覆盖全部上述保证
```

## 病例状态机

```
matched ─▶ screening ─▶ consent_pending ─▶ consented ─▶ scheduled
   ▲                                                          │
   │                                            四方确认窗口   ▼
   │                                              collected ◀┘
   │                                                  │ 交接给承运方
   │                                                  ▼
   │                                              in_transit ◀──(放行/改送)
   │                                                  │ 温控异常→quarantined
   │                                  送达/签收        ▼
   └──────────────────────────────────────────────▶ infused（终态）

任意非终态 ─▶ cancelled（取消，终态）
任意非终态 ─▶ superseded（启用替代供者，原病例归档，终态；替代供者为独立病例）
```

- 采集前必须四方（供者、采集医院、承运方、受者医院）确认当前排期版本。
- 撤回同意仅允许在采集发生前；撤回后阻止排期与采集。
- 温控异常期间货物 `quarantined`，禁止交接，直到出具放行 / 附条件放行 / 改送 / 报废结论。
- 关键节点（采集、每次交接、回输）在发生时**固化当时生效的同意版本号与文书哈希**，
  日后撤回不改变这些历史事实。

## 运行

```bash
python3 service.py --check            # 配置与领域自检
python3 service.py --port 8000        # 启动（默认内存台账）
python3 service.py --port 8000 --ledger data/ledger.jsonl   # 持久化到 JSONL

# 导出任一采集物的监管复核时间线
python3 service.py --ledger data/ledger.jsonl \
    --timeline CASE-DEMO-01 --tz Asia/Urumqi
python3 service.py --ledger data/ledger.jsonl \
    --timeline CASE-DEMO-01 --json
```

健康检查保持稳定：`GET /health` →
`{"status":"ok","service":"stem-cell-donation","name":"造血干细胞捐献协同"}`。

## HTTP 接口

调用方以请求头标识身份（联调用；生产替换为正式令牌）：
`X-Actor-Id`、`X-Actor-Role`（`coordinator`/`donor`/`donor_center`/`courier`/
`recipient_hospital`/`regulator`）、`X-Actor-Tz`。

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/cases` | 配型成功建档（协调员），含各方与各自时区 |
| `POST /api/cases/{id}/screening/start`、`/pass` | 筛查 |
| `POST /api/cases/{id}/consent/grant`、`/withdraw` | 同意版本化签署 / 撤回 |
| `POST /api/cases/{id}/schedule` | 排期或改期（产生新排期版本） |
| `POST /api/cases/{id}/schedule/confirm` | 一方确认；可带 `external_source/external_event_id` |
| `POST /api/cases/{id}/collection` | 采集完成（需四方已确认 + 有效同意） |
| `POST /api/cases/{id}/handovers` | 双人双签交接（交出人/接收人/温度/容器/凭证） |
| `POST /api/cases/{id}/excursion` | 温控异常上报（外部事件，幂等） |
| `POST /api/cases/{id}/excursion/resolve` | 处置：released/released_with_note/diverted/discarded |
| `POST /api/cases/{id}/deliver`、`/accept`、`/reject`、`/infusion` | 送达/签收/拒收/回输 |
| `POST /api/cases/{id}/cancel`、`/swap-donor` | 取消 / 启用替代供者 |
| `POST /api/notifications/{nid}/resend` | 重发通知（每次尝试留痕） |
| `POST /api/delivery-receipts` | 短信送达回执（外部事件，幂等） |
| `GET /api/cases`、`/api/cases/{id}` | 病例列表 / 角色裁剪后的病例视图 |
| `GET /api/cases/{id}/timeline?tz=…&viewer_role=regulator` | 监管时间线 |
| `GET /api/parties/{pid}/inbox` | 参与方通知收件箱与待办 |

外部回调（HIS 确认、IoT 温控、短信网关回执）在请求体中携带
`external_source` 与 `external_event_id`；重复提交返回
`{"result":"duplicate_external","first_event_seq":…}`，不产生第二次推进或通知。

## 测试

```bash
npm test                 # 发现并运行全部 test_*.py，外加 service_contract 契约
python3 -m unittest -q test_ledger test_consent_flow test_notifications \
    test_privacy_tz_coldchain test_audit test_http test_scenarios
```

覆盖：外部事件/业务命令幂等、哈希链防篡改与持久化重放、同意只追加版本化、
状态机守卫、跨时区窗口、按需通知（配型/改期/撤回/异常/报废/取消/替代供者）、
通知重发与送达回执、字段级隐私与双盲、监管时间线（同意版本/交接人/异常处置）、
HTTP 鉴权与端到端链路。
