from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import unittest

from measurement_review.domain import explore, validate
from measurement_review.service import Workbench
from measurement_review.server import make_server
from measurement_review.storage import fingerprint


ROOT = Path(__file__).resolve().parents[1]


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures" / "data.json").read_text())
        self.selection = json.loads((ROOT / "product.json").read_text())["selection"]

    def test_fixture_is_valid_and_serializable(self):
        validate(self.data)
        view = explore(self.data, self.selection)
        self.assertTrue(view["rows"])
        self.assertTrue(view["notes"])
        json.dumps(view, allow_nan=False)

    def test_exploration_does_not_mutate_inputs(self):
        before = deepcopy(self.data)
        explore(self.data, self.selection)
        self.assertEqual(before, self.data)

    def test_missing_table_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            validate({})

    def test_all_missing_column_has_no_center(self):
        view = explore(self.data, {"batch": "transfer"})
        copied = next(row for row in view["rows"] if row["field"] == "signal_a_copy")
        self.assertIsNone(copied["median"])
        self.assertEqual(copied["observed"], 0)

    def test_review_overlay_does_not_rewrite_original(self):
        before_records = deepcopy(self.data["records"])
        self.data["reviews"][0]["revised_value"] = 33
        view = explore(self.data, self.selection)
        self.assertEqual(before_records, self.data["records"])
        self.assertIn("review_1", view["model"]["reviews_applied"])

    def test_reference_record_keeps_original_value_in_evidence(self):
        self.data["reviews"][0]["revised_value"] = 33
        view = explore(self.data, {"batch": "reference"})
        entry = next(row for row in view["records"] if row["record_id"] == "record_011")
        cell = next(cell for cell in entry["cells"] if cell["field"] == "signal_b")
        self.assertEqual(cell["value"], 45.5)

class MultivariateTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures" / "data.json").read_text())

    def test_cell_anomaly_traced_to_original_measurement(self):
        view = explore(self.data, {"batch": "current"})
        entry = next(row for row in view["records"] if row["record_id"] == "record_118")
        self.assertEqual(entry["status"], "单元格可疑")
        cell = next(cell for cell in entry["cells"] if cell["field"] == "signal_b")
        self.assertTrue(cell["suspicious"])
        self.assertEqual(cell["value"], 45.5)
        self.assertEqual(cell["unit"], "mV")
        self.assertLess(cell["upper"], 45.5)
        self.assertIn("45.5", entry["explanation"])

    def test_combination_anomaly_without_suspicious_cell(self):
        view = explore(self.data, {"batch": "current"})
        entry = next(row for row in view["records"] if row["record_id"] == "record_117")
        self.assertEqual(entry["status"], "整记录偏离")
        self.assertFalse(any(cell.get("suspicious") for cell in entry["cells"]))
        self.assertIn("组合", entry["explanation"])

    def test_consistent_record_is_unflagged(self):
        view = explore(self.data, {"batch": "current"})
        entry = next(row for row in view["records"] if row["record_id"] == "record_090")
        self.assertEqual(entry["status"], "一致")

    def test_batch_drift_reported_as_single_finding(self):
        view = explore(self.data, {"batch": "transfer"})
        assessment = view["batch_assessment"]
        self.assertEqual(assessment["verdict"], "批次整体偏移")
        self.assertLessEqual(assessment["drift_p"], 0.01)
        self.assertIn("temperature", [shift["field"] for shift in assessment["field_shifts"]])
        self.assertIn("参照", assessment["explanation"])

    def test_near_duplicate_column_is_merged(self):
        view = explore(self.data, {"batch": "current"})
        self.assertEqual(view["model"]["duplicate_groups"],
                         [{"kept": "signal_a", "merged": ["signal_a_copy"], "correlation": 1.0}])
        self.assertNotIn("signal_a_copy", view["model"]["fields_used"])

    def test_threshold_ignores_checked_batch(self):
        baseline = explore(self.data, {"batch": "current"})["model"]
        tampered = deepcopy(self.data)
        for record in tampered["records"]:
            if record["batch"] == "current":
                record["values"] = {key: (value * 5 if value is not None else None)
                                    for key, value in record["values"].items()}
        altered = explore(tampered, {"batch": "current"})["model"]
        self.assertEqual(baseline["fingerprint"], altered["fingerprint"])
        self.assertEqual(baseline["empirical_threshold"], altered["empirical_threshold"])

    def test_assessment_is_deterministic(self):
        self.assertEqual(explore(self.data, {"batch": "current"}), explore(self.data, {"batch": "current"}))

    def test_assessment_does_not_mutate_data(self):
        before = deepcopy(self.data)
        explore(self.data, {"batch": "transfer"})
        self.assertEqual(before, self.data)

    def test_reference_self_check_uses_leave_one_out(self):
        view = explore(self.data, {"batch": "reference"})
        entry = next(row for row in view["records"] if row["record_id"] == "record_011")
        self.assertEqual(entry["status"], "单元格可疑")
        self.assertEqual(view["batch_assessment"]["verdict"], "参照批次自身")
        self.assertIn("留一法", view["model"]["calibration"]["method"])

    def test_missing_reference_batch_degrades_openly(self):
        for record in self.data["records"]:
            if record["batch"] == "reference":
                record["batch"] = "archive"
        view = explore(self.data, {"batch": "current"})
        self.assertIsNone(view["model"])
        self.assertEqual(view["batch_assessment"]["verdict"], "无法判定")
        self.assertTrue(all(entry["status"] == "无法评估" for entry in view["records"]))
        self.assertTrue(view["limitations"])

    def test_insufficient_pair_is_disclosed(self):
        self.data["fields"].append({"id": "pressure", "unit": "kPa", "range": [0, 200]})
        for index, record in enumerate(self.data["records"][:10]):
            record["values"]["pressure"] = 100.0 + index
        view = explore(self.data, {"batch": "current"})
        self.assertIn({"fields": ["signal_a", "pressure"], "common_observations": 10},
                      view["model"]["pairs_insufficient"])
        self.assertTrue(any("pressure" in limitation for limitation in view["limitations"]))


class WhatIfTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workbench = Workbench(ROOT, self.temporary.name)

    def test_revising_checked_record_leaves_model_untouched(self):
        result = self.workbench.whatif({"selection": {"batch": "current"},
            "change": {"record_id": "record_118", "action": "revise", "field": "signal_b", "value": 36.6}})
        self.assertFalse(result["model"]["changed"])
        self.assertEqual(result["changed_total"], 1)
        self.assertEqual(result["target"]["before"]["status"], "单元格可疑")
        self.assertEqual(result["target"]["after"]["status"], "一致")

    def test_excluding_reference_record_refits_model(self):
        result = self.workbench.whatif({"selection": {"batch": "current"},
            "change": {"record_id": "record_011", "action": "exclude"}})
        self.assertTrue(result["model"]["changed"])
        self.assertNotEqual(result["model"]["empirical_threshold_before"],
                            result["model"]["empirical_threshold_after"])

    def test_keep_is_baseline(self):
        result = self.workbench.whatif({"selection": {"batch": "current"},
            "change": {"record_id": "record_118", "action": "keep"}})
        self.assertEqual(result["changed_total"], 0)
        self.assertFalse(result["model"]["changed"])

    def test_whatif_does_not_persist(self):
        before = self.workbench.store.datasets()
        self.workbench.whatif({"selection": {"batch": "current"},
            "change": {"record_id": "record_011", "action": "exclude"}})
        self.assertEqual(before, self.workbench.store.datasets())

    def test_unknown_record_rejected(self):
        with self.assertRaises(ValueError):
            self.workbench.whatif({"change": {"record_id": "nope", "action": "keep"}})


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workbench = Workbench(ROOT, self.temporary.name)

    def test_review_saved_as_new_version_preserving_original(self):
        saved = self.workbench.add_review({"review": {"record_id": "record_011", "field": None,
            "decision": "exclude", "note": "复测后排除", "revised_value": None}})
        self.assertNotEqual(saved["revision"], self.workbench.default_revision)
        original = self.workbench.store.dataset(self.workbench.default_revision)
        self.assertEqual(len(original["reviews"]), 1)
        updated = self.workbench.store.dataset(saved["revision"])
        self.assertEqual(len(updated["reviews"]), 2)
        self.assertEqual(updated["records"], original["records"])
        view = self.workbench.inspect({"revision": saved["revision"], "selection": {"batch": "current"}})["view"]
        self.assertEqual(view["model"]["reference_fitted"], 89)
        self.assertIn("record_011", view["model"]["reference_excluded"])

    def test_review_validation(self):
        with self.assertRaises(ValueError):
            self.workbench.add_review({"review": {"record_id": "missing", "decision": "confirm"}})
        with self.assertRaises(ValueError):
            self.workbench.add_review({"review": {"record_id": "record_011", "decision": ""}})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workbench = Workbench(ROOT, self.temporary.name)

    def test_content_addressed_import_is_idempotent(self):
        data = self.workbench.store.dataset(self.workbench.default_revision)
        uploaded = self.workbench.upload({"label": "再次导入", "data": data})
        self.assertEqual(uploaded["revision"], self.workbench.default_revision)
        self.assertEqual(len(self.workbench.store.datasets()), 1)

    def test_browser_numeric_roundtrip_preserves_fingerprint(self):
        self.assertEqual(fingerprint({"values": [1.0, 2, -0.0]}), fingerprint({"values": [1, 2.0, 0]}))

    def test_saved_description_keeps_original_dataset(self):
        saved = self.workbench.save({"title": "会议记录"})
        original = deepcopy(saved)
        data = self.workbench.store.dataset(self.workbench.default_revision)
        data["context_note"] = "新资料"
        newer = self.workbench.upload({"label": "新数据", "data": data})
        self.assertNotEqual(newer["revision"], saved["revision"])
        reopened = self.workbench.store.notebook(saved["id"])
        self.assertEqual(reopened, original)
        self.assertEqual(fingerprint(reopened["dataset"]), saved["revision"])

    def test_invalid_upload_leaves_no_dataset(self):
        with self.assertRaises(ValueError):
            self.workbench.upload({"label": "坏数据", "data": {}})
        self.assertEqual(len(self.workbench.store.datasets()), 1)

    def test_unknown_version_cannot_fall_back(self):
        with self.assertRaises(LookupError):
            self.workbench.inspect({"revision": "missing"})

    def test_blank_title_rejected(self):
        with self.assertRaises(ValueError):
            self.workbench.save({"title": " "})

    def test_saved_view_is_reproducible_after_restart(self):
        saved = self.workbench.save({"title": "重启核对"})
        restarted = Workbench(ROOT, self.temporary.name)
        self.assertEqual(saved, restarted.store.notebook(saved["id"]))


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.server = make_server(ROOT, self.temporary.name, port=0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temporary.cleanup()

    def test_http_explore_save_and_read(self):
        with urlopen(self.base + "/health") as response:
            self.assertEqual(json.load(response)["status"], "ok")
        request = Request(self.base + "/api/notebooks", data=json.dumps({"title": "页面保存"}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            saved = json.load(response)
            self.assertEqual(response.status, 201)
        with urlopen(self.base + "/api/notebooks/" + str(saved["id"])) as response:
            self.assertEqual(saved, json.load(response))

    def test_bad_data_returns_client_error(self):
        request = Request(self.base + "/api/datasets", data=b'{"label":"bad","data":{}}',
                          headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_unknown_route_is_not_static_file_access(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.base + "/../product.json")
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_http_whatif_and_review_roundtrip(self):
        request = Request(self.base + "/api/whatif", data=json.dumps(
            {"selection": {"batch": "current"},
             "change": {"record_id": "record_118", "action": "revise",
                        "field": "signal_b", "value": 36.6}}).encode(),
            headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
        self.assertFalse(result["model"]["changed"])
        request = Request(self.base + "/api/reviews", data=json.dumps(
            {"review": {"record_id": "record_011", "field": None, "decision": "exclude",
                        "note": "", "revised_value": None}}).encode(),
            headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
            saved = json.load(response)
        with urlopen(self.base + "/api/data?revision=" + saved["revision"]) as response:
            data = json.load(response)
        self.assertEqual(len(data["reviews"]), 2)

    def test_http_whatif_bad_action_is_client_error(self):
        request = Request(self.base + "/api/whatif", data=json.dumps(
            {"change": {"record_id": "record_118", "action": "delete"}}).encode(),
            headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as caught:
            urlopen(request)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
