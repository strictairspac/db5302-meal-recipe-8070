# 砚街食堂·团餐备料展开

MySQL 递归 CTE 展开多层配方，汇总共享原料并识别循环。面向演示数据和本地复现，场景、操作剧本和业务边界见 [BUSINESS.md](BUSINESS.md)。

## 业务模块

- 菜单批次
- 半成品配方
- 原料展算
- 采购备料单

## 本地运行

```sh
cp -n .env.example .env
docker compose up -d --build --wait
```

- 工作台 `http://127.0.0.1:18291`
- FastAPI 文档 `http://127.0.0.1:19291/docs`
- MySQL 8.0 端口 `127.0.0.1:17791`，账号 `app`，演示库 `workflow`
- `docker compose exec -T backend python -m scripts.seed_demo` 手动重跑演示数据
- `docker compose logs -f backend worker` 查看运行情况
- `docker compose down` 停容器并保留演示卷

### 演示账号

统一密码 `Demo-Walkthrough-2026`。

| 账号 | 角色 | 能执行的操作 |
|---|---|---|
| `operator` | 经办员 | 读取档案、创建实验输入、提交执行、导出结果 |
| `reviewer` | 复核员 | 读取、导出和签署结果 |
| `viewer` | 观察员 | 读取、导出已有结果 |
| `buyer` | 采购员 | 分批领取已复核的采购备料单并确认收货 |

`SEED_DEMO=true` 启动时生成账号与一个业务样例。工作台新建档案会预填样例。可编辑表单中的数值、文本和结构化 JSON 后提交。业务说明中的原始文件是上游示例，演示数据通过表单录入。角色枚举位于后端 `src/constants/workflow.py` 与前端 `src/constants/workflow.js`。执行状态枚举也位于这两个文件，使用于队列、列表和执行详情。证据校验码是报告 JSON 按 UTF-8、键排序且省略多余空格序列化后的 SHA-256。

## 工程结构

| 服务 | 实现 |
|---|---|
| frontend | Nginx 与原生 HTML、CSS、JavaScript |
| backend | Python 3.11、FastAPI、SQLAlchemy |
| worker | Python 持久队列执行与 Redis 唤醒信号 |
| db | MySQL 8.0，持久卷保存档案、执行、证据及审计 |
| redis | Redis 7，追加日志卷保存队列唤醒信号 |

编辑 `backend/src/services/domain.py` 可修改试算方法与业务表单说明。API 路由、数据定义和页面位于 `backend/src/routes/`、`backend/src/models/` 与 `frontend/src/pages/`。开发时更新源文件后重新构建对应服务。

`.env.example` 提供 `FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT`、`DB_PASSWORD`、`AUTH_KEY` 、`SEED_DEMO`、`CLAIM_LEASE_SECONDS` 与 `NETWORK_SUBNET`。修改 `NETWORK_SUBNET` 可避开本机现有容器网段。所有发布的端口绑定本机回环地址。接入独立部署前需配置自己的凭据并按业务范围做独立验收。

### 采购领单接口

已复核的证据按复核落账顺序进入发单队列，采购员通过接口分批领走备料单。

| 接口 | 作用 |
|---|---|
| `POST /claims` `{terminal, token?}` | 领取下一批；领到返回批次（含单号、顺序、内容与 `claim_token`），暂无待发返回 `204` |
| `POST /claims/{id}/ack` `{token}` | 确认收货，确认后才发下一批 |
| `GET /claims`、`GET /claims/{id}` | 领单历史与详情 |

并发语义：

- 复核通过时在 `review_ledger` 中按事务提交顺序分配递增序号；两人同时复核时序号决定发单先后，复核失败（409）的证据不进入队列。
- 同一时间只存在一个未确认批次。领单后未确认即断线，用同一 `token`（或同一 `terminal`）重试会拿回同一单号、顺序和内容。
- 等待确认期间新落账的复核排队，不出现在当前批次；当前批次确认后才组装下一批。
- 每批有租约 `CLAIM_LEASE_SECONDS`（默认 30 秒）。租约到期后可换台终端接着领原批，单号、顺序和内容不变但轮换 `claim_token`，旧终端的迟到确认返回 `409`。

[MIT License](LICENSE)
