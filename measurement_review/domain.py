from collections import Counter, defaultdict
from statistics import mean, median
import math

from .validation import require_tables, unique, finite, references


def validate(data):
    require_tables(data, ["records", "fields", "reviews"])
    unique(data["records"], ["record_id"])
    unique(data["fields"], ["id"])
    unique(data["reviews"], ["review_id"])
    references(data["reviews"], "record_id", data["records"], "record_id")
    fields = {field["id"] for field in data["fields"]}
    for record in data["records"]:
        if not set(record["values"]) <= fields:
            raise ValueError("记录包含未知字段")
        for value in record["values"].values():
            if value is not None:
                finite(value)
    for review in data["reviews"]:
        if review["field"] is not None and review["field"] not in fields:
            raise ValueError("复核意见引用未知字段")
        if review["revised_value"] is not None:
            finite(review["revised_value"])
    for field in data["fields"]:
        if len(field["range"]) != 2 or field["range"][0] > field["range"][1]:
            raise ValueError("物理范围无效")


def explore(data, selection):
    batch = selection.get("batch", "current")
    records = [record for record in data["records"] if record["batch"] == batch]
    if not records:
        raise ValueError("此批次没有测量记录")
    rows = []
    for field in data["fields"]:
        values = [record["values"].get(field["id"]) for record in records]
        observed = [value for value in values if value is not None]
        center = median(observed) if observed else None
        rows.append({"field": field["id"], "unit": field["unit"], "observed": len(observed),
                     "missing": len(values) - len(observed), "median": center,
                     "median_absolute_deviation": median(abs(value - center) for value in observed) if observed else None,
                     "outside_physical_range": sum(not field["range"][0] <= value <= field["range"][1] for value in observed)})
    return {"summary": {"测量记录": len(records), "字段": len(rows), "缺测格": sum(row["missing"] for row in rows)},
            "rows": rows, "series": [{"label": row["field"], "y": row["missing"]} for row in rows],
            "notes": ["范围核对不等于多变量异常检测，不自动修改原始测量。", "中位数按当前选择批次描述，不是经验证的参考分布。"]}
