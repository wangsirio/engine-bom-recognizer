# 企业标题栏 JSON 模板

在 Web 工作台进入「系统设置 → 标题栏 JSON 模板」。可直接添加输出键并保存，也可以一次导入 [示例格式的 JSON 文件](../static/titleblock-template.example.json)。模板保存在 `data/titleblock_template.json`，服务重启后仍会生效；固定模型配置（`--config`）不影响模板保存。每个新 BOM 任务读取当时的模板，后续编辑模板不会改写已有任务的标题栏输出。

每条配置含 `key`（最终 JSON 键）、`sources`（按优先顺序排列的系统字段）、`aliases`（原图标签的可选别名，按顺序排在系统字段之后）。`sources` 和 `aliases` 至少配置其一。系统字段列表在页面可展开查看。`aliases` 可用于指定现有字段表以外的印刷标签，需要真实视觉模型读图；Mock 只返回固定演示数据。

多个等价来源对同一个输出键只生成一个值。系统采用首个非空值，不拼接。如果不同来源的值不一致，BOM 结果页会提示人工复核。配置的键找不到有效值就输出 `null`。没配置的键不输出到企业版 JSON；标准 BOM JSON、CSV 以及审图用的原始标题栏不改结构。

识别完成后，在「标题栏信息 → 查看企业版标题栏 JSON」可检查结果并下载企业 BOM JSON。导出的 `title_block` 是模板化对象，和企业 BOM 的 `rows` 并列。

接口：`GET /api/enterprise/titleblock-template` 返回当前 `template` 和 `sources`；`POST` 使用页面的 `X-API-Token`，提交 `{"template": {"name": "…", "fields": […]}}` 持久保存，提交 `{"template": null}` 停用。
