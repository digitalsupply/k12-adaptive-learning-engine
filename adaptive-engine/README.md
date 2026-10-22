# K12 自适应学习引擎（骨架）

基于 `k12-knowledge-points` 数据集的自适应学习引擎实现骨架：
**知识点先修图谱 + 掌握度建模（BKT/Elo）+ 错题归因 + 分阶段学习路径规划 + 复习调度**。

```
adaptive-engine/
├── ale/                  引擎核心包
│   ├── dataset.py        数据集懒加载（按学段×科目）+ 全局 ID 索引 + 派生索引缓存
│   ├── graph.py          先修 DAG：拓扑序、祖先回溯、分层、枢纽识别、环检测
│   ├── mastery.py        MasteryModel 接口 + BKT / Elo 两个实现（可插拔）
│   ├── diagnose.py       错题归因：沿先修链回溯 + 贪心集合覆盖找根因
│   ├── planner.py        路径规划：先修闭包 → 剔除已掌握 → 分阶段输出
│   ├── review.py         SM-2 变体复习调度
│   ├── state.py          学生状态（作答历史/掌握度/复习队列）JSON 持久化（原子写 + 文件锁）
│   ├── engine.py         AdaptiveEngine 门面（CLI 与 API 共用）
│   ├── figures.py        交互图形注册表（知识点 UUID → 图形）
│   └── config.py         全部可调参数（阈值/衰减/回溯深度…）
├── cli.py                命令行入口
├── server.py             FastAPI 服务 + 托管前端 + 托管图形静态资源
├── web/                  单页前端（SVG 图谱交互，无框架依赖）
├── figures/              交互图形（离线构建的静态 HTML/SVG）
│   ├── registry.json     图形注册表：知识点 UUID → 图形
│   └── ohm-lab/          欧姆定律实验台（model / geom / live / build）
├── tests/                60 个测试：合成图单测 + 真实数据集端到端 + 图形注册表 + 回归
├── pyproject.toml        pytest 配置（pythonpath，任意目录可跑）
└── data/                 运行时目录（students/ 学生档案、cache/ 派生索引，均已 gitignore）
```

## 快速开始

```bash
# 依赖（仓库根目录已建好 .venv，或自行 pip install -r requirements.txt）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 跑测试（含真实数据集冒烟）；pyproject.toml 里配了 pythonpath，任意目录可跑
.venv/bin/python -m pytest tests/ -q

# 预热派生索引（可选：首次请求会自动建，之后走缓存）
.venv/bin/python cli.py prebuild-index

# CLI 演示
.venv/bin/python cli.py search 待定系数法 --band 初中
.venv/bin/python cli.py new-student 小明 --model bkt
.venv/bin/python cli.py simulate <SID> --band 初中 --subject 数学 --n 30
.venv/bin/python cli.py diagnose <SID>          # 错题归因
.venv/bin/python cli.py plan <SID> <goal_kp_id> # 分阶段学习路径
.venv/bin/python cli.py next <SID> --goal <goal_kp_id>

# Web 服务（浏览器打开 http://localhost:8000）
.venv/bin/python server.py
```

数据集路径默认取仓库 `../split`，可用环境变量 `K12_DATA_DIR` 覆盖。

> **置信度过滤（重要）**：默认 `min_confidence=0` 全量加载。音乐、体育等科目的
> 条目大部分来自「目录标题抽取」，`confidence` 只有 0.2~0.4；若按 0.6 硬过滤，
> 整个科目会静默消失（接口返回空图）。低置信条目在 UI 上以虚线弱化显示，
> 详情页会提示「仅按目录标题抽取」。需要严格模式时设 `K12_MIN_CONFIDENCE=0.6`。

## 核心设计

### 掌握度：BKT 与 Elo 可插拔

统一 `MasteryModel` 接口（`prior / update / decay / p`），学生档案里记录用哪个模型，
`create_model()` 工厂注册，新增模型（如 DKT 神经网络）只需实现同一接口。

