"""报告模板 CSV 导入、区块核验、版本确认与报告数据的端到端测试。"""
from __future__ import annotations

import copy
import json
import threading

import pytest

from app.services import report_builder as rb
from app.services.report_builder import SAMPLE_CSV, registry
from app.store import store


@pytest.fixture(autouse=True)
def clean_registry():
    registry.reset()
    yield
    registry.reset()


def _csv(*lines: str) -> str:
    return "\n".join(lines) + "\n"


HEADER = "模板编号,报告名称,区块,指标编号,指标名称,源行编号,金额"


def _read_import(result):
    assert result["ok"] is True, result.get("message")
    return result["import"]


def _metric(report, code):
    matches = [item for item in report["metrics"] if item["metric_code"] == code]
    assert len(matches) == 1
    return matches[0]


# ============================================================
# CSV 解析与结构校验
# ============================================================

class TestCsvParsing:
    def test_missing_required_column_is_rejected(self):
        result = registry.import_csv(_csv("模板编号,区块,指标编号", "T1,销售,METR-SALES-SUM"))
        assert result["ok"] is False
        assert "缺少必需列" in result["message"]
        assert registry.list_imports() == []

    def test_empty_file_is_rejected(self):
        result = registry.import_csv("")
        assert result["ok"] is False
        assert "为空" in result["message"]

    def test_header_aliases_are_accepted(self):
        text = _csv(
            "template_id,report_name,block,metric_code,metric_name,source_row_id,amount",
            "T-ALIAS,别名模板,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T-ALIAS,别名模板,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["template_id"] == "T-ALIAS"
        assert snapshot["blocks"][0]["verified"] is True

    def test_multiple_template_ids_in_one_file_are_rejected(self):
        text = _csv(
            HEADER,
            "T1,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T2,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
        )
        result = registry.import_csv(text)
        assert result["ok"] is False
        assert "多个模板编号" in result["message"]

    def test_numeric_amount_with_thousands_separator(self):
        text = _csv(
            HEADER,
            "T-FMT,格式模板,销售,METR-SALES-SUM,销售额,SALE-ROW-1,\"10\"",
            "T-FMT,格式模板,销售,METR-SALES-SUM,销售额,SALE-ROW-2,\"20\"",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["blocks"][0]["preview_value"] == 30.0


# ============================================================
# 区块与源行核验
# ============================================================

class TestBlockVerification:
    def test_sample_file_verifies_sales_and_holds_after_sale(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        assert snapshot["verified_blocks"] == ["销售"]
        assert snapshot["pending_blocks"] == ["售后"]
        sale, after_sale = snapshot["blocks"]
        assert sale["metrics"][0]["metric_code"] == "METR-SALES-SUM"
        assert sale["metrics"][0]["value"] == 30.0
        assert after_sale["metrics"] == []
        assert after_sale["pending_items"][0]["metric_code"] == "AFTERSALE-LEGACY"

    def test_unknown_alias_cites_same_name_candidates(self):
        text = _csv(
            HEADER,
            "T-ALIAS,报告,售后,AFTERSALE-LEGACY,售后金额,SALE-ROW-1,10",
        )
        snapshot = _read_import(registry.import_csv(text))
        pending = snapshot["pending_items"][0]
        assert "未知指标编号" in pending["reason"]
        assert "METR-AFTERSALE-SUM" in pending["name_candidate_codes"]

    def test_metric_without_code_uses_name_candidates_but_stays_pending(self):
        text = _csv(
            HEADER,
            "T-NOCODE,报告,销售,,销售额,SALE-ROW-1,10",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == []
        pending = snapshot["pending_items"][0]
        assert "缺少可靠指标编号" in pending["reason"]
        assert sorted(pending["name_candidate_codes"]) == ["METR-SALES-OLD", "METR-SALES-SUM"]

    def test_missing_source_row_keeps_block_pending(self):
        text = _csv(
            HEADER,
            "T-PARTIAL,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == []
        assert any("源行不完整" in item["reason"] for item in snapshot["pending_items"])

    def test_source_row_outside_query_scope_is_pending(self):
        text = _csv(
            HEADER,
            "T-OUT,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T-OUT,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
            "T-OUT,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-9,30",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == []
        assert any("不在指标" in item["reason"] for item in snapshot["pending_items"])

    def test_amount_mismatch_is_pending(self):
        text = _csv(
            HEADER,
            "T-MISMATCH,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,11",
            "T-MISMATCH,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == []
        assert any("不一致" in item["reason"] for item in snapshot["pending_items"])

    def test_unparseable_amount_is_pending(self):
        text = _csv(
            HEADER,
            "T-BAD,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,abc",
            "T-BAD,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == []
        assert any("无法解析" in item["reason"] for item in snapshot["pending_items"])

    def test_blank_amount_falls_back_to_query_rows(self):
        text = _csv(
            HEADER,
            "T-BLANK,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,",
            "T-BLANK,报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == ["销售"]
        assert snapshot["blocks"][0]["metrics"][0]["value"] == 30.0

    def test_same_name_metrics_keep_separate_codes(self):
        text = _csv(
            HEADER,
            "T-SAME,同报名,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T-SAME,同报名,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
            "T-SAME,同报名,旧口径,METR-SALES-OLD,销售额,SALE-ROW-1,10",
            "T-SAME,同报名,旧口径,METR-SALES-OLD,销售额,SALE-ROW-2,20",
        )
        snapshot = _read_import(registry.import_csv(text))
        assert snapshot["verified_blocks"] == ["销售", "旧口径"]
        codes = [metric["metric_code"] for metric in snapshot["verified_metrics"]]
        assert codes == ["METR-SALES-SUM", "METR-SALES-OLD"]
        assert snapshot["verified_metrics"][0]["stable_key"] != snapshot["verified_metrics"][1]["stable_key"]
        assert snapshot["same_name_codes"] == {"METR-SALES-OLD": ["METR-SALES-SUM"]}

    def test_metric_definitions_are_really_computed(self):
        """同名两条指标按各自口径实算：sum=30，max=20。"""
        text = _csv(
            HEADER,
            "T-CALC,计算模板,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T-CALC,计算模板,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
            "T-CALC,计算模板,旧口径,METR-SALES-OLD,销售额,SALE-ROW-1,10",
            "T-CALC,计算模板,旧口径,METR-SALES-OLD,销售额,SALE-ROW-2,20",
        )
        snapshot = _read_import(registry.import_csv(text))
        values = {m["metric_code"]: m["value"] for m in snapshot["verified_metrics"]}
        assert values == {"METR-SALES-SUM": 30.0, "METR-SALES-OLD": 20.0}


# ============================================================
# 模板版本与旧模板基线
# ============================================================

class TestTemplateVersions:
    def test_legacy_report_stays_on_old_template_by_default(self):
        report = registry.build_report()
        assert report["template_version"] == 1
        assert report["activation_scope"] == "legacy"
        assert report["total_amount"] == 20.0
        assert report["metrics"][0]["metric_code"] == "METR-SALES-OLD"

    def test_import_creates_next_version_without_activating(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        assert snapshot["template_version"] == 2
        assert snapshot["status"] == "待确认"
        report = registry.build_report()
        assert report["template_version"] == 1  # 未确认前仍用旧模板
        assert report["candidate_total_amount"] == 30.0
        assert report["pending_blocks"] == ["售后"]

    def test_partial_confirmation_switches_official_template(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        result = registry.confirm(snapshot["import_id"], "partial")
        assert result["ok"] is True
        report = result["report"]
        assert report["template_version"] == 2
        assert report["activation_scope"] == "partial"
        assert report["total_amount"] == 30.0
        assert [m["metric_code"] for m in report["metrics"]] == ["METR-SALES-SUM"]
        assert report["pending_blocks"] == ["售后"]

    def test_whole_confirmation_conflicts_with_pending_block(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        result = registry.confirm(snapshot["import_id"], "whole")
        assert result["ok"] is False
        assert "整份启用与待核区块冲突" in result["message"]
        assert result["scope_conflict"]["pending_blocks"] == ["售后"]
        assert result["scope_conflict"]["allowed_scopes"] == ["partial"]
        assert registry.build_report()["template_version"] == 1

    def test_whole_confirmation_allowed_when_every_block_verified(self):
        text = _csv(
            HEADER,
            "T-FULL,完整报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T-FULL,完整报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
            "T-FULL,完整报告,售后,METR-AFTERSALE-SUM,售后金额,AFTER-SALE-ROW-1,50",
        )
        snapshot = _read_import(registry.import_csv(text))
        result = registry.confirm(snapshot["import_id"], "whole")
        assert result["ok"] is True
        assert result["report"]["activation_scope"] == "whole"
        assert result["report"]["total_amount"] == 80.0

    def test_partial_confirmation_requires_one_verified_block(self):
        text = _csv(HEADER, "T-NONE,空洞报告,售后,UNKNOWN-CODE,售后金额,SALE-ROW-1,10")
        snapshot = _read_import(registry.import_csv(text))
        result = registry.confirm(snapshot["import_id"], "partial")
        assert result["ok"] is False
        assert "没有任何已核验区块" in result["message"]

    def test_new_template_without_confirmation_reports_nothing(self):
        """新模板尚未确认时不能顶替旧模板，也不能提前出数。"""
        text = _csv(
            HEADER,
            "T-NEW,新模板,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "T-NEW,新模板,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
        )
        _read_import(registry.import_csv(text))
        report = registry.build_report("T-NEW")
        assert report["status"] == "未导入"
        assert report["metrics"] == []
        assert report["total_amount"] == 0.0
        assert report["candidate_total_amount"] == 30.0

    def test_empty_template_id_still_reports_deterministically(self):
        report = registry.build_report("NO-SUCH-TEMPLATE")
        assert report["template_version"] == 0
        assert report["metrics"] == []

    def test_invalid_scope_is_rejected(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        result = registry.confirm(snapshot["import_id"], "everything")
        assert result["ok"] is False
        assert "启用范围" in result["message"]

    def test_confirming_twice_only_creates_one_version(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        first = registry.confirm(snapshot["import_id"], "partial")
        second = registry.confirm(snapshot["import_id"], "partial")
        assert first["ok"] and second["ok"]
        assert second["import"]["confirmations"] == 1
        versions = [
            item for item in registry.computable_templates()
            if item["template_id"] == "REPO-SALES-2026"
        ]
        assert len(versions) == 2  # 旧模板 + 本次确认的一版

    def test_concurrent_confirmation_confirms_once(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        results: list[dict] = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            results.append(registry.confirm(snapshot["import_id"], "partial"))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert all(item["ok"] for item in results)
        assert sum(1 for item in results if item["import"]["confirmations"] == 1) >= 1
        current = registry.get_import(snapshot["import_id"])
        assert current["confirmations"] == 1
        versions = [
            item for item in registry.computable_templates()
            if item["template_id"] == "REPO-SALES-2026"
        ]
        assert len(versions) == 2

    def test_reimport_of_same_file_reuses_version(self):
        first = _read_import(registry.import_csv(SAMPLE_CSV))
        second = _read_import(registry.import_csv(SAMPLE_CSV))
        assert first["import_id"] == second["import_id"]
        assert second["replayed"] is True
        assert second["template_version"] == first["template_version"]
        assert registry.import_csv(SAMPLE_CSV)["import"]["replay_count"] == 2

    def test_changed_file_creates_new_version(self):
        first = _read_import(registry.import_csv(SAMPLE_CSV))
        text = _csv(
            HEADER,
            "REPO-SALES-2026,本期销售报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10",
            "REPO-SALES-2026,本期销售报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20",
            "REPO-SALES-2026,本期销售报告,售后,METR-AFTERSALE-SUM,售后金额,AFTER-SALE-ROW-1,50",
        )
        second = _read_import(registry.import_csv(text))
        assert second["import_id"] != first["import_id"]
        assert second["template_version"] == first["template_version"] + 1
        assert second["verified_blocks"] == ["销售", "售后"]

    def test_template_list_row_is_updated_on_confirmation(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        rows = [row for row in store.rows("report_builder") if row["模板编号"] == "REPO-SALES-2026"]
        assert rows[0]["模板版本"] == 1  # 确认前不动模板清单
        registry.confirm(snapshot["import_id"], "partial")
        rows = [row for row in store.rows("report_builder") if row["模板编号"] == "REPO-SALES-2026"]
        assert rows[0]["模板状态"] == "已发布"
        assert rows[0]["模板版本"] == 2
        assert rows[0]["引用指标"] == "METR-SALES-SUM"
        assert rows[0]["启用范围"] == "partial"


# ============================================================
# 报告数据可回查
# ============================================================

class TestReportTraceability:
    def test_every_metric_is_traceable(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        report = registry.confirm(snapshot["import_id"], "partial")["report"]
        metric = _metric(report, "METR-SALES-SUM")
        assert metric["metric_identity"].startswith("METR-SALES-SUM@v")
        assert len(metric["stable_key"]) > len(metric["metric_identity"])
        assert metric["definition_fingerprint"]
        assert metric["source_rows"] == [
            {"row_id": "SALE-ROW-1", "amount": 10.0},
            {"row_id": "SALE-ROW-2", "amount": 20.0},
        ]
        assert metric["calculation_scope"]["source_row_ids"] == ["SALE-ROW-1", "SALE-ROW-2"]
        assert metric["calculation_scope"]["row_count"] == 2
        assert metric["calculation_scope"]["aggregation"] == "sum"
        assert metric["template_version"] == 2

    def test_unverified_after_sale_metric_never_enters_report(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        report = registry.confirm(snapshot["import_id"], "partial")["report"]
        codes = [item["metric_code"] for item in report["metrics"]]
        assert "AFTERSALE-LEGACY" not in codes
        assert "METR-AFTERSALE-SUM" not in codes
        assert report["excluded_metrics"][0]["block"] == "售后"
        assert report["total_amount"] == 30.0

    def test_report_does_not_leak_pending_block_even_if_it_has_verified_metric(self):
        """区块内一条指标已核验、另一条待核时，整个区块都不入报。"""
        text = _csv(
            HEADER,
            "T-MIX,混合报告,售后,METR-AFTERSALE-SUM,售后金额,AFTER-SALE-ROW-1,50",
            "T-MIX,混合报告,售后,UNKNOWN-CODE,未知指标,AFTER-SALE-ROW-1,50",
        )
        snapshot = _read_import(registry.import_csv(text))
        block = snapshot["blocks"][0]
        assert block["verified"] is False
        assert len(block["verified_entries"]) == 1  # 单条指标通过，但区块整体待核
        assert snapshot["verified_metrics"] == []

    def test_report_is_deterministic(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        registry.confirm(snapshot["import_id"], "partial")
        assert registry.build_report() == registry.build_report()

    def test_computable_templates_lists_every_version(self):
        _read_import(registry.import_csv(SAMPLE_CSV))
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV.replace("AFTERSALE-LEGACY", "METR-AFTERSALE-SUM")))
        registry.confirm(snapshot["import_id"], "whole")
        items = [
            item for item in registry.computable_templates()
            if item["template_id"] == "REPO-SALES-2026"
        ]
        assert [item["version_label"] for item in items] == ["v1", "v2", "v3"]
        assert [item["status"] for item in items] == ["已替代", "待确认", "已生效"]
        assert items[-1]["total_amount"] == 80.0


# ============================================================
# HTTP 契约
# ============================================================

def _all_paths(app) -> list[str]:
    """展开 FastAPI 的懒加载子路由，按实际匹配顺序返回路径。"""
    paths: list[str] = []
    for route in app.routes:
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested is not None:
            paths.extend(getattr(item, "path", "") for item in nested)
        elif getattr(route, "path", None):
            paths.append(route.path)
    return paths


class TestReportImportHttp:
    def test_endpoints_are_reachable(self):
        from app.main import app

        paths = _all_paths(app)
        assert "/api/report_builder/imports" in paths
        assert "/api/report_builder/imports/{import_id}/confirm" in paths
        assert "/api/report_builder/computable-templates" in paths
        assert "/api/report_builder/report" in paths
        assert "/api/report_builder/sample-csv" in paths

    def test_fixed_routes_are_registered_before_dynamic_entry_route(self):
        from app.main import app

        paths = _all_paths(app)
        dynamic = paths.index("/api/{module}/{entry_id}")
        assert paths.index("/api/report_builder/report") < dynamic
        assert paths.index("/api/report_builder/computable-templates") < dynamic

    def test_report_payload_is_json_serialisable(self):
        snapshot = _read_import(registry.import_csv(SAMPLE_CSV))
        registry.confirm(snapshot["import_id"], "partial")
        payload = json.dumps(registry.build_report(), ensure_ascii=False)
        assert "METR-SALES-SUM" in payload
        assert json.loads(payload)["total_amount"] == 30.0
