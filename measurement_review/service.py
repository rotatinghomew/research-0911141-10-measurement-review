from copy import deepcopy
from pathlib import Path
import json

from .domain import explore, validate
from .multivariate import assess
from .storage import Store, fingerprint
from .validation import finite


class Workbench:
    def __init__(self, root, state):
        self.root = Path(root)
        self.product = json.loads((self.root / "product.json").read_text(encoding="utf-8"))
        self.store = Store(Path(state) / "research.sqlite3")
        initial = json.loads((self.root / "fixtures" / "data.json").read_text(encoding="utf-8"))
        validate(initial)
        self.default_revision = self.store.put_dataset("演示材料", initial)

    def context(self):
        return {"product": self.product, "default_revision": self.default_revision,
                "datasets": self.store.datasets(), "notebooks": self.store.notebooks()}

    def upload(self, body):
        data = body["data"]
        try:
            validate(data)
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("字段结构不完整或类型不符") from error
        return {"revision": self.store.put_dataset(body.get("label", ""), data)}

    def inspect(self, body):
        revision = body.get("revision", self.default_revision)
        selection = body.get("selection", self.product["selection"])
        if not isinstance(selection, dict):
            raise ValueError("选择条件必须是对象")
        data = self.store.dataset(revision)
        result = explore(data, selection)
        return {"revision": revision, "selection": selection, "data_sha256": fingerprint(data), "view": result}

    def save(self, body):
        result = self.inspect(body)
        return self.store.save(body.get("title", ""), result["revision"], result["selection"], result)

    def whatif(self, body):
        """试算保留/排除/修订一条记录的影响；只读对比，不落盘、不改原始记录。"""
        revision = body.get("revision", self.default_revision)
        selection = body.get("selection", self.product["selection"])
        if not isinstance(selection, dict):
            raise ValueError("选择条件必须是对象")
        change = body.get("change")
        if not isinstance(change, dict):
            raise ValueError("缺少试算内容")
        record_id = change.get("record_id")
        action = change.get("action")
        if action not in {"keep", "exclude", "revise", "clear"}:
            raise ValueError("未知试算操作：应为 keep/exclude/revise/clear")
        data = self.store.dataset(revision)
        records = {record["record_id"]: record for record in data["records"]}
        if record_id not in records:
            raise ValueError("试算记录不存在：" + str(record_id))
        field = change.get("field")
        known_fields = {item["id"] for item in data["fields"]}
        if action in {"revise", "clear"} and field not in known_fields:
            raise ValueError("试算字段不存在：" + str(field))
        value = change.get("value")
        if action == "revise":
            if value is None:
                raise ValueError("修订试算需要提供数值")
            finite(value)
        baseline = assess(data, selection)
        extra, overrides = [], {}
        if action == "exclude":
            extra = [{"review_id": "what-if", "record_id": record_id, "field": None,
                      "decision": "exclude", "note": "", "revised_value": None}]
        elif action == "revise":
            extra = [{"review_id": "what-if", "record_id": record_id, "field": field,
                      "decision": "revise", "note": "", "revised_value": value}]
            overrides = {record_id: {field: value}}
        elif action == "clear":
            extra = [{"review_id": "what-if", "record_id": record_id, "field": field,
                      "decision": "exclude", "note": "", "revised_value": None}]
            overrides = {record_id: {field: None}}
        scenario = assess(data, selection, extra_reviews=extra, score_overrides=overrides)
        before = {entry["record_id"]: entry for entry in baseline["records"]}
        after = {entry["record_id"]: entry for entry in scenario["records"]}
        changed = [{"record_id": rid, "before": entry["status"], "after": after[rid]["status"],
                    "distance_before": entry["distance"], "distance_after": after[rid]["distance"]}
                   for rid, entry in before.items() if entry["status"] != after[rid]["status"]]
        distances_shifted = sum(1 for rid, entry in before.items()
                                if entry["distance"] != after[rid]["distance"])
        model_before = baseline["model"] or {}
        model_after = scenario["model"] or {}
        fingerprint_before = model_before.get("fingerprint")
        fingerprint_after = model_after.get("fingerprint")
        notes = []
        if action == "keep":
            notes.append("“保留”是基线对照：模型与所有判定不变。")
        elif fingerprint_before != fingerprint_after:
            notes.append("试算改变了参照拟合：模型指纹与经验阈值随之变化，判定变化的记录如下。")
        elif records[record_id]["batch"] == "reference":
            notes.append("试算未改变模型：该调整对参照拟合影响有限。")
        else:
            notes.append("该记录属于待检批次，不参与参照拟合；模型与阈值不变，其他记录判定不受影响。"
                         "阈值只由参照批次校准，避免用待检数据自我证明。")
        notes.append("试算结果不落盘；原始记录与已有复核意见保持不变。")
        return {"revision": revision, "selection": selection,
                "change": {"record_id": record_id, "action": action, "field": field, "value": value},
                "model": {"fingerprint_before": fingerprint_before, "fingerprint_after": fingerprint_after,
                          "changed": fingerprint_before != fingerprint_after,
                          "empirical_threshold_before": model_before.get("empirical_threshold"),
                          "empirical_threshold_after": model_after.get("empirical_threshold")},
                "changed_records": changed[:100], "changed_total": len(changed),
                "distances_shifted": distances_shifted,
                "target": {"record_id": record_id, "before": before.get(record_id), "after": after.get(record_id)},
                "notes": notes}

    def add_review(self, body):
        """把人工复核意见追加为新的数据版本；原始记录与旧版本保持不变。"""
        revision = body.get("revision", self.default_revision)
        review = body.get("review")
        if not isinstance(review, dict):
            raise ValueError("缺少复核意见")
        data = deepcopy(self.store.dataset(revision))
        record_id = review.get("record_id")
        if record_id not in {record["record_id"] for record in data["records"]}:
            raise ValueError("复核意见引用的记录不存在：" + str(record_id))
        field = review.get("field")
        if field is not None and field not in {item["id"] for item in data["fields"]}:
            raise ValueError("复核意见引用未知字段：" + str(field))
        decision = review.get("decision")
        if not isinstance(decision, str) or not decision.strip() or len(decision) > 40:
            raise ValueError("意见类型应为1至40字")
        note = review.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise ValueError("说明应为不超过500字的文本")
        revised = review.get("revised_value")
        if revised is not None:
            finite(revised)
        existing = {item["review_id"] for item in data["reviews"]}
        number = len(existing) + 1
        while f"review_{number}" in existing:
            number += 1
        review_id = f"review_{number}"
        data["reviews"].append({"review_id": review_id, "record_id": record_id, "field": field,
                                "decision": decision.strip(), "note": note.strip(), "revised_value": revised})
        validate(data)
        label = next((item["label"] for item in self.store.datasets() if item["revision"] == revision), "数据")
        return {"revision": self.store.put_dataset((label + " · 复核")[:120], data), "review_id": review_id}