- **BKT**：四参数贝叶斯知识追踪，知识点难度调制先验/学习率/失误率/蒙对率。
  *对经典公式的修正：学习转移只在答对后应用*——原版对答错也套转移，
  低先验时会出现"答错反而涨掌握度"的反直觉结果（测试 `test_bkt_update_direction` 守住这一点）。
- **Elo**：知识点难度映射为"题目分"，学生对每个知识点维护能力分，掌握概率即胜率；
  K 值随练习次数收敛。
  *状态语义修正*：能力分与胜率分开存放（`MasteryState.ability`）。旧实现把胜率
  写进 `p_mastery`，下一次 `update()` 又把它当能力分乘一遍 span，能力估计系统性
  偏低——"连对 10 次"的偏差达 8.6 个百分点。修复后 Elo 的学习轨迹与"能力分独立
  存放"的标准写法逐位一致（测试 `test_elo_trajectory_matches_reference_implementation`）。
- 两者都实现了指数遗忘（`decay`），参数在 `config.py`。
- **阈值按模型分派**：BKT 用 `mastered_threshold`（0.85），Elo 用
  `elo_mastered_threshold`（0.80）。走 `config.mastered_for(model.name)` 统一取，
  接口层、规划器、归因器用的是同一个口径——此前 `elo_mastered_threshold` 是死配置，
  所有判断都落到了 0.85 上。

### 错题归因：证据传播 + 贪心集合覆盖

1. 症状 = 做错的知识点集；
2. 沿 `prerequisite` 边向上 BFS，祖先获得证据权重 `(1 - p(祖先)) × 0.6^(距离)`——
   越弱、离症状越近，嫌疑越大；
3. 贪心选覆盖未解释症状最多的祖先为根因，附一条「根因 ← … ← 症状」解释链，
   直到症状全部覆盖或达到根因上限。

### 只用硬前置建 DAG

数据集有 `prerequisite / leads-to / related` 三类边。图的拓扑、分层、祖先回溯
**只认 `prerequisite`**，`leads-to` 以虚线留在可视化里，`related` 不参与推理——
否则弱边会污染先修深度（曾导致"最大深度 17 但最长链只有 1"的不一致）。

**环的处理**：LLM 抽取的先修边不保证无环（实测 39 个科目中 17 个存在环，
初中物理有一个 35 节点的大环）。所有图算法先在 SCC（强连通分量）上缩点：
拓扑序、深度、规划阶段都在缩点后的 DAG 上计算，环内节点共享同一深度与阶段
（它们互相依赖，本来就无法排序）。`graph.cycles()` 只报告真正的环。

### 路径规划

