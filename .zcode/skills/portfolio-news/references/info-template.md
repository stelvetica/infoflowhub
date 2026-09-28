# 标的信息库 JSON 产出约束

基于《标的池.json》维护《标的信息库.json》，作为 skill 的持久资产，增量更新。

## 产出格式

```json
{
  "updated_at": "YYYY-MM-DD",
  "stocks": [
    {
      "code": "688249.SH",
      "name": "晶合集成",
      "main_business": "12英寸晶圆代工",
      "industry": "半导体-晶圆代工",
      "chains": ["芯片代工", "AI算力"],
      "in_pool": true
    }
  ]
}
```

完整示例见 `references/pool-example.json` 的 `info_db` 字段。

## 增量更新规则

1. 读《标的池.json》的全部代码，与已有信息库对比：
   - **已有代码 + 在池**：保持原信息，`in_pool=true`
   - **已有代码 + 不在池**：**保留信息**，`in_pool=false`（不删除，避免反复进出持仓时重算）
   - **新增代码**：基于公开信息补 `main_business`/`industry`/`chains`，`in_pool=true`
2. 更新 `updated_at` 为今日。

## 字段口径

| 字段 | 说明 |
|---|---|
| `code` | 股票代码（与 Excel 一致，含后缀） |
| `name` | 股票名称（与 Excel 一致） |
| `main_business` | 一句话主营业务，基于公开信息 |
| `industry` | 细分行业，如"半导体-晶圆代工""电力设备-风电整机" |
| `chains` | 产业链数组，一只股票可属多个链条，如宁德时代属["动力电池","储能"] |
| `in_pool` | 是否在当前持仓池内 |

## 硬约束

1. **唯一产出格式是 JSON**，不输出 MD、不包裹代码块。
2. 不修改已有标的的 `main_business`/`industry`/`chains`（除非明显错误，且须保守）。
3. 不删除 `in_pool=false` 的标的——只改标记，不丢信息。
4. `chains` 命名需保持一致性（同一产业链在所有标的中用同一名称，如统一用"AI算力"而非"AI算力链"）。
5. JSON 必须可被 `json.loads` 解析，无尾逗号。

## 禁止事项

- 不编造主营业务；不确定时基于公开信息给最可能答案，不写"暂无"。
- 不在 `chains` 中塞行业名（行业用 `industry` 字段，`chains` 只放产业链环节名）。
- 不修改 `code`/`name`（以 Excel 为准）。
