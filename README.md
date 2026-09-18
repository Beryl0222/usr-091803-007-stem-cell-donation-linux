# 造血干细胞捐献协同后端

面向中华骨髓库跨机构协同的后端服务：一条非血缘配型成功通知之后，在**捐献者、采集医院、冷链承运方、受者医院、分库协调员**之间贯穿
**配型 → 筛查 → 同意 → 排期 → 采集 → 样本交接 → 温控运输 → 回输** 全流程。

- 内部一律存 **UTC 绝对时刻**，窗口按**各中心所在地时区**解释；
- 短信、IoT、中心回调**重复到达同一外部事件，状态只推进一次**；
- 已确认同意**不可改写**，撤回是引用原版本的**更高新版本**；
- 改期、取消、替代供者、途中温控异常时**只通知真正需要处理的人**，其余方“抑制留痕但不打扰”；
- 通知**允许重发**，每次发送与**送达回执**都记录；
- 敏感身份按**职责最小化**展示（供/患双盲）；
- 面对任一采集物可生成**监管可复核的完整时间线**：每个节点采用的同意版本、交接人、温控异常处置结果，并附 **SHA-256 哈希链**完整性校验。

无第三方依赖，仅用 Python 3.10+ 标准库（`zoneinfo`）。

## 运行

```bash
python3 service.py --check          # 基础配置自检
python3 service.py --port 8000      # 启动（默认装配演示目录）
curl http://127.0.0.1:8000/health

python3 demo.py                     # 端到端业务演示（含撤回/替代供者/温控异常/时间线/脱敏）
npm test                            # 根契约测试 + 68 个领域测试
```

演示目录（令牌即 `tk_<账号>`）：

| 账号 | 角色 | 机构（时区） |
|---|---|---|
| `u_coord` 古丽娜 | 分库协调员 | 新疆分库 `Asia/Urumqi` |
| `u_daff` 刘联络 | 捐献者联络员 | 新疆分库 |
| `u_coll` 王护士 | 采集科护士 | 北京采集医院 `Asia/Shanghai` |
| `u_car` 赵押运 | 冷链押运员 | 神州冷链 |
| `u_recv` 孙接收 | 输血科接收员 | 上海受者医院 |
| `u_doc` 李医生 | 受者主治医生 | 上海受者医院 |
| `u_reg` 周督查 | 监管质控 | 新疆分库 |

## 领域与状态机

```
matched → screening → consented → scheduled → collected → in_transit → delivered → infused → closed
                │           │           │            │            │
                │           └──(撤回)──→ donor_withdrawn           └─(温控报废/到院拒收)→ recollect →（同意仍有效）重排期
                └─(筛查不过)→ 由协调员决定备选；终态后可 match.alternative_activated 启用替代供者（新建病例，旧病例挂起留痕）
```

- 时间槽（Slot）**版本化**：改期不覆盖旧窗口，旧版本置 `cancelled` 并保留原因，新窗口为 `proposed` 待确认。
- 同意（ConsentVersion）**只追加**：`grant` / `withdrawal`，撤回必须 `supersedes_version` 引用被撤回的 grant。
- 产品在采集完成时**绑定当时的同意版本**（`product.consent_version`）；即便日后追加撤回版本，历史节点采用的同意仍如实保留。
- 交接（Handover）校验**持有链连续性**（交出人必须属于当前持有机构）、温度读数与封条；封条破损需显式破损接收确认。
- 温控异常（TempExcursion）有三种闭环处置：`continue`（评估继续）、`release_with_waiver`（须附 `waiver_ref` 特许放行单）、`recollect`（报废重采）。异常未闭环时禁止正式接收入库与回输。

## 关键不变量

| 需求 | 实现 |
|---|---|
| 同一外部事件只推进一次 | `idempotency_key`（显式 `idem` 或按事件类型的自然键）唯一索引；重复事件 `applied=false` 并回指首事件，不产生通知、不改状态 |
| 各中心按所在地时间解释窗口 | UTC 存储；Slot 携带解释时区，`local_times` 同时给出所有相关机构的本地时间 |
| 只通知需要处理的人 | `coord/routing.py` 逐事件给出 `targets` 与 `suppressed`（抑制也落库，含原因），按里程碑判断各方是否已卷入 |
| 同意新版本撤回、不可改写 | grant/withdrawal 追加版本；同号文书拒绝重复登记；进入运输/回输后的撤回拒绝直接改写，须走医疗伦理应急 |
| 通知可重发、送达有记录 | 重发追加 `sends[].attempt`；送达回执翻转 `delivered_at_utc`，重复回执幂等、不覆盖首次送达时间 |
| 职责最小化展示 | `coord/projection.py` 字段级裁剪（见下表）；机构维度限定可访问病例范围 |
| 监管可复核时间线 | `GET /api/products/{code}/timeline`：节点含行动人、采用同意、交接人、异常处置、`prev_hash/hash`；`/api/audit/verify` 校验哈希链 |

