# 低温光谱仪校准液路 · 带上下界的精确整数最小费用流

工程师在浏览器录入阀站（净供给/净需求）与有向管路（稳定标识、整数上下界
与单位成本），后端以**任意精度整数**先消去下界求可行流，再在完整残量网络中
消除全部负费用改进，返回各管流量、最小总成本，以及可逐边复算的最优性证据
（站点势 π 与正残量边约化成本）。

## 算法（`app/solver.py`，全程整数，无浮点、无贪心、不限轮次）

1. **校验**：站点 ≤ 12、管路 ≤ 36；全部字段必须是整数（拒绝浮点数/布尔/字符串）；
   `lo ≥ 0`、`hi ≥ lo`；总供给必须等于总需求。错误带字段路径定位
   （如 `pipes[3].hi`）。
2. **下界消去**：每条边先注入 `lo`，节点余额变为
   `b(v) = balance(v) + Σlo(进入 v) − Σlo(离开 v)`，边剩余容量 `hi−lo`。
3. **可行流（Dinic 最大流，整数）**：超源向 `b>0` 的点连容量 `b` 的边，
   `b<0` 的点向超汇连容量 `−b` 的边。超源出边未送满即无可行流，返回
   **未满足需求**（汇侧弧残余）、**滞留供给**（源侧弧残余）与残量网络中
   **超源可达站点集合**作为失败 cut 证据。
4. **最优性（负环消除）**：在完整残量网络（正向弧 `cost`、反向弧 `−cost`）上
   用 Bellman–Ford 找负费用环并沿环增广，循环至不存在负环。每次增广使整数
   总费用严格下降，故必然终止；不预设轮次上限。
5. **势与复算**：最后一轮 Bellman–Ford 的距离即为势 π，满足每条正残量边
   `c + π[u] − π[v] ≥ 0`（残量网络无负弧 ⇒ 当前流为最小费用流）。API 回传
   逐边流量/界/费用贡献、每条正残量弧的容量和约化成本、逐站流入/流出/净流出
   对照，供独立复算。

## 审计标识（`app/audit.py`）

- 请求带头 `X-Audit-Id: <标识>`（或在 JSON 中放 `audit_id`）。
- **同标识 + 同载荷**重传：不重新求解，直接返回首次原记录（`replayed: true`）。
- **同标识 + 改载荷**：HTTP 409 拒绝，并回显已存载荷指纹。
- 载荷指纹为规范化 JSON（排序键）的 SHA-256；记录落盘 `/data/audit.json`，
  服务/容器重启后仍可重放与查询（`GET /api/records/<id>`）。

## 冻结嵌入核验：不可拆分标准样品批次（`app/embed.py`）

工程师在一份**已完成的 optimal 审计详情**中，可另提交一个**稳定编组**：至多
**6 批不可拆分**的标准样品，每批指定来源站、目标站、正整数体积与不得经过的
管路（`avoid`），核验这些实物批次能否**同时**嵌入当次已冻结的管路流量。

- **只接受 optimal 来源**：`POST /api/embed` 必须带 `audit_id`（头 `X-Audit-Id`
  或 JSON 字段），服务从该审计原记录**冻结**其站点、稳定管路顺序与每条已求得
  流量（作为逐管容量）。来源不存在 → 404；非 optimal → 409。
- **稳定编组标识** `group_id`（头 `X-Group-Id` 或 JSON 字段）：
  - 同标识 + 同来源同批次重传 → 回放首次原编组结论（`replayed:true`），不重算；
  - 同标识 **改换来源或改动批次** → 409 拒绝，并回显已存来源与指纹；
  - 编组记录落盘 `/data/groups.json`，可 `GET /api/groups/<id>` 查询，
    刷新、重开页面或重启服务后核对到的都是同一结论。