目标 → 先修闭包 → 按掌握度阈值剔除已掌握 → 拓扑排序 → 按"弱节点子图内最长前置深度"
分阶段。超长路径按「离目标近者优先」截断（保底包含目标），截断后重算阶段。
估时为骨架级启发式（8 + 难度×14 分钟，程序性知识 +4）。

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/groups` | 学段×科目清单 |
| GET | `/api/config` | 前端阈值下发（掌握度阈值、置信度提示阈值…） |
| GET | `/api/search?q=&band=&subject=` | 知识点搜索 |
| GET | `/api/kp/{id}?student=` | 详情（目标/误区/先修链/掌握度） |
| GET | `/api/graph/{band}/{subject}?student=&depth=&root=&spine=&max_nodes=` | 图谱可视化数据（支持按范围收窄，见下） |
| POST | `/api/students` | 建学生 `{name, model}` |
| POST | `/api/students/{sid}/attempt` | 记录作答 `{kp_id, correct}`（答错自动归因） |
| POST | `/api/students/{sid}/attempts` | 批量记录作答 `{attempts:[{kp_id, correct}]}`（整批只写一次档案） |
| POST | `/api/students/{sid}/simulate` | 模拟练习 `{kp_ids, ability, noise, seed}` |
| POST | `/api/students/{sid}/diagnose` | 归因 `{kp_ids}` |
| GET | `/api/students/{sid}/diagnose/recent` | 最近错题自动归因 |
| POST | `/api/students/{sid}/plan` | 学习路径 `{goal_id}` |
| GET | `/api/students/{sid}/recommend?goal=` | 推荐下一步 |
| GET | `/api/students/{sid}/reviews` | 到期复习 |
| GET | `/api/figures` | 已注册的交互图形清单 |
| GET | `/figures/**` | 图形静态资源（构建产物） |
| GET | `/api/kp/{id}` 里的 `figure` 字段 | 该知识点挂着的交互图形（没有则无此字段） |

### 图谱范围收窄

大科目整图太大（小学语文 4851 个知识点 → 1.4 MB 响应 + 上万个 SVG 元素），
`/api/graph` 支持四个收窄参数，响应里的 `truncated / total_nodes / total_edges`
告诉前端是否被裁剪：

| 参数 | 含义 | 效果（小学语文）|
|---|---|---|
| `spine=true` | 只保留参与先修关系的节点 | 4851 → 2529 节点 |
| `depth=N` | 只保留深度 ≤ N | 按层级过滤 |
| `root=<kp_id>` | 只保留该节点所在连通分量 | 1683 → 14 节点（初中数学）|
| `max_nodes=N` | 节点数上限，按连通分量从大到小装 | 800 节点 / 648 条边 |

前端工具栏的「范围」下拉对应这些模式；节点超过 1200 个时只给有信息量的节点
（已作答 / 选中 / 根因 / 路径内）画文字标签，避免 DOM 爆炸。

## 性能与持久化

- **派生索引缓存**：`kp_id → (学段, 科目)` 与 `名称/别名 → kp_id` 两个索引会落盘到
  `data/cache/`，以 split 文件的 `(路径, 大小, mtime)` 指纹判断新鲜度，数据集变了
  自动重建。冷启动 `locate()` 从 0.39 s 降到 0.015 s，搜索从 0.41 s / 157 MB 降到
  0.17 s / 106 MB。`cli.py prebuild-index` 可手动预热。
- **学生档案原子写**：先写同目录临时文件再 `os.replace` 覆盖，中途崩溃不会留下
  半截 JSON；失败时清掉临时文件，原档案毫发无损。
- **文件锁**：`StudentStore.mutate()` 在 `fcntl` 独占锁内完成「读-改-写」，
  多 worker 并发作答不会互相覆盖（此前是裸的读-改-写，后写的那次会丢）。
- **损坏档案降级**：`get()` 遇到坏 JSON 抛 `KeyError`（API 回 404 + 提示），
  列表接口跳过坏档案，不会整个挂掉。


## 交互图形：欧姆定律实验台

`figures/ohm-lab/` 是一个**用物体解释概念**的交互图形：6 V 电源、0–20 Ω 滑线变阻器、
两个 10 Ω 电阻、电流表、电压表和一台 I–U 记录仪。拖动滑片 → 变阻器接入电阻变大 →
电流表、电压表指针回落 → 记录纸上的笔沿直线移动；换接法（单个 / 串联 / 并联），
笔臂倾角就是 1/R，三条参考线分别对应 5 Ω、10 Ω、20 Ω。

### 单一真源

| 文件 | 作用 |
|---|---|
| `model.mjs` | 纯电学模型 `solve()`：滑片位置 + 接法 + 开关 → U / I / R，指针角度、图线坐标、读数格式全部由它派生 |
| `geom.mjs` | 等距投影几何（与 Anatomy kit 逐字等价，构建时抽样对比，不一致直接构建失败） |
| `live.js` | 浏览器运行时：拖拽、接法切换、指针弹簧缓动、笔臂/记录线、巡演、键盘、reduced-motion |
| `build.mjs` | 离线构建：生成 `embed.html`（浅色，嵌前端用）、`embed-dark.html`、`ohm-lab-light.html`、`ohm-lab.html`（独立页） |

要点：

- **运行时不含任何 LLM 调用**，也不读网络：读数与图线是同一个纯模型算出来的，
  图形本身就是解释，不是装饰动画。
- 图内自评（「我看懂了这条线」/「还不太明白」）走**形成性评估**通道：
  答对加掌握度、答错触发根因归因，与主页面 ✓/✗ 是同一个作答接口。
- 暗色版与浅色版共用 `model.mjs` / `live.js`，只换主题 token；两版产物都在仓库里。

### 构建

```bash
cd adaptive-engine
node figures/ohm-lab/build.mjs        # 需要本机 Anatomy skill（ANATOMY_HOME 可覆盖）
node --test figures/ohm-lab/model.test.mjs   # 10 个模型断言
```

构建产物是纯静态 HTML（浅色嵌入页约 280 kB），直接被 `/figures` 托管。
构建产物可复现：同样输入重跑 `build.mjs`，产物逐字节一致。

### 注册表

`figures/registry.json` 决定「哪些知识点挂图形」：

```json
{
  "version": 1,
  "figures": {
    "ohm-lab": {
      "id": "ohm-lab",
      "title": "欧姆定律实验台",
      "subtitle": "串联滑线变阻器 · 三种接法 · I–U 记录纸",
      "embed_url": "/figures/ohm-lab/embed.html",
      "embed_dark_url": "/figures/ohm-lab/embed-dark.html",
      "standalone_url": "/figures/ohm-lab/ohm-lab-light.html",
      "standalone_dark_url": "/figures/ohm-lab/ohm-lab.html"
    }
  },
  "by_kp": { "<知识点 UUID>": "ohm-lab" }
}
```

`ale/figures.py` 的 `FigureRegistry` 读它；文件缺失或损坏时返回空（详情页不出现
图形入口），**不影响主引擎**。目前映射了 9 个初中物理知识点
（欧姆定律表达式与单位、I–U 图像分析、控制变量法实验设计、伏安法测电阻原理、
串联电流处处相等、伏安法接线规范与常见错误等）。

### 前端接入与 postMessage 协议

知识点详情页出现「打开实验台 →」入口，点开是全屏模态里的 iframe（~860 px 宽，
不用侧栏 380 px 去压一张 640 px 的图）。父子页面用 `postMessage` 通信，
只认同源：

| 方向 | 消息 | 说明 |
|---|---|---|
| iframe → 父页面 | `{type:"ready"}` | 图形就绪，父页面回送掌握度 |
| iframe → 父页面 | `{type:"height", height}` | 内容高度，父页面据此调 iframe 高度 |
| iframe → 父页面 | `{type:"attempt", kp_id, correct, reading}` | 图内自评，`reading` 带 U / I / R / 接法 / 滑片位置 |
| 父页面 → iframe | `{type:"mastery", kp_id, p}` | 当前掌握度，图形据此调整提示（低掌握先动滑片，高掌握先预测再验证） |

父页面收到 `attempt` 后：校验 `kp_id` 与当前选中一致 → `POST /api/students/{sid}/attempt`
→ 掌握度条与图谱重新着色 → 把新的 `p` 回送 iframe。图内作答与主页面 ✓/✗ 共用同一接口，
所以「在实验台里点懂了」和「在详情页点懂了」进入同一条学习轨迹。

## 已知边界（骨架阶段）

- 学生状态存 JSON 文件（已加原子写 + 文件锁），适合单机演示；
  生产应换 SQLite/Postgres——文件锁只能防丢更新，防不住跨机部署。
- 交互图形目前只有「欧姆定律实验台」一个，注册表可继续扩展；
  图形产物需要手工执行 `node figures/ohm-lab/build.mjs` 重建（不进 CI）。
- 关系不跨学科不跨学段（数据集本身如此），跨科归因暂不支持。
- 高中知识点 `grades` 常为空，展示层回退到教材模块名。
- 估时、推荐权重均为可解释的启发式，未用真实作答数据校准——这正是骨架留白：
  接入真实题库后，用 `assessability` 字段筛可出题的知识点，把 `misconceptions`
  变成干扰项，即可闭环成完整的自适应练习系统。
- Elo 的能力分与胜率虽然已分开存放，但能力分仍以 `p_mastery` 的比例做持久化
  兼容层；接入真实数据后建议给 Elo 单独的 state schema。
- CORS 默认放开（本地演示用）；部署到公网请设 `K12_CORS_ORIGINS`
  （逗号分隔的来源列表）。

## License

数据集 © 2026 猫叔，[PolyForm Noncommercial 1.0.0](../LICENSE)——
本引擎代码同样仅限非商业用途。
