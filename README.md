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

## 实物批次编组嵌入（`app/embed.py`）

在已完成的最优审计上，工程师可提交**稳定编组标识**与**至多 6 批不可拆分**
标准样品（每批：来源站、目标站、整数体积、不得经过的管路），确认这些实物
批次能否同时嵌入当次已冻结的管路流量。

- **只接受 `optimal` 来源**：服务从审计原记录冻结站点顺序、稳定管路顺序与
  每条已求得流量（即各管可用容量）；`infeasible`/`invalid` 来源或不存在
  的审计分别返回 400/404。
- **完整整数容量分配**：每批枚举其全部可用简单路径（避开禁行管路、且冻结
  流量足以承载），按提交顺序逐批做带回溯的完整搜索——容量判断仅用于剪枝，
  **不以逐批最短路、贪心或有限尝试替代**。批次按提交序、路径按
  （站点序列, 管路标识） 字典序枚举，首个完整分配即**规范解**，重算必然一致。
- **成功**（`status: "embedded"`）：按批次标识给出规范路径（管路序列与途经
  站点）、逐管占用 `used` 与剩余通量 `remaining = flow − used`；任意管路上
  的批次总量不超过原流量。
- **失败**（`status: "cannot_embed"`）：稳定列出**最先耗尽搜索层**
  （使前缀不可行的第一批）及其后的**未安置批次**、规范前缀安置下**已占满
  管路**与按冻结管路顺序的**逐管剩余容量**。
- **编组幂等**：同编组标识 + 同来源同批次重传回放原结论（`replayed: true`）；
  改换来源或批次返回 409。编组记录落盘 `/data/groups.json`，重启后仍可
  回放与查询（`GET /api/groups/<id>`），页面提交、刷新、重开后均可核对
  同一结论。

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
2. 在镜像内运行求解器、编组嵌入与 API 单元测试（含 60 组随机网络对暴力
   枚举的最优性对照、回溯非贪心、最先耗尽层证据、编组幂等存储）；
3. 对运行中的 web 做本题 API 冒烟（健康检查、最优解与约化成本证据、审计
   重放/冲突 409、不可行证据、非法输入定位、**一组可装入的编组嵌入**、
   **管路竞争失败的最先耗尽层证据**、**原审计与记录读取回归**）；
4. 全部通过则 `exit 0` 并退出，任何失败为非零退出码（`restart: "no"`，不常驻）。

## 本地开发（仅需 Python 3.11 标准库，无第三方依赖）

```bash
cd app
python3 -m unittest discover -s tests -v      # 44 项测试
PORT=8080 AUDIT_DB=/tmp/audit.json GROUP_DB=/tmp/groups.json python3 server.py
python3 tests/smoke.py http://localhost:8080  # API 冒烟
```

## API 摘要

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 |
| `GET /` | 录入与复算页面 |
| `POST /api/solve` | 提交 `{stations:[{id,balance}], pipes:[{id,from,to,lo,hi,cost}]}` |
| `GET /api/records/<audit_id>` | 取回审计原记录 |
| `POST /api/embed` | 提交 `{audit_id, group_id, batches:[{id,source,target,volume,forbid?}]}`（也可用头 `X-Group-Id`） |
| `GET /api/groups/<group_id>` | 取回编组原记录（冻结网络、批次与结论） |

`balance>0` 为净供给、`<0` 为净需求（也支持 `supply`/`demand` 记法）。
成功返回 `status:"optimal"`、`total_cost`、`flows`、`potentials`、`edges[].residual`
（约化成本证据）与 `node_check`；无可行流返回 `status:"infeasible"`、
`unmet_demand`、`stranded_supply`、`reachable_from_source`；非法输入返回
400 与 `loc` 字段路径。

编组嵌入成功返回 `status:"embedded"`、`batches[].path`（规范路径）、
`pipe_usage[]`（逐管 `flow/used/remaining`）；无法同时装入返回
`status:"cannot_embed"`、`first_exhausted_layer`、`unplaced_batches`、
`placed_prefix`、`saturated_pipes`、`remaining_capacity`；来源非 optimal
返回 400，来源不存在返回 404，同编组标识改换来源或批次返回 409。