- **求解是完整整数容量分配，非逐批最短路/贪心/有限尝试**：
  1. 为每批枚举（来源→目标、避开禁用管路的）**全部顶点简单路径**（单批超过
     20000 条则显式拒绝，不静默截断）；
  2. 按可选路径数升序分层，对各层路径的完整笛卡尔积做**穷尽回溯 + 后缀可行
     记忆**（以逐层剩余容量向量为键）；任一路径上整批体积不可分地逐管占用，
     任一管路批次总量超过其冻结流量即该组合非法。
- 成功 `status:"embedded"`：按批次标识给出**规范路径**（站点序、管路序，按
  长度+管路序号排序，结论稳定）、**逐管占用**（占用明细 `by`）与每管
  **剩余通量** `remaining = frozen_flow − used`。
- 失败 `status:"cannot_embed"`：稳定给出**最先耗尽的最深搜索层**起的未安置
  批次（及其候选简单路径数/快照下仍可容纳路径数）、**已占满管路**
  （占用 == 冻结流量）与全部管路的**剩余容量**快照。
- 普通原审计求解与读取接口（`/api/solve`、`/api/records/<id>`）保持不变。

页面第 6 节提供批次录入（来源/目标下拉随阀站表联动）、禁经管路、完整核验与
按编组标识查询；表单存浏览器 `localStorage`，刷新/重开后自动恢复并从服务端
重新核对同一编组结论。

## 运行（Docker Compose，需 Docker + Compose v2）

```bash
# 可选：自定义宿主机端口
export HOST_PORT=9090          # 默认 8080

docker compose up -d --build web     # 仅启动页面与 API
# 浏览器打开 http://localhost:9090
curl -s http://localhost:9090/health
```

### 一次性核验（verify 容器自行退出并以退出码报告）

```bash
./verify.sh                    # = docker compose up --build --abort-on-container-exit --exit-code-from verify
echo $?                        # 0 = 全部通过
```

`verify` 服务：

1. 等待 `web` 健康检查通过后启动；
2. 在镜像内运行求解器/API/嵌入核验单元测试（含 60 组随机网络对暴力枚举的
   最优性对照，以及 120 组随机网络对全路径笛卡尔积暴力枚举的嵌入完备性对照，
   另含“完整搜索 vs 朴素贪心会误判”的用例）；
3. 对运行中的 web 做本题 API 冒烟（健康检查、最优解与约化成本证据、审计重放/
   冲突 409、不可行证据、非法输入定位，以及冻结嵌入的可装入/管路竞争失败/
   编组重放与换来源换批次 409/原审计回归）；
4. 全部通过则 `exit 0` 并退出，任何失败为非零退出码（`restart: "no"`，不常驻）。

## 本地开发（仅需 Python 3.11 标准库，无第三方依赖）

```bash
cd app
python3 -m unittest discover -s tests -v      # 45 项测试
PORT=8080 AUDIT_DB=/tmp/audit.json GROUP_DB=/tmp/groups.json python3 server.py
python3 tests/smoke.py http://localhost:8080  # API 冒烟（含嵌入核验）
```

## API 摘要

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /` | 录入与复算页面 |
| `POST /api/solve` | 提交 `{stations:[{id,balance}], pipes:[{id,from,to,lo,hi,cost}]}` |
| `GET /api/records/<audit_id>` | 取回审计原记录 |
| `POST /api/embed` | 对已冻结 optimal 审计做不可拆分批次完整嵌入：`{audit_id, group_id, batches:[{id,from,to,volume,avoid}]}` |
| `GET /api/groups/<group_id>` | 取回编组核验原记录 |

`balance>0` 为净供给、`<0` 为净需求（也支持 `supply`/`demand` 记法）。
成功返回 `status:"optimal"`、`total_cost`、`flows`、`potentials`、`edges[].residual`
（约化成本证据）与 `node_check`；无可行流返回 `status:"infeasible"`、
`unmet_demand`、`stranded_supply`、`reachable_from_source`；非法输入返回
400 与 `loc` 字段路径。
