# 技术路线（2026-10-13 更新）

本文档记录团队已经确定的技术路线，产品层面的定义见 `PRODUCT.md`。后续开发都沿用这条路线；要改变方向，先更新本文档。

标记说明：✅ 已确认；⚠️ 待查证或待决定。

---

## 1. 比赛背景

- 比赛：**AMD Developer Hackathon: ACT III**（lablab.ai），线上开发期为 10 月 12–18 日。✅ 已报名。
- ⚠️ 提交截止：据查是 **10 月 18 日 15:00 UTC（加州时间早上 8 点）**，以活动页面的倒计时为准。
- 赛道：**只报 Health and Wellbeing**。评审关注：明确的患者需求、负责任地处理健康信息、实用易懂的结果、可衡量的改善。
- 评分维度：Application of Technology、Presentation、Business Value、Originality。
- 硬性要求：项目中**相当一部分工作负载**必须跑在 AMD 上，并且 AMD 的部分必须出现在给评委看的产品里。
- 提交内容：公开的 GitHub 仓库（`amd-health-agent`）、能跑起来的 README、容器化的应用、可访问的演示网址、演示视频、PPT、封面图。API key 只能放在环境变量里。

## 2. 核心原则

1. **以产品为中心。** 用户感受到的是对话、问卷、推荐结果；数据库是后台的基础设施。介绍和开发的优先级都从用户体验出发。
2. **Python 负责客观的数字和排序；LLM 负责判断和回复。** 费用计算、各项指标的排序、硬性事实的核对都由 Python 完成；挑哪些问题、哪个计划最适合用户、怎么回复，由 LLM 根据用户的权重决定。数字永远不由模型生成。
3. **靠数据库，不靠模型记忆。** 保险信息每年都变，又要求数字精确，所以知识放在可追溯的数据库里，不训练模型去“记住”它。
4. **为能力较弱的模型设计。** 运行时模型只做选择题和填空题：从问题库里选题、从候选里挑选、填参数、写回复。难的工作交给 Python 和离线流程。
5. **不懂就问，由程序强制执行**（`workflow/guards.py`）。
6. **每个值都有出处。** 保单页码、原文摘录、价格来源。

## 3. 总体架构

```
              离线：建数据库
┌──────────────────────────────────────────────────────────┐
│ 保单 PDF（人工收集，可借助 Tavily 等工具）                  │
│   → AMD 模型提取 → 程序验证 → 人工复核 → ① 保单信息数据库    │
│ 保险公司的 MRF 价格文件                                     │
│   → Python 流式解析 → ② allowed amount 数据库（SQLite）     │
└──────────────────────────────────────────────────────────┘
              在线：用户提问时
┌──────────────────────────────────────────────────────────┐
│ 对话窗 → LLM 从问题库挑题 → 用户填问卷                       │
│ → 查两个数据库 → Python 计算和排序 → LLM 按用户权重判断、回复 │
│ → 对话中展示结果 / dashboard                                │
│ 用户问到数据库里没有的保险公司 → 搜索 worker（Playwright）   │
└──────────────────────────────────────────────────────────┘
```

## 4. 两个数据库

### ① 保单信息数据库
- 内容：除了 allowed amount 以外，保单 PDF 里的**所有信息**：保费、deductible、copay、coinsurance、OOP max、网络层级、转诊 / 预授权、限制条件。
- 格式：**规则 JSON v2**，分计划层和规则层两层，见 issue #1。网络层级和额度由每个计划自己声明，可以表示 UC SHIP 那样的 4 层网络（Ashe 学生健康中心、UC Family、Anthem PPO、网络外）。
- 存储：~~每个计划存一个文件 `policies/<plan_id>.json`~~ ✅ 2026-10-17 改定：存进一个只读 SQLite 文件 `data/plans/policy.sqlite`（`plans` / `benefits` / `plan_networks` 三张表，见 `db/README.md`），跟着代码一起部署。每个计划的 SBC 抽成两张 CSV，放在 `data/plans/extracted/<plan_id>/`；`scripts/policy/build_seed.py` 负责校验、分类，并重新生成这个 SQLite 文件。一行只记一个价格（计划 × SBC 段落 × 服务 × 组成部分 × 网络 × 档位）。团队交来的抽取 CSV 一律视为已核对，在 `data/plans/sources.csv` 的 `verified_by` 里记录。
- 收录范围：**UC SHIP**，加上**美国最常见的几家大型商业保险**（由团队挑选）。
- 来源：团队从官方渠道收集保单 PDF（Schedule of Benefits / SBC、保费表），离线收集时可以借助 Tavily 等工具；用户也可以上传保单。**PDF 内容的提取必须由 AMD 模型完成**，不能由其他 LLM 或编码助手生成数据。
- 资料清单（放进 git）：`plan_id`、计划名称、保险公司、保单年度、来源网址、下载日期、文件哈希、收集人。PDF 文件本身不放进 git。
- 不计算保费补贴。