**字段可见性**：

| 角色 | 供者身份 | HLA | 同意书原件 | 说明 |
|---|---|---|---|---|
| 监管 / 协调员 | ✅ | ✅ | ✅ | 最完整视图（监管访问本身入审计） |
| 捐献者联络员 | ✅ | ❌ | ✅ | 供者侧 |
| 受者主治医生 | ❌ | ✅ | ❌ | 双盲：知病情/配型不知供者 |
| 采集/接收/冷链 | ❌ | ❌ | ❌ | 只见产品码与作业信息；交接工作人员姓名保留以落实责任 |

## HTTP 接口

所有 `/api/*` 需请求头 `X-Staff-Token`；角色不符返回 `403`，未登录 `401`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务身份（免鉴权） |
| POST | `/api/searches` | 受者医院发起检索需求 |
| POST | `/api/events` | 投递外部事件（核心入口） |
| GET | `/api/cases` / `/api/cases/{id}` | 病例列表/详情（按机构范围 + 字段脱敏） |
| GET | `/api/cases/{id}/timeline` | 病例时间线 |
| GET | `/api/products/{code}` | 采集物（交接链/温控） |
| GET | `/api/products/{code}/timeline` | **采集物完整监管时间线** |
| GET | `/api/notifications?case_id=` | 通知（仅见本人，协调/监管见全部） |
| POST | `/api/notifications/{id}/resend` | 重发通知 |
| GET | `/api/events` | 事件台账（含重复事件，协调/监管） |
| GET | `/api/audit/verify` | 哈希链完整性（监管） |
| GET | `/api/directory` | 机构/人员名录（协调/监管） |

事件投递示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/events \
  -H "X-Staff-Token: tk_coord" -H "Content-Type: application/json" -d '{
    "type": "slot.reschedule_requested",
    "external_id": "SMS-7782",          # 外部系统事件编号（渠道侧唯一即可）
    "source": "sms",
    "case_id": "CASE_00003",
    "payload": {
      "reason": "受者层流病房床位被占用",
      "requested_by": "recipient",
      "new_start_local": "2026-09-27T09:00",   # 采集医院当地墙上时间
      "new_end_local":   "2026-09-27T14:00"
    }}'
```

支持的 `type`：

```
match.success  screening.result  consent.granted  consent.withdrawn
slot.proposed  slot.confirmed  slot.reschedule_requested  slot.cancelled
match.alternative_activated
collection.started  collection.completed
chain.handover  temp.excursion_reported  temp.excursion_resolved
product.delivered  infusion.completed  case.cancelled
notify.delivery_receipt
```

自然幂等键示例（也可用 `idem` 显式覆盖）：
`match:{search}:{donor}`、`screen:{case}:{exam_ref}`、`grant:{case}:{doc}:{signed_at}`、
`handover:{product}:{from}>{to}:{at}`、`tempalert:{product}:{detected_at}`、
`tempres:{excursion}:{disposition}`。

## 代码结构

```
coord/
  clock.py       UTC 时钟、当地时区解释（ZoneInfo）、Window
  identity.py    机构（含时区）、人员、角色、令牌目录
  states.py      阶段、事件类型、同意/窗口/产品/温控/通知常量
  models.py      SearchRequest / Case / ConsentVersion / Slot / Product / Handover / TempExcursion / Notification
  audit.py       SHA-256 哈希链审计记录与自校验
  store.py       带锁内存存储、事件幂等索引
  routing.py     逐事件的精准受众（targets + suppressed 及原因）
  notifier.py    通知模板、发送尝试、重发、送达回执
  workflow.py    事件驱动状态机（校验/迁移/审计/通知在同一把锁内完成）
  projection.py  字段级 RBAC 脱敏与机构范围
  timeline.py    采集物/病例监管时间线组装（同意、交接人、异常处置、完整性）
  api.py         标准库 JSON HTTP 接口
  app.py         装配与演示种子目录
  testsupport.py 固定时钟与阶段驱动（测试/演示复用）
tests/           68 个单元与 HTTP 契约测试
service.py       运行入口（保留原 /health 契约）
demo.py          端到端演示
```

## 设计说明（与真实生产的差距）

- 存储为进程内内存 + 线程锁，便于零依赖联调；幂等索引、哈希链可平移到带唯一约束的事务数据库（事件为 append-only 事实流）。
- 通知通道为模拟发送（记录通道与尝试）；真实短信/网关接入点在 `Notifier._send_attempt`，送达回执经 `notify.delivery_receipt` 事件回流。
- 身份为演示用静态令牌；生产应替换为机构 SSO/网关鉴权，角色映射保持不变。
