# Product
这是一个能够进行保险判别的产品，主要干两件事情：
1. **费用预测**：针对病人现有的保险和可能的疾病，为病人要支付的价格做出一个预测。
2. **保险对比（Insurance Arena）**：针对病人的疾病，给病人提供不同保险的选择，并且通过不同保险的对比给病人一个最好的选择。

目前针对的主要群体是留学生群体，因为留学生群体不会受到各类政府政策限制，所以可以只用考虑商业保险和学校提供的保险。


# The agent NEED to know
## The policy of the insurance being selected (Policy Flyer of the insurance company)
    1. deductible （享受保险的门槛）
    2. copay （服务项目固定的价格）
    3. coinsurance (percentage copay)
    4. out-of-pocket max（每年自付封顶）
    5. 限制条件：既往病史等待期、转诊 / 预授权、不保项目

### Prompt and harness
1. Schedule of benefits: json to be put in the context window
2. 准确识别用户 request 并且对应 schedule of benefits


## The requirements of user
    1. personal info (age gender race geographic info): target at international student (insurance is the only choice, no gov supports)
    2. chronic disease
    3. 学校 waiver 要求、签证要求（如 J-1 最低保障标准）


# Agent 与代码的分工
原则：**LLM 负责把非结构化的东西变成结构化的，Python 代码负责在结构化数据上做判断和计算。**

| 交给 LLM | 写死在 Python 里 | 无法完全自动化 |
| --- | --- | --- |
| 读保单 PDF、提取 benefit | 费用计算（deductible → coinsurance → OOP max） | Allowed amount（价格不公开，只能给区间） |
| 理解用户 request、追问 | 对比打分 / 排序 | 保证理赔结果（需人工核实） |
| 搜集计划（官网 / computer use） | 数据校验（schema + 规则） | 用户未来的病情（只能按场景假设） |
| 解释结果、引用原文 | | |

Agent 作为调度员，把能力做成 tools：查规则、算钱、估算 allowed amount、对比计划、查原文。每个 tool 内部尽量确定、可测试。


# Agent Output

## 情况一：费用预测
它最终生成的就是一个非常直接的数字，来告诉你用户的计算，然后用表格形式呈现中间计算过程中每一步的消费，所以这是一个非常直接的结果。

### Allowed amount
1. social media
2. 根据这个 insurance 和 user info 针对的搜索
3. 公开价格数据（如 Transparency in Coverage、医院 price transparency 文件）

拿不到确定值时，输出**区间**（低 / 中 / 高三个 allowed amount 各算一遍），并标明是估算及来源。


## 情况二：Insurance Arena
compare different insurance plans for the user

数据来源：
1. social media (support)
2. **insurance company website**
3. according to user input

这个 compare 的计划里面会生成一个 dashboard，然后把不同的影响因子放进去，通过可视化的形式展示出来。同时，dashboard 旁边还可以开展一个 bot，来针对 dashboard 上的内容由用户继续提供提问。

### 对比 criteria（三层）
1. **硬性门槛**（不合格直接排除，不打分）：学校 waiver、签证要求、用户必需项目（常用药、医生是否在网、既往病史等待期）。
2. **核心指标 = 年度总花费** = 年保费 + 预期自付（受 OOP max 封顶），按场景计算：
   - 健康年（体检 + 少量门诊）
   - 用户的预期使用（根据慢性病推出，用户可调整）
   - 意外大病（急诊 + 住院，衡量最坏情况）
3. **非价格因素**（只展示，不混进总分）：网络大小、转诊 / 预授权、理赔口碑。

不使用综合评分（如 8.5/10），用"省多少钱 + 有什么风险"说话。区间重叠很多时，直接告诉用户"差不多，看其他因素"。


# 前端形式：Dashboard 报告 + Bot
- **Dashboard**（Next.js，好看、动态），从结论到细节逐层展开：
  1. 推荐结论（一句话说明原因 + 被排除的计划）
  2. 场景对比图（每个计划的花费区间，可调整假设）
  3. 对比表（2–4 个计划并排，和用户病情相关的行高亮）
  4. 细节和出处（每个数字对应保单页码或估算依据）
- **Bot 侧栏**：用户可以根据报告进一步提问。
  - bot 能修改假设（如"我一年看 10 次专科"），dashboard 随之更新
  - 点击 dashboard 上的条目，bot 直接解释该条
  - bot 不自己算数，所有数字来自后端 tools，保证和 dashboard 一致
  - 不给医疗建议、不保证理赔，必要时引导用户找保险公司 / 学校核实
- 手机端：bot 改为底部抽屉。

### 架构
```
Next.js 前端（Dashboard + Bot）
        │ 共享同一份 report JSON
        ▼
Python 后端（FastAPI）
 ├─ POST /report         生成对比报告
 ├─ POST /report/update  修改假设后重新计算
 └─ POST /chat           LLM agent（tools：查规则 / 算钱 / 改假设 / 查原文）
```
**report JSON 是唯一的数据来源**：dashboard 渲染它，bot 读取和修改它。下一步先定这个 schema。


# Criteria to success
1. the correctness of the info generated
2. the quality of comparing insurance
    1. in-person verification
    2. 对几组计划请懂保险的人人工排序，看算法排序是否一致
3. the scope of insurance being considered (how many companies and plans being considered)

**the quality of computer use agent**
