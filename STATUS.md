# 项目现状（2026-10-16）

一句话：用户问"我在某家医院看一次病要自付多少"，系统用 **价格库 × 规则库** 算出金额。金额全部由 Python 计算，AI 只负责理解问题和解释结果。

## 三个部分

| 部分 | 内容 | 位置 | 状态 |
|---|---|---|---|
| 价格库（Branch B） | 保险公司公开的协商价（allowed amount），7 个计划 / 6 家保险公司，洛杉矶医院和医生 | `data/mrf/`（`rates.sqlite`、`allowed_amounts.csv`）；生成脚本在 `scripts/mrf/` | ✅ 完成 |
| 规则库（Branch A） | 各计划的 copay、自付比例、自付额等，不含价格 | prime_100：`workflow/normalized_pdf_insurance_rules.json`（66 条）；其他计划：`policies/`（尚未生成） | ⚠️ 只有 prime_100 |
| 匹配 + 计算 | 计划 ↔ 价格文件，服务 ↔ 收费代码（如普通门诊 = 99213 / 99203），然后算钱 | `branch-a-mrf-integration/overlay/`；计算器 `workflow/calculate_cost.py` | ⚠️ 只连好了 prime_100 |

## 规则库从哪来
- **不是 AI 建的。** 由 Perplexity 检索官方文件（SBC PDF），**人工核对后**在 `verified` 列填 `yes`，再导入：
  `python scripts/policy/import_plan_csv.py plans.csv benefits.csv` → 生成 `policies/plans.json` 和 `policies/rules.json`
- 只有 `verified=yes` 的行能用来算钱；其他行一律标成"需要人工确认"。
- 现有进度：plans.csv 已有 26 个计划，但大多是 NOT FOUND；**benefits.csv 还没有**，它才是算钱用的那张表。

## 计算规则
- 只有 copay：付 copay。
- 只有比例：自付额部分自己付，剩下的按比例付。
- **copay + 比例**（如 prime_100 门诊 "$25 + 80%"）：先付 copay，剩下的再付比例。例：$200 → 25 + 175 × 20% = $60。如果还要先过自付额，目前返回"需要人工确认"。
- 不计入自付上限（OOP max）和 balance billing。

## 例子：prime_100 在 UCLA 看一次普通门诊
- 价格：$432.76（UHC Choice Plus，代码 99213）
- 规则：$25 copay + 20%，不走自付额 → 应为 $106.55
- **现在还算不出来**：规则库里这一行没提取完整（copay 和比例都是空的），需要人工把这两个值补上（copay 25、自付 20%、不走自付额）。

## 下一步
1. 用 Perplexity 补 benefits.csv（先做 Covered California 的 4 个标准档次，再做 UC SHIP），人工核对后导入。
2. 把新计划连到价格库，把 12 种服务连到收费代码（只是配置，需要人工确认）。
3. 补 prime_100 门诊那一行。
4. AI 对话时附上保单的定义（definitions）原文：`policies/definitions/prime_100.txt`。

## 跑测试
```bash
python -m unittest discover tests
cd branch-a-mrf-integration/overlay && python -m unittest discover -s tests
```
