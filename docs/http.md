# 本地接口

服务只用于本地教研复核，不连接真实人员目录或外部数据源。数据以 UTF-8 JSON 传输；未找到对象返回 404，无效业务输入返回 400，正文超出 8MB 返回 413。

- `GET /health` 返回就绪状态。
- `GET /api/context` 返回产品配置、默认版本、已导入版本与保存记录目录。
- `GET /api/data?revision=版本` 返回该版本完整原始数据；省略版本使用演示材料。
- `POST /api/datasets` 接收 `label` 和完整 `data`，返回新建或复用的 `revision`。
- `POST /api/explore` 接收可选 `revision` 和 `selection`，返回数据指纹、实际选择和领域描述。
- `POST /api/whatif` 接收 `revision`、`selection` 和 `change`（record_id、action=keep/exclude/revise/clear、field、value），返回试算前后模型指纹、经验阈值、判定变化的记录与目标记录对照。试算只读，不落盘。
- `POST /api/reviews` 接收 `revision` 和 `review`（record_id、field 可空、decision、note、revised_value 可空），把意见追加为新的数据版本并返回新 `revision` 与 `review_id`；原版本不变。
- `POST /api/notebooks` 使用相同选择参数和 `title`，服务端重新计算后保存，不能由客户端伪造结果。
- `GET /api/notebooks` 返回材料目录，`GET /api/notebooks/编号` 返回冻结描述、选择条件和所用数据。

领域描述包含 `summary`、`rows`、`series`、`notes`，以及变量间关系复核的 `records`（逐记录判定、可疑单元格、复核意见、解释）、`batch_assessment`（批次判定与漂移检验）、`model`（模型字段、近似重复、校准方式、经验阈值、模型指纹）和 `limitations`（判断限制）。这里记录的是已有浏览交互，不限制新分析功能的内部结构。增添业务能力时需保持现有原始记录和保存材料可读取。
