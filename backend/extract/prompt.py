"""看图识别提示词与默认配置（带 prompt_version）。"""
from __future__ import annotations

PROMPT_VERSION = "dc12-extract-v3"

# Q-05 隐藏把握度阈值：中等，不进设置页（ENGINEERING / 派单建议 0.55）
DEFAULT_CONFIDENCE_THRESHOLD = 0.55

# 格式错误默认重试次数（可配置）
DEFAULT_FORMAT_RETRIES = 1

# 角色：本单只调主模型识别，禁止同模型双跑冒充复核
EXTRACT_ROLE = "primary"

SYSTEM_PROMPT = """你是建筑/电气/土建设计图纸看图助手。根据审查图提取结构化事实，只输出 JSON，不要 Markdown。
坐标框为图纸单元内归一化坐标：原点左上，x 向右、y 向下，范围 0～1，且 x0<=x1、y0<=y1。
把握度 confidence 为 0～1；看不清时 unclear=true 并尽量给出可见残缺文本。
一页多个图框时 title_blocks 返回多个；无图签则 title_blocks 为空数组。
类别包括尺寸、标高、编号写法、材料规格、数量与材料表、详图索引断链。详图标题记录detail_number，索引记录target_drawing_number和target_detail_number。
只有图中编号、轴线/位置或明确引用关系能证明同一对象时才提供object_id、comparison_key（对象+属性键），并置identity_confirmed=true。
同名、同材料种类、同一行或纯数字相同不能证明同一对象；不明确时identity_confirmed=false。比较对象标识须包含位置/编号，跨图一致。
quantity_role记录drawing_count或material_table。不要做数量计算，只读原文及明示数值。
目录识别：is_catalog_sheet只在当前图片可见“图纸目录/图纸文件目录”或明确图纸表格时为true。
catalog_listed_numbers逐行逐字抄录表格“图号/图纸编号”列，保留完整前后缀；不得把卷册号、文件代号、序号、页码当图纸页号，不补零、不按规律推算缺失图号。卷册号另放catalog_volume_numbers。
catalog_complete仅当当前输入图片中整个目录表的边界和全部图号行都可读、无省略/遮挡/跨图裁切时为true；只看到部分表或有看不清的行必须false。catalog_bbox为整个可见目录表框，catalog_entries可逐项提供图号及其可见原文坐标、confidence/unclear。
非目录页is_catalog_sheet=false；目录页即使读不全仍保留明确读出的图号，并设catalog_complete=false。不得把“模型调用成功”视为目录完整。
禁止编造看不见的内容。"""

USER_PROMPT_TEMPLATE = """请识别本张审查图，按下列 JSON schema 返回（prompt_version={prompt_version}）：
{{
  "prompt_version": "{prompt_version}",
  "is_catalog_sheet": false,
  "catalog_listed_numbers": [],
  "catalog_volume_numbers": [],
  "catalog_complete": false,
  "catalog_bbox": null,
  "catalog_entries": [
    {{"drawing_number":"逐字读取的图号", "bbox":{{"x0":0,"y0":0,"x1":1,"y1":1}}, "confidence":0.0, "unclear":false}}
  ],
  "title_blocks": [
    {{
      "specialty": "专业",
      "drawing_number": "图号",
      "name": "图名",
      "bbox": {{"x0":0,"y0":0,"x1":1,"y1":1}},
      "confidence": 0.0,
      "unclear": false
    }}
  ],
  "facts": [
    {{
      "category": "类别",
      "object_id": "明确对象编号或位置；不明则空字符串",
      "comparison_key": "同一对象属性键；不明则空字符串",
      "identity_confirmed": false,
      "target_drawing_number": null,
      "target_detail_number": null,
      "detail_number": null,
      "quantity_role": null,
      "text": "原文",
      "numeric_value": null,
      "unit": null,
      "bbox": {{"x0":0,"y0":0,"x1":1,"y1":1}},
      "confidence": 0.0,
      "unclear": false
    }}
  ]
}}
只输出一个 JSON 对象。"""


def build_user_prompt(prompt_version: str = PROMPT_VERSION) -> str:
    return USER_PROMPT_TEMPLATE.format(prompt_version=prompt_version)
