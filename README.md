# 影响解释包平台（Impact Explanation Packages）

上游一批明细修订到达后，自动重算受影响的报表聚合，并生成**可下载、可独立复核**的影响解释包。
纯 Python 3 标准库实现（无任何第三方依赖），虚构数据加工与真实文件生成全部从零计算。

## 架构：三个独立运行的服务

```
                 ┌────────────────────────────┐
                 │  SQLite (WAL) + packages/  │  共享存储
                 └───────▲───────▲────────▲───┘
                         │       │        │
   修订批 ──► ┌──────────┴─┐  ┌──┴───────┐  ┌─┴───────────┐
   (HTTP)     │  ingestd   │  │recomputed│  │ packagesvc  │ ◄── 网页 UI (:8003)
              │ 修订接收    │  │ 依赖重算  │  │ 解释包服务   │
              │ :8001      │  │ :8002    │  │ :8003       │
              └────────────┘  └──────────┘  └─────────────┘
```

- **ingestd（修订接收）**：唯一写入 `detail_versions` 的服务。接收一批修订（更新/删除/新增），
  在**同一个事务**里完成：钉住输入边界 → 写入新版本明细 → 登记批次 → 派发重算任务。
- **recomputed（依赖重算）**：轮询重算队列，按批次**固定的输入边界**重放基线、应用本批修订，
  算出受影响聚合与差额，逐报表提交断点（崩溃可续算，幂等可重算）。
- **packagesvc（解释包服务）**：托管网页 UI、代理提交、提供差额钻取数据，
  后台构建线程生成解释包，支持**取消 / 恢复 / 下载**。

## 关键机制

### 1. 同一修订、同一输入边界（不多头挑最新数据）

`detail_versions` 是只增不删的版本历史，`meta.version` 是全局单调版本计数器。
ingestd 接收批次时取 `boundary = 当前计数器值` 作为该批的**输入边界**，本批变更占用
`boundary+1 … boundary+n`。之后任意时刻重算该批的第 i 张报表，输入都严格是：

```
baseline = 所有明细在 version ≤ boundary 时的状态
new      = baseline 应用且仅应用本批修订
```

后到的批次（版本更高）永远不会渗入本批数字——测试 `test_boundary_isolation`
验证了"B2 先于 B1 重算到达，B1 的结果仍不含 B2 的变更"。

### 2. 存储权衡：增量贡献 + 有界重放（而非全量中间贡献）

| 方案 | 代价 | 本系统的选择 |
|---|---|---|
| 存全部中间贡献（每个明细×每个聚合） | 存储随数据量×报表数膨胀，每次更新都要维护 | ✗ |
| 完全按需重放 | 无额外存储，但每次都要全量历史 | 部分采用 |
| **本批增量贡献 + 边界切片重放** | 存储 ∝ 修订大小；重放 ∝ 边界切片 | ✓ |

- 重算时只落库**受影响聚合行**（old/new/Δ）和**被修订明细的贡献**（`impact_rows`），
  加上报表级合计（`report_diffs`）。
- 构建包时按边界重放出**证据切片**（边界时刻全量明细快照 + 本批修订清单）嵌入包内。
- 只保留值真正变化的聚合行：纯维度移动（如只改 region）不会在产品报表产生零差额行。

### 3. 包下载后可独立复核，来源再变也不影响

```
impact_package_<批次>__g<代际>.zip
├── manifest.json                   # 清单：批次、边界、各文件 sha256、报表合计
├── evidence/baseline_details.jsonl # 证据：边界时刻全部明细（输入切片）
├── evidence/revisions.jsonl        # 证据：本批修订（每条 old/new 状态）
├── impact/<report>.json            # 每张报表：受影响聚合 old/new/Δ + 逐明细贡献
├── verify.py                       # 独立校验器（纯标准库，随包分发）
└── README.txt
```

- `python3 verify.py` 在解包目录离线运行：校验 sha256 → 从证据**重算全部聚合** →
  与包内影响数据逐项比对（受影响键集合、每行 old/new/Δ、逐明细贡献、报表合计）。
  不访问网络、不访问源系统、不信任生产方。
- 包内容完全由固定边界推导，发布后字节不再变化；来源继续更新只产生新批次新包
  （demo 第 8 步、`test_package_immutable_under_source_updates` 验证字节级不变）。
- **构建前自检**：每次构建先在暂存目录运行包内校验器，PASS 才允许发布——
  开发期间它真实拦截过一次"受影响集合与重算差额集合不一致"的缺陷。