### ② Allowed amount 数据库
- 来源：保险公司公开的 **In-Network Rate MRF**（Transparency in Coverage），用流式 JSON 解析，存进 SQLite。✅ 团队已经成功处理过 MRF，**演示用到的价格保证在库里**。
- 价格状态：`verified_rate` / `estimated_rate` / `rate_unavailable`。查不到具体医生的价格时，给出地区的低 / 中 / 高区间。
- 注意：Anthem 的 MRF 目录文件压缩后约 10GB，必须流式解析；协商价不保证等于最终的 allowed amount。
- 这一步不需要 LLM，主要是 CPU 的工作。

### 两个数据库的对接
- **计划对照**：我们的 `plan_id` → MRF 里的计划编号。✅ 2026-10-17：`plan_networks` 表（`data/plans/plan_networks.csv`），按网内档位对应到 `mrf_sources.plan_key` + `network`，每行附依据。团队同意由编码助手根据保单原文和 `data/plan_profiles.v1.json` 建立。
- **服务对照**：用户的通俗说法 → `service_key` → CPT 代码。CPT 版权问题在非商业阶段不处理。
- 医生信息：NPI（政府公开的 NPPES）。

## 5. 问卷（问题库）
- 问题库写在 HINT 里注入提示词。LLM 根据用户的问题挑题，**每一轮对话都可以发**；用户已经说过的信息自动填好，标明“请确认”。
- 问题库要足够完整；每道题都要对应到两个数据库的查询条件；每道题都有“不知道”选项。
- 收集用户对各个因素的**权重**，供 LLM 做判断。
- 答案按字段结构化地交回，记录“来自用户问卷”，用户填的数字直接交给 Python。
- ⚠️ 问题库的具体内容、权重的设置方式、Python 要不要补上漏掉的必需字段，**之后单独讨论**。问卷的界面参考另一个产品，用 REA 研究后确定。

## 6. 搜索
- **用途**：用户问到**数据库里没有的保险公司**时，现场搜索并说明它是否合适。重点是**展示搜索过程**，不追求全面。
- **实现**：产品运行时**全部用 Playwright**，不用 Tavily。流程固定为 3–4 步：
  1. 找到这家公司的计划页面
  2. 打开页面
  3. Python 列出链接并打分，模型从前几个里挑一个
  4. 模型提取关键信息并做判断
  - 当前实现（2026-10-16）：还没有可用的模型 API，第 3、4 步暂由 Python 完成（按关键词给链接打分、用正则读出保费和自付额），回复按模板生成，不做“是否合适”的判断。接上模型后，再改为由模型挑选链接、做判断。
- **部署**：搜索 worker 部署在 **Railway**，使用 Playwright 官方 Docker 镜像。✅ 2026-10-16 改定：进度**以实时文字 + 每一步的网页截图**显示，前端右侧的 “Computer” 面板参考 Hark 的工作小窗口（见 `docs/research/hark-interaction-patterns.md` 第 5b 节）。代码在 `search_worker/`，接口见 `docs/frontend-protocol.md`。
- **搜到的信息**：用来判断是否合适（例如没有合适的计划、价格明显偏高），**不用来计算费用**，始终标明来源。
- 被网站拦截时**不去绕过**，改为提示用户。
- ✅ 负责人：Justin，计划 10 月 14 日部署。

## 7. 模型
- **所有 AI 调用都使用 AMD 上的开源模型。** 代码通过 OpenAI 兼容接口（`base_url` + API key + 模型名）调用，见 issue #2。
- **接口**：⚠️ 主办方表示 **Token Factory 算作 vLLM**，不需要 $100 额度。需要确认：Token Factory 是谁提供的、是否跑在 AMD 上、怎么拿到 key、有哪些模型、有没有调用限制。如果不合适，就在 AMD Developer Cloud 上用 vLLM 自己部署（单张 MI300X，约 $1.99/小时，不用时关机）。
- **模型**：**Qwen3.5-35B-A3B**。Qwen3.5 全系列都能看图，所以一个模型同时做对话和读保单。可以再拿 Qwen3.5-122B-A10B 在读保单的准确率上对比一次。**不使用 DeepSeek**：完整版单卡放不下，单卡放得下的版本又不能看图。

