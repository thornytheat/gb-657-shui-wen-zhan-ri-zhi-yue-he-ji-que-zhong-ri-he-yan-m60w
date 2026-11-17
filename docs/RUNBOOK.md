# 运行手册 hydrocalendar

## 1 启动

```bash
cd projects/gb-657
docker compose up -d --build
curl -sS http://localhost:18216/api/health
```

页面：<http://localhost:18216/>。首次打开没有批次，选样例点「开始处理」即可跑通。

容器内端口 8000，宿主 18216；SQLite 落在命名卷 `hydrocalendar-data` 的 `/data/hydrocalendar.sqlite`。

## 2 主流程

1. 总览页选 `readings_dirty.csv`（脏样例，13 类问题），点「开始处理」。
2. 阶段卡显示七段「进 / 出 / 隔离」；任一阶段失败可点该卡的「重试」。
3. 读数诊断页看逐行诊断与隔离区，点「隔离区 CSV」下载证据。
4. 区间还原页看翻转、换表末段、新表首读与标记，可点「重算区间」。
5. 月度分摊页看每月合计、账单与尾差，可点「重算分摊」。
6. 批次对账页看四账与运行清单；导出页下载五份文件。

命令行同一套接口：

```bash
curl -sS -X POST http://localhost:18216/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"sample":"readings_dirty.csv"}'
curl -sS http://localhost:18216/api/reconcile | python3 -m json.tool
curl -sS -o /tmp/monthly.csv http://localhost:18216/api/export/monthly.csv
```

## 3 常见操作

- **同输入重跑**：再点一次「开始处理」或 POST 同一 `sample`，命中批次键，返回同一 `batch_id`，行数与合计不变。
- **强制重算**：POST `/api/batches/{batch_id}/retry`，用同一个 `sample` 重跑并覆盖该批次的行。
- **换样例**：下拉框里其余样例分别是干净、GBK 中文表头、末行截断、空文件、仅表头、全空行、表头缺列。
- **重跑测试**：`docker compose exec -T app python -m pytest -q`。
- **清库**：`docker compose down -v` 删除命名卷；下次启动自动建表。

### 雨量补报（停报后补数）

逐日补报与跨日累计补报是两条独立证据，累计**不会**被摊成日值，逐日缺测照旧保留。

```bash
# 逐日补报：一天一条
curl -sS -X POST http://localhost:18216/api/hydro/supplements/daily \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","station":"0012","day":"2024-02-29","rainfall_mm":"12.3"}'

# 跨日累计补报：只给起止日与总量；跨月时补分月归属（必须加总等于总量）
curl -sS -X POST http://localhost:18216/api/hydro/supplements/cumulative \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"req-002","station":"0012","start_day":"2024-01-30",
       "end_day":"2024-02-02","total_mm":"30.0",
       "attribution":{"2024-01":"10.0","2024-02":"20.0"}}'

# 回读：两种依据分列的月量与每笔观测的判定
curl -sS 'http://localhost:18216/api/hydro/months?station=0012' | python3 -m json.tool
```

- `request_id` 是幂等键：超时重发同一请求不会新增观测；同号不同载荷返回 409 `E_HYDRO_REQUEST_REUSED`。
- `base_revision` 可选：先回读拿 `revision` 再带回，若期间有人改过该站决定，返回 409 `E_HYDRO_STALE_REVISION`，按最新视图重发。
- 月量 `state`：`pending`（跨月归属待定，量见 `pending_reported_mm`）、`conflict`（日值与累计不符/同版本冲突/部分重叠）、`partial`、`complete`。
- 补齐证据或核算中断后：`curl -sS -X POST http://localhost:18216/api/hydro/recompute -d '{}'`；服务启动时也会自动补算。

## 4 失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 422 `E_EMPTY_FILE` | 样例文件 0 字节 | 选非空样例 |
| 422 `E_HEADER_ONLY` | 只有表头 | 补数据行 |
| 422 `E_ALL_BLANK` | 数据行全空 | 检查导出是否只带格式 |
| 422 `E_HEADER_INVALID` | 缺 `meter_id/register/read_at/reading` | 按列别名表改名 |
| 422 `E_CONTRACT_INVALID` | `meters.csv` 重复表号、零倍率、换表目标缺失 | 修契约 |
| 400 `E_SAMPLE_UNKNOWN` | 样例名不在白名单 | 用页面下拉框里的名字 |
| 404 `E_BATCH_NOT_FOUND` | 还没有批次就导出 | 先跑一次处理 |
| 409 `E_HYDRO_STALE_REVISION` | 补报基于旧决定号 | 回读最新 `revision` 后重发 |
| 409 `E_HYDRO_REQUEST_REUSED` | `request_id` 被另一笔载荷占用 | 换新请求号或核对载荷 |
| 400 `E_HYDRO_ATTRIBUTION_INVALID` | 跨月累计未给全分月归属或加总不符 | 补齐 `attribution` 且加总等于 `total_mm` |
| 422 `E_HYDRO_RECOMPUTE_FAILED` | 观测已保存但重算失败 | 月量仍是原值；POST `/api/hydro/recompute` 或等启动自动补算 |
| 月量 `state=pending` | 跨月累计缺分月归属证据 | 按更高版本重报并带上 `attribution` |
| 四账「不平」 | 逻辑或数据被改 | 看差额字段与说明，逐账核对 |

日志：`docker compose logs -f app`（uvicorn 访问日志）。运行清单里的 `input_digest` 可用来确认两次运行读的是同一份输入。

## 5 复算

1. 从导出页取 `monthly.csv` 与 `manifest.json`；
2. `manifest.json` 的 `inputs[].sha256` 与样例文件比对，确认输入未变；
3. 逐月加总 `用量kWh` 应等于 `manifest.json` 的 `counters` 与 `accounts[1]`（区间账）两侧；
4. 账单 `金额元` 之和应等于 `accounts[3]` 两侧，分项之和等于账单额。

## 6 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HYDROCALENDAR_DB` | `/data/hydrocalendar.sqlite` | SQLite 路径 |
| `HYDROCALENDAR_SAMPLE_DIR` | `/app/sample_data` | 样例目录 |

规则版本写在 `app/config.py` 的 `RULE_VERSION`，进入运行清单；改规则必须同时加测试，否则同输入重跑会出现两个批次。