### 4. 取消 / 恢复 / 旧任务不得覆盖新包

- 构建按报表分步推进，每完成一张报表就把规范化的文件负载写入 `build_checkpoints`；
  取消后暂存目录清理、断点保留，**恢复**（新代际）直接复用断点负载跳过已完成步骤。
- 每次构建请求生成单调递增的**代际号 generation**；发布是原子 CAS：
  `UPDATE packages … WHERE batch_id=? AND generation=<我的代际>`。
  旧任务即使晚结束，CAS 失败 → 标记 `superseded` 并删除自己的产物，
  永远替换不了更新的包（`test_stale_build_cannot_replace_newer_package` 验证）。
- 取消/失败对包状态的回写同样带代际条件，旧代际无法篡改新代际的状态。

## 运行

```bash
# 一键端到端演示（种子数据 → 三服务 → 提交修订 → 重算 → 构建 → 下载 → 独立复核 → 来源再更新）
python3 scripts/demo.py          # 然后打开 http://127.0.0.1:8003 ，Ctrl+C 结束

# 或分步运行
python3 scripts/seed.py --reset            # 生成 600 条虚构明细（确定性，种子 42）
python3 -m impact.ingestd     &            # :8001 修订接收
python3 -m impact.recomputed  &            # :8002 依赖重算
python3 -m impact.packagesvc  &            # :8003 解释包 + 网页
python3 scripts/source_simulator.py        # 可选：模拟来源持续更新

python3 -m unittest tests.test_system -v   # 12 个系统测试
```

网页支持：提交修订批（JSON，可一键填示例）→ 查看各报表受影响聚合 →
**展开差额**到逐明细贡献 → 构建 / **取消** / **恢复** / 下载解释包。

## API 一览

| 方法 | 路径 | 服务 | 说明 |
|---|---|---|---|
| POST | `/api/batches` | ingestd | 接收修订批，返回 `{batch_id, boundary_version}` |
| GET  | `/api/batches[/<id>]` | 两者 | 批次列表 / 详情（含变更内容） |
| GET  | `/api/details` | ingestd | 当前明细状态（供模拟器/UI） |
| POST | `/api/submit` | packagesvc | 代理转发到 ingestd |
| GET  | `/api/batches/<id>/impact` | packagesvc | 影响钻取数据（报表→聚合→明细贡献） |
| POST | `/api/packages/<id>/build` | packagesvc | 构建（幂等；取消后再调即恢复，新代际） |
| POST | `/api/packages/<id>/cancel` | packagesvc | 取消当前构建 |
| GET  | `/api/packages/<id>/status` | packagesvc | 包状态、代际、进度、断点数 |
| GET  | `/api/packages/<id>/download` | packagesvc | 下载已发布的 zip（带 `X-Sha256`） |

修订批格式：

```json
{"changes": [
  {"detail_id": "D000123", "amount": 155000, "qty": 40},
  {"detail_id": "D000240", "region": "East"},
  {"detail_id": "D000300", "delete": true},
  {"detail_id": "DX0001", "region": "North", "product": "P-03",
   "channel": "online", "amount": 88000, "qty": 22}
]}
```

金额单位为分（整数），数量为单位件数；无操作变更、重复明细、缺字段新增等会被 422 拒绝。

## 配置（环境变量）

`IMPACT_HOME`（数据目录，默认 `./data`）、`IMPACT_INGEST_PORT`/`IMPACT_WORKER_PORT`/`IMPACT_PACKAGE_PORT`
（默认 8001/8002/8003）、`IMPACT_INGEST_BASE`、`IMPACT_POLL_INTERVAL`、
`IMPACT_BUILD_STEP_DELAY`（人为放慢构建步，便于观察取消/竞态）、`IMPACT_BUILD_WORKERS`。

## 已知边界与扩展点

- 聚合算子目前为 SUM；新增报表只需在 `impact/reports.py` 注册维度组合，
  校验器与 UI 会自动跟随（manifest 内嵌报表定义）。
- 证据切片当前为边界全量明细（虚构数据规模下很小）；数据量大时可在
  `core.evidence_for_batch` 切换为"仅受影响键"切片，代价是包内无法复核报表总计。
- 版本历史是重放一致性的前提，需保留；如需裁剪，应在裁剪前把受影响批次的证据落包。
- 重算失败批次留在 `failed` 状态，重算幂等，可直接重投任务。