## 8. 部署
- ✅ 2026-10-16 改定：agent 流程不再放进 Vercel Python 函数，改为一个独立的 **Python FastAPI 服务**。原因：SSE 长连接会受 Vercel 函数执行时长的限制，SQLite 文件也有体积上限；FastAPI 可以直接复用 `guards.py`、`calculate_cost.py` 和 `scripts/mrf/lookup.py`。
- ~~**Vercel**：Next.js 前端~~ ✅ 2026-10-17 改定：不用 Vercel，前后端都部署在 **Railway**（项目 `student-health-agent`，机房 iad，都从 `frontend` 分支部署）：
  - 前端服务 `student-health-agent`：Next.js（根目录 `/web`），网址 `student-health-agent-production.up.railway.app`，作为提交的演示网址。
  - 后端服务 `agent-api`：FastAPI agent 服务（根目录 `/`，启动命令 `cd workflow && uvicorn server:app --host 0.0.0.0 --port $PORT`），包括 agent 循环、guards、计算器和只读数据；磁盘卷 `rates-data` 挂在 `/data`，里面放价格库。环境变量：`AMD_*`、`BRANCH_A_RATES_SQLITE`、`CORS_ORIGINS`。
  - 搜索 worker（Playwright + Chromium）以后作为第三个服务加进来。
- 前后端协议见 `docs/frontend-protocol.md`。前端在没有后端时使用自带的 mock（按 Demo 流程回放事件）。
- 不使用 Supabase：数据都是只读的，用 SQLite 文件就够。✅ 2026-10-17：价格库 `rates.sqlite`（3.1 GB）不进 git，用 `railway volume files upload` 上传到 Railway 服务的磁盘卷 `/data`，再用环境变量 `BRANCH_A_RATES_SQLITE=/data/rates.sqlite` 指过去；保单库只有约 100 KB，直接放进 git。磁盘卷要求 Hobby 及以上套餐（免费版只有 0.5 GB）。
- 另外写好 `Dockerfile` 和 `docker-compose.yml`，满足比赛的容器化要求，让评委能在本机运行。

## 9. 展示 AMD 的应用
- 所有 AI 调用（问卷挑题、对话、读保单、搜索时的判断、推荐和解释）都在 AMD 上运行。
- 批量读取保单建立数据库：每份并行提取多次、互相校验，记录处理速度和准确率，放进 PPT。
- 演示中重点展示“上传保单 → 很快读完 → 马上给出评价”。
- 可选：小规模 LoRA 微调（保单服务名称 → `service_key`），只在核心功能完成后再做，训练数据必须经过人工核对。

## 10. Demo 流程
**演示人物**：26 岁的 UCLA 博士生，美国公民，刚满 26 岁、不能再用父母的保险。轻度哮喘（日常用吸入剂，一年看 2 次专科），一次流感去 urgent care，一次打球扭伤脚踝去急诊、拍 X 光。

1. 用户：“这是我的 UC SHIP，你觉得这个保险怎么样？”
2. agent 发问卷，用户填写病情和就医习惯。
3. agent 评价 UC SHIP：各场景下要花多少钱、有什么风险（价格来自 MRF 数据库）。
4. 用户：“你推荐什么别的商业保险吗？”
5. agent 再发问卷，用户填写预算和各项需求的权重。
6. agent 从数据库里匹配，推荐最适合的计划，并展示 dashboard。
7. 用户问一家**数据库里没有**的保险公司 → 搜索 → 说明为什么不合适。

## 11. 分工
- **队友**：allowed amount 数据库（MRF）、接入 AMD 模型、读保单的流程。
- **Justin**：前端（对话窗、问卷卡片、dashboard）、问题库设计、保单信息数据库的格式（#1）、收集保单 PDF、搜索 worker。

## 12. 排期

| 天 | 重点 |
| --- | --- |
| 10/13–14 | 接入 AMD 模型（Token Factory 或 vLLM）；v2 格式（#1）；搜索 worker 部署到 Railway |
| 10/15–16 | 收集保单并批量提取，建好保单信息数据库；问题库；前端（对话窗 + 问卷卡片） |
| 10/16–17 | **端到端打通演示流程**；dashboard；整理性能和准确率数据 |
| 10/17 | 部署到 Railway（前后端）；录演示视频；PPT |
| 10/18 早上 | 提交（⚠️ 以实际截止时间为准） |

## 13. 待确认事项
- [ ] 确切的提交截止时间
- [ ] Token Factory 的情况：提供方、是否跑在 AMD 上、模型、key、限制
- [ ] 数据库收录哪几家商业保险（团队挑选），以及它们在 MRF 里对应的计划编号
- [ ] 问题库的具体设计（单独讨论）
- [ ] 问卷界面的参考产品（用 REA 研究）
- [ ] `guards.py` 要改为也接受价格查询工具和问卷返回的金额
