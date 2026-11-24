"""报告生成领域服务：模板 CSV 导入、区块核验、版本确认与指标报告数据。

核心约束：

- 指标身份以「指标编号 + 口径版本」为准，名称只用于展示；不同编号允许同名。
- 只有按编号核验通过的区块及其源行才进入报告；未知编号、旧别名单独待核。
- 同一份导入按内容指纹去重；确认动作加锁，并发提交同一次导入只落一个模板版本。
- 报告中的每一条指标都能回查稳定身份、源行和计算范围。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.seed import SEED_ROWS
from app.store import store

TEMPLATE_MODULE = "report_builder"
METRIC_MODULE = "metric_def"

LEGACY_TEMPLATE_ID = "REPO-SALES-2026"
LEGACY_REPORT_NAME = "本期销售报告"
LEGACY_METRIC_CODE = "METR-SALES-OLD"

# 查询结果行：按指标编号存放。CSV 未带金额时，按编号从这里取源行。
METRIC_QUERY_ROWS: dict[str, list[dict[str, Any]]] = {
    "METR-SALES-SUM": [
        {"row_id": "SALE-ROW-1", "amount": 10.0},
        {"row_id": "SALE-ROW-2", "amount": 20.0},
    ],
    "METR-SALES-OLD": [
        {"row_id": "SALE-ROW-1", "amount": 10.0},
        {"row_id": "SALE-ROW-2", "amount": 20.0},
    ],
    "METR-AFTERSALE-SUM": [
        {"row_id": "AFTER-SALE-ROW-1", "amount": 50.0},
    ],
}

LEGACY_SOURCE_ROWS = copy.deepcopy(METRIC_QUERY_ROWS[LEGACY_METRIC_CODE])

TRACEABILITY = {
    "identity_rule": "指标编号 + 口径版本 + 口径指纹",
    "source_row_rule": "按指标编号读取查询源行，逐行核对编号与金额",
    "scope_rule": "仅已核验区块进入报告，区块内全部指标通过才启用",
}

SAMPLE_CSV = (
    "模板编号,报告名称,区块,指标编号,指标名称,源行编号,金额\n"
    "REPO-SALES-2026,本期销售报告,销售,METR-SALES-SUM,销售额,SALE-ROW-1,10\n"
    "REPO-SALES-2026,本期销售报告,销售,METR-SALES-SUM,销售额,SALE-ROW-2,20\n"
    "REPO-SALES-2026,本期销售报告,售后,AFTERSALE-LEGACY,售后金额（旧别名）,AFTER-SALE-ROW-1,50\n"
)

ALLOWED_AGGREGATIONS = {
    "sum", "avg", "count", "count_distinct", "max", "min", "p50", "p90", "p99",
}

_AGGREGATION_ALIASES = {
    "sum": "sum", "求和": "sum", "总和": "sum", "合计": "sum",
    "avg": "avg", "平均": "avg", "平均值": "avg", "均值": "avg",
    "count": "count", "计数": "count", "行数": "count",
    "count_distinct": "count_distinct", "countdistinct": "count_distinct",
    "去重计数": "count_distinct", "去重数": "count_distinct",
    "max": "max", "最大值": "max", "极大值": "max",
    "min": "min", "最小值": "min", "极小值": "min",
    "p50": "p50", "中位数": "p50", "median": "p50",
    "p90": "p90", "p99": "p99",
}

_HEADER_ALIASES = {
    "templateid": "template_id", "templatecode": "template_id", "template": "template_id",
    "模板编号": "template_id", "模板id": "template_id",
    "reportname": "report_name", "report": "report_name",
    "报告名称": "report_name", "报告名": "report_name",
    "block": "block", "section": "block", "blockname": "block",
    "区块": "block", "区块名称": "block",
    "metriccode": "metric_code", "metricid": "metric_code", "metric": "metric_code",
    "指标编号": "metric_code", "指标代码": "metric_code", "指标id": "metric_code",
    "metricname": "metric_name", "指标名称": "metric_name", "指标名": "metric_name",
    "sourcerowid": "source_row_id", "sourcerow": "source_row_id", "rowid": "source_row_id",
    "源行编号": "source_row_id", "源行": "source_row_id", "源行id": "source_row_id",
    "amount": "amount", "value": "amount", "金额": "amount", "数值": "amount",
}

_REQUIRED_HEADERS = {"template_id", "report_name", "block", "metric_code"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalise_header(value: str) -> str:
    return "".join(str(value or "").strip().lower().replace("_", "").replace("-", "").split())


def _as_text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _as_amount(value: Any) -> tuple[float | None, bool]:
    """返回 (金额, 是否可解析)。空值当作未提供，交由查询行补齐。"""
    text = _as_text(value).replace(",", "").replace("¥", "").replace("￥", "")
    if not text:
        return None, True
    try:
        return round(float(text), 10), True
    except (TypeError, ValueError):
        return None, False


# ============================================================
# 指标口径目录
# ============================================================

@dataclass(frozen=True)
class MetricInfo:
    """指标目录里一条可计算口径，身份由编号和口径指纹共同确定。"""

    code: str
    name: str
    aggregation: str
    formula: str
    data_source: str
    period: str
    version: int

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            [self.code, self.name, self.aggregation, self.formula, self.data_source, self.period]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def identity(self) -> str:
        return f"{self.code}@v{self.version}"

    @property
    def stable_key(self) -> str:
        return f"{self.identity}#{self.fingerprint}"

    def describe(self) -> dict[str, Any]:
        return {
            "metric_code": self.code,
            "metric_name": self.name,
            "metric_identity": self.identity,
            "stable_key": self.stable_key,
            "definition_version": self.version,
            "definition_fingerprint": self.fingerprint,
            "aggregation": self.aggregation,
            "formula": self.formula,
            "data_source": self.data_source,
            "period": self.period,
        }


def _normalise_aggregation(raw: Any) -> str | None:
    key = _normalise_header(_as_text(raw))
    return _AGGREGATION_ALIASES.get(key, key if key in ALLOWED_AGGREGATIONS else None)


def _metric_catalog() -> dict[str, MetricInfo]:
    """从指标定义模块读取当前可用口径；不可计算的样例行直接跳过。"""
    catalog: dict[str, MetricInfo] = {}
    for row in store.rows(METRIC_MODULE):
        code = _as_text(row.get("指标编号"))
        aggregation = _normalise_aggregation(row.get("聚合方式"))
        if not code or aggregation is None:
            continue
        formula = _as_text(row.get("计算口径")) or f"{aggregation.upper()}(amount)"
        try:
            version = int(row.get("版本") or 1)
        except (TypeError, ValueError):
            version = 1
        catalog[code] = MetricInfo(
            code=code,
            name=_as_text(row.get("指标名称")) or code,
            aggregation=aggregation,
            formula=formula,
            data_source=_as_text(row.get("数据来源")),
            period=_as_text(row.get("统计周期")),
            version=version,
        )
    return catalog


def _aggregate(rows: list[dict[str, Any]], aggregation: str, field: str = "amount") -> float:
    """按口径对源行做确定性计算，报告金额由此实际算出。"""
    if aggregation == "count":
        return float(len(rows))
    raw_values = [row.get(field) for row in rows]
    if aggregation == "count_distinct":
        return float(len({value for value in raw_values if value is not None}))
    values: list[float] = []
    for value in raw_values:
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return 0.0
    if aggregation == "sum":
        result = sum(values)
    elif aggregation == "avg":
        result = sum(values) / len(values)
    elif aggregation == "max":
        result = max(values)
    elif aggregation == "min":
        result = min(values)
    elif aggregation in {"p50", "p90", "p99"}:
        result = _percentile(values, {"p50": 50, "p90": 90, "p99": 99}[aggregation])
    else:
        result = 0.0
    return round(result, 10)


def _percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percent / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + fraction * (ordered[high] - ordered[low])


# ============================================================
# CSV 解析
# ============================================================

@dataclass
class ParsedRow:
    line: int
    template_id: str
    report_name: str
    block: str
    metric_code: str
    metric_name: str
    source_row_id: str
    amount: float | None
    amount_parseable: bool


def _parse_csv(csv_text: str) -> tuple[dict[str, str], list[ParsedRow], list[str]]:
    """解析并规范化模板 CSV；结构性错误直接拒绝，行级问题留给核验环节。"""
    text = (csv_text or "").lstrip("﻿")
    if not text.strip():
        return {}, [], ["CSV 内容为空"]
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {}, [], ["CSV 缺少表头"]
    mapping: dict[str, str] = {}
    for header in reader.fieldnames:
        key = _HEADER_ALIASES.get(_normalise_header(header))
        if key and key not in mapping.values():
            mapping[header] = key
    normalised_headers = set(mapping.values())
    missing = sorted(_REQUIRED_HEADERS - normalised_headers)
    if missing:
        return {}, [], [f"CSV 缺少必需列：{'、'.join(missing)}"]

    rows: list[ParsedRow] = []
    errors: list[str] = []
    for line_no, raw in enumerate(reader, start=2):
        values = {key: raw.get(header, "") for header, key in mapping.items()}
        if not any(_as_text(value) for value in values.values()):
            continue
        amount, parseable = _as_amount(values.get("amount"))
        rows.append(ParsedRow(
            line=line_no,
            template_id=_as_text(values.get("template_id")),
            report_name=_as_text(values.get("report_name")),
            block=_as_text(values.get("block")) or "未标注区块",
            metric_code=_as_text(values.get("metric_code")),
            metric_name=_as_text(values.get("metric_name")),
            source_row_id=_as_text(values.get("source_row_id")),
            amount=amount,
            amount_parseable=parseable,
        ))
        if len(rows) > 5000:
            errors.append("CSV 数据行超过 5000 行上限")
            break
    if not rows and not errors:
        errors.append("CSV 没有可导入的数据行")
    template_ids = {row.template_id for row in rows if row.template_id}
    if len(template_ids) > 1:
        errors.append(f"同一文件包含多个模板编号：{'、'.join(sorted(template_ids))}")
    if rows and not template_ids:
        errors.append("CSV 缺少模板编号")
    for row in rows:
        if not row.report_name:
            errors.append(f"第 {row.line} 行缺少报告名称")
            break
    return {
        "template_id": next(iter(template_ids), ""),
        "report_name": next((row.report_name for row in rows if row.report_name), ""),
    }, rows, errors


# ============================================================
# 核验：区块 -> 指标编号 -> 源行
# ============================================================

def _verify_minimal_metric(metric: MetricInfo) -> str | None:
    if metric.aggregation not in ALLOWED_AGGREGATIONS:
        return f"指标 {metric.code} 的聚合方式不可计算"
    return None


def _verify_metric_rows(
    catalog: dict[str, MetricInfo],
    block: str,
    code: str,
    rows: list[ParsedRow],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """核验一个区块内的一个指标编号，通过后返回可计算条目和待核项。"""
    metric = catalog[code]
    issues: list[dict[str, Any]] = []

    def issue(reason: str, row: ParsedRow | None = None) -> None:
        issues.append({
            "line": row.line if row else None,
            "block": block,
            "metric_code": code,
            "metric_name": _as_text(row.metric_name) if row else metric.name,
            "source_row_id": _as_text(row.source_row_id) if row else "",
            "reason": reason,
        })

    definition_issue = _verify_minimal_metric(metric)
    if definition_issue:
        issue(definition_issue)

    query_rows = METRIC_QUERY_ROWS.get(code)
    if not query_rows:
        issue(f"指标 {code} 没有可用的查询源行")
        return None, issues

    query_index = {str(item.get("row_id")): item for item in query_rows}
    provided: dict[str, float] = {}
    for row in rows:
        if not row.amount_parseable:
            issue("源行金额无法解析", row)
            continue
        if not row.source_row_id:
            issue("缺少源行编号，无法关联查询行", row)
            continue
        if row.source_row_id not in query_index:
            issue(f"源行 {row.source_row_id} 不在指标 {code} 的查询范围内", row)
            continue
        expected = float(query_index[row.source_row_id].get("amount", 0.0))
        amount = expected if row.amount is None else row.amount
        if abs(amount - expected) > 1e-9:
            issue(
                f"源行 {row.source_row_id} 金额 {amount:g} 与查询结果 {expected:g} 不一致",
                row,
            )
            continue
        if row.source_row_id in provided:
            issue(f"源行 {row.source_row_id} 重复出现", row)
            continue
        provided[row.source_row_id] = expected

    missing = [row_id for row_id in query_index if row_id not in provided]
    if missing:
        issue(f"源行不完整，缺少 {'、'.join(sorted(missing))}")
    if issues:
        return None, issues

    source_rows = [
        {"row_id": row_id, "amount": provided[row_id]}
        for row_id in query_index
        if row_id in provided
    ]
    value = _aggregate(source_rows, metric.aggregation)
    scope = {
        "block": block,
        "metric_code": code,
        "metric_identity": metric.identity,
        "stable_key": metric.stable_key,
        "aggregation": metric.aggregation,
        "formula": metric.formula,
        "data_source": metric.data_source,
        "period": metric.period,
        "source_row_ids": [item["row_id"] for item in source_rows],
        "row_count": len(source_rows),
        "query_scope": f"{code}:{','.join(item['row_id'] for item in source_rows)}",
    }
    entry = {
        **metric.describe(),
        "block": block,
        "value": value,
        "verified": True,
        "source_rows": source_rows,
        "calculation_scope": scope,
    }
    return entry, []


def _verify_blocks(
    catalog: dict[str, MetricInfo],
    rows: list[ParsedRow],
) -> list[dict[str, Any]]:
    """按区块分组核验，区块整体通过才允许进入可计算清单。"""
    ordered: dict[str, list[ParsedRow]] = {}
    for row in rows:
        ordered.setdefault(row.block, []).append(row)

    blocks: list[dict[str, Any]] = []
    for block, block_rows in ordered.items():
        entries: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        grouped: dict[str, list[ParsedRow]] = {}
        for row in block_rows:
            grouped.setdefault(row.metric_code, []).append(row)

        for code, code_rows in grouped.items():
            if not code:
                for row in code_rows:
                    candidates = sorted(
                        other.code for other in catalog.values()
                        if row.metric_name and other.name == row.metric_name
                    )
                    reason = "缺少可靠指标编号，无法判定口径"
                    if candidates:
                        reason += f"；同名编号候选：{'、'.join(candidates)}"
                    issues.append({
                        "line": row.line, "block": block, "metric_code": "",
                        "metric_name": row.metric_name,
                        "source_row_id": row.source_row_id,
                        "reason": reason,
                        "name_candidate_codes": candidates,
                    })
                continue
            metric = catalog.get(code)
            if metric is None:
                for row in code_rows:
                    candidates = sorted(
                        other.code for other in catalog.values()
                        if row.metric_name and other.name == row.metric_name
                    )
                    reason = f"未知指标编号 {code}，无法判定口径"
                    if candidates:
                        reason += f"；同名编号候选：{'、'.join(candidates)}"
                    issues.append({
                        "line": row.line, "block": block, "metric_code": code,
                        "metric_name": row.metric_name or "",
                        "source_row_id": row.source_row_id,
                        "reason": reason,
                        "name_candidate_codes": candidates,
                    })
                continue
            entry, metric_issues = _verify_metric_rows(catalog, block, code, code_rows)
            issues.extend(metric_issues)
            if entry is not None:
                entries.append(entry)

        verified = not issues and bool(entries)
        blocks.append({
            "block": block,
            "status": "已核验" if verified else "待核",
            "verified": verified,
            "metrics": entries if verified else [],
            "verified_entries": entries,
            "pending_items": issues,
            "pending_reasons": sorted({item["reason"] for item in issues}),
            "preview_value": round(sum(item["value"] for item in entries), 10),
        })
    return blocks


# ============================================================
# 模板版本仓库
# ============================================================

@dataclass
class ImportRecord:
    import_id: str
    fingerprint: str
    template_id: str
    report_name: str
    version: int
    status: str
    created_at: str
    confirmed_at: str | None
    activation_scope: str | None
    confirmations: int
    blocks: list[dict[str, Any]]
    replay_count: int = 0

    @property
    def pending_blocks(self) -> list[str]:
        return [block["block"] for block in self.blocks if not block["verified"]]

    @property
    def verified_blocks(self) -> list[str]:
        return [block["block"] for block in self.blocks if block["verified"]]


class ReportTemplateRegistry:
    """内存中的模板版本仓库，锁保证同一次导入的确认只发生一次。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._imports: dict[str, ImportRecord] = {}
        self._versions: dict[str, list[dict[str, Any]]] = {}
        self._legacy: dict[str, dict[str, Any]] = {}

    def reset(self) -> None:
        """清空运行期导入与版本，回到只有旧模板基线；测试和演示重置共用。"""
        with self._lock:
            self._imports.clear()
            self._versions.clear()
            self._legacy.clear()
            rows = store.rows(TEMPLATE_MODULE)
            rows[:] = [dict(row) for row in SEED_ROWS.get(TEMPLATE_MODULE, [])]

    # -- 基础查询 -------------------------------------------------

    def _legacy_version(self, template_id: str) -> dict[str, Any]:
        if template_id in self._legacy:
            return copy.deepcopy(self._legacy[template_id])
        catalog = _metric_catalog()
        metric = catalog.get(LEGACY_METRIC_CODE)
        if metric is None:
            metric = MetricInfo(
                code=LEGACY_METRIC_CODE, name="销售额", aggregation="max",
                formula="MAX(amount)", data_source="doris", period="daily", version=1,
            )
        source_rows = copy.deepcopy(LEGACY_SOURCE_ROWS)
        value = _aggregate(source_rows, metric.aggregation)
        scope = {
            "block": "销售",
            "metric_code": metric.code,
            "metric_identity": metric.identity,
            "stable_key": metric.stable_key,
            "aggregation": metric.aggregation,
            "formula": metric.formula,
            "data_source": metric.data_source,
            "period": metric.period,
            "source_row_ids": [item["row_id"] for item in source_rows],
            "row_count": len(source_rows),
            "query_scope": f"{metric.code}:{','.join(item['row_id'] for item in source_rows)}",
        }
        entry = {
            **metric.describe(),
            "block": "销售",
            "value": value,
            "verified": True,
            "source_rows": source_rows,
            "calculation_scope": scope,
        }
        version = {
            "template_id": template_id,
            "report_name": LEGACY_REPORT_NAME,
            "version": 1,
            "version_label": "v1",
            "status": "已生效",
            "activation_scope": "legacy",
            "fingerprint": "legacy",
            "import_id": "LEGACY",
            "created_at": "2026-01-01T00:00:00+00:00",
            "confirmed_at": "2026-01-01T00:00:00+00:00",
            "blocks": [{
                "block": "销售",
                "status": "已核验",
                "verified": True,
                "metrics": [entry],
                "verified_entries": [entry],
                "pending_items": [],
                "pending_reasons": [],
                "preview_value": value,
            }],
            "same_name_codes": _same_name_codes(catalog, [entry]),
        }
        self._legacy[template_id] = copy.deepcopy(version)
        self._versions.setdefault(template_id, []).append(copy.deepcopy(version))
        return version

    def _version_entries(self, template_id: str) -> list[dict[str, Any]]:
        if template_id == LEGACY_TEMPLATE_ID:
            self._legacy_version(template_id)
        return self._versions.setdefault(template_id, [])

    def _official(self, template_id: str) -> dict[str, Any] | None:
        """当前正式使用的版本：只认已生效的版本，未确认的草稿永不顶上。"""
        versions = self._version_entries(template_id)
        active = [item for item in versions if item["status"] == "已生效"]
        if active:
            # 最新确认的一版是当前生效版本。
            return active[-1]
        return None

    # -- 导入 -----------------------------------------------------

    def import_csv(self, csv_text: str, file_name: str = "") -> dict[str, Any]:
        meta, rows, errors = _parse_csv(csv_text)
        if errors:
            return {"ok": False, "message": "；".join(errors), "errors": errors, "import": None}
        catalog = _metric_catalog()
        blocks = _verify_blocks(catalog, rows)
        fingerprint = _fingerprint(meta, rows)
        now = _now()
        with self._lock:
            existing = self._imports.get(fingerprint)
            if existing is not None:
                existing.replay_count += 1
                snapshot = self._snapshot(existing, catalog, replayed=True)
                return {
                    "ok": True,
                    "message": (
                        f"同一份文件已导入过，沿用模板 v{existing.version}，未重复创建版本"
                    ),
                    "import": snapshot,
                }
            versions = self._version_entries(meta["template_id"])
            version_no = max((item["version"] for item in versions), default=0) + 1
            import_id = "IMP-" + fingerprint[:12].upper()
            record = ImportRecord(
                import_id=import_id,
                fingerprint=fingerprint,
                template_id=meta["template_id"],
                report_name=meta["report_name"],
                version=version_no,
                status="待确认",
                created_at=now,
                confirmed_at=None,
                activation_scope=None,
                confirmations=0,
                blocks=blocks,
            )
            self._imports[fingerprint] = record
            version = {
                "template_id": record.template_id,
                "report_name": record.report_name,
                "version": version_no,
                "version_label": f"v{version_no}",
                "status": "待确认",
                "activation_scope": None,
                "fingerprint": fingerprint,
                "import_id": import_id,
                "created_at": now,
                "confirmed_at": None,
                "blocks": copy.deepcopy(blocks),
                "same_name_codes": _same_name_codes(catalog, _entries(blocks)),
            }
            versions.append(version)
            snapshot = self._snapshot(record, catalog, replayed=False)
            snapshot["file_name"] = file_name
            return {
                "ok": True,
                "message": f"已导入模板 {record.template_id} v{version_no}，等待确认启用范围",
                "import": snapshot,
            }

    # -- 确认 -----------------------------------------------------

    def get_import(self, import_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = next(
                (item for item in self._imports.values() if item.import_id == import_id),
                None,
            )
            if record is None:
                return None
            return self._snapshot(record, _metric_catalog(), replayed=False)

    def list_imports(self) -> list[dict[str, Any]]:
        with self._lock:
            catalog = _metric_catalog()
            items = [
                self._snapshot(record, catalog, replayed=False)
                for record in self._imports.values()
            ]
        items.sort(key=lambda item: item["created_at"])
        return items

    def confirm(self, import_id: str, scope: str) -> dict[str, Any]:
        normalised_scope = _normalise_scope(scope)
        if normalised_scope is None:
            return {
                "ok": False,
                "message": "启用范围只能是「部分启用」或「整份启用」",
                "import": None,
            }
        with self._lock:
            record = next(
                (item for item in self._imports.values() if item.import_id == import_id),
                None,
            )
            if record is None:
                return {"ok": False, "message": f"导入记录 {import_id} 不存在", "import": None}
            catalog = _metric_catalog()
            if record.status == "已确认":
                return {
                    "ok": True,
                    "message": (
                        f"该次导入已确认，继续使用模板 v{record.version}，未重复生成版本"
                    ),
                    "import": self._snapshot(record, catalog, replayed=True),
                    "report": self.build_report(record.template_id),
                }
            pending = record.pending_blocks
            if normalised_scope == "whole" and pending:
                return {
                    "ok": False,
                    "message": (
                        f"整份启用与待核区块冲突：{'、'.join(pending)} 仍缺证据；"
                        "请补齐证据后重传，或改用部分启用"
                    ),
                    "scope_conflict": {
                        "requested_scope": "whole",
                        "pending_blocks": pending,
                        "allowed_scopes": ["partial"],
                    },
                    "import": self._snapshot(record, catalog, replayed=False),
                }
            if normalised_scope == "partial" and not record.verified_blocks:
                return {
                    "ok": False,
                    "message": "没有任何已核验区块，无法部分启用",
                    "import": self._snapshot(record, catalog, replayed=False),
                }
            now = _now()
            record.status = "已确认"
            record.confirmed_at = now
            record.activation_scope = normalised_scope
            record.confirmations += 1
            for version in self._versions[record.template_id]:
                if version["status"] == "已生效":
                    version["status"] = "已替代"
            target = next(
                item for item in self._versions[record.template_id]
                if item["import_id"] == record.import_id
            )
            target["status"] = "已生效"
            target["activation_scope"] = normalised_scope
            target["confirmed_at"] = now
            target["blocks"] = copy.deepcopy(record.blocks)
            self._upsert_template_row(record)
            snapshot = self._snapshot(record, catalog, replayed=False)
            return {
                "ok": True,
                "message": (
                    f"模板 {record.template_id} v{record.version} 已按"
                    f"{'部分启用' if normalised_scope == 'partial' else '整份启用'}生效"
                ),
                "import": snapshot,
                "report": self.build_report(record.template_id),
            }

    # -- 报告与清单 -----------------------------------------------

    def _snapshot(
        self,
        record: ImportRecord,
        catalog: dict[str, MetricInfo],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        pending = record.pending_blocks
        verified = record.verified_blocks
        return {
            "import_id": record.import_id,
            "fingerprint": record.fingerprint,
            "template_id": record.template_id,
            "report_name": record.report_name,
            "template_version": record.version,
            "version_label": f"v{record.version}",
            "status": record.status,
            "activation_scope": record.activation_scope,
            "created_at": record.created_at,
            "confirmed_at": record.confirmed_at,
            "confirmations": record.confirmations,
            "replayed": replayed,
            "replay_count": record.replay_count,
            "blocks": copy.deepcopy(record.blocks),
            "verified_blocks": verified,
            "pending_blocks": pending,
            "pending_items": [
                item for block in record.blocks for item in block["pending_items"]
            ],
            "verified_metrics": [
                metric for block in record.blocks if block["verified"]
                for metric in block["metrics"]
            ],
            "scope_conflict": (
                {
                    "message": (
                        "销售等已核验区块可部分启用，但整份启用被待核区块阻断"
                    ),
                    "requested_scope": None,
                    "pending_blocks": pending,
                    "allowed_scopes": ["partial"] if verified else [],
                    "whole_enabled": False,
                    "partial_enabled": bool(verified),
                }
                if pending else None
            ),
            "same_name_codes": _same_name_codes(catalog, _entries(record.blocks)),
        }

    def build_report(self, template_id: str = LEGACY_TEMPLATE_ID) -> dict[str, Any]:
        with self._lock:
            versions = self._version_entries(template_id)
            official = self._official(template_id)
            drafts = [item for item in versions if item["status"] == "待确认"]
            draft = drafts[-1] if drafts else None
            candidate_entries = _entries(draft["blocks"]) if draft is not None else []
            candidate_entries = [copy.deepcopy(entry) for entry in candidate_entries]
            candidate_entries.sort(key=lambda item: (item["block"], item["metric_code"]))

            if official is None:
                return {
                    "template_id": template_id,
                    "report_name": draft["report_name"] if draft is not None else "",
                    "template_version": 0,
                    "template_version_label": "—",
                    "activation_scope": None,
                    "status": "未导入",
                    "official_since": None,
                    "metrics": [],
                    "total_amount": 0.0,
                    "pending_blocks": [
                        block["block"] for block in (draft["blocks"] if draft else [])
                        if not block["verified"]
                    ],
                    "excluded_metrics": [
                        item for block in (draft["blocks"] if draft else [])
                        if not block["verified"] for item in block["pending_items"]
                    ],
                    "draft": self._snapshot(
                        next(item for item in self._imports.values()
                             if item.import_id == draft["import_id"]),
                        _metric_catalog(), replayed=False,
                    ) if draft is not None else None,
                    "candidate_metrics": candidate_entries,
                    "candidate_total_amount": round(
                        sum(item["value"] for item in candidate_entries), 10),
                    "same_name_codes": _same_name_codes(
                        _metric_catalog(), candidate_entries),
                    "traceability": dict(TRACEABILITY),
                }
            official_entries = _entries(official["blocks"])
            metrics = [copy.deepcopy(entry) for entry in official_entries]
            metrics.sort(key=lambda item: (item["block"], item["metric_code"]))
            for metric in metrics:
                metric["template_version"] = official["version"]
                metric["template_version_label"] = official["version_label"]
                metric["activation_scope"] = official["activation_scope"]

            pending_source = draft if draft is not None else official
            pending_blocks = [
                block["block"] for block in pending_source["blocks"] if not block["verified"]
            ]
            excluded = [
                item for block in pending_source["blocks"] if not block["verified"]
                for item in block["pending_items"]
            ]
            return {
                "template_id": template_id,
                "report_name": official["report_name"],
                "template_version": official["version"],
                "template_version_label": official["version_label"],
                "activation_scope": official["activation_scope"],
                "status": official["status"],
                "official_since": official.get("confirmed_at"),
                "metrics": metrics,
                "total_amount": round(sum(item["value"] for item in metrics), 10),
                "pending_blocks": pending_blocks,
                "excluded_metrics": excluded,
                "draft": self._snapshot(
                    next(item for item in self._imports.values()
                         if item.import_id == draft["import_id"]),
                    _metric_catalog(), replayed=False,
                ) if draft is not None else None,
                "candidate_metrics": candidate_entries,
                "candidate_total_amount": round(
                    sum(item["value"] for item in candidate_entries), 10),
                "same_name_codes": _same_name_codes(
                    _metric_catalog(), metrics + candidate_entries),
                "traceability": dict(TRACEABILITY),
            }

    def computable_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            catalog = _metric_catalog()
            self._version_entries(LEGACY_TEMPLATE_ID)
            templates: list[dict[str, Any]] = []
            for template_id in sorted(self._versions):
                for version in self._versions[template_id]:
                    metrics = [
                        copy.deepcopy(entry) for block in version["blocks"]
                        if block["verified"] for entry in block["metrics"]
                    ]
                    for metric in metrics:
                        metric["template_version"] = version["version"]
                        metric["template_version_label"] = version["version_label"]
                        metric["activation_scope"] = version["activation_scope"]
                    templates.append({
                        "template_id": template_id,
                        "report_name": version["report_name"],
                        "template_version": version["version"],
                        "version_label": version["version_label"],
                        "status": version["status"],
                        "activation_scope": version["activation_scope"],
                        "computable": bool(metrics),
                        "fully_verified": not any(
                            not block["verified"] for block in version["blocks"]),
                        "verified_blocks": [
                            block["block"] for block in version["blocks"] if block["verified"]
                        ],
                        "pending_blocks": [
                            block["block"] for block in version["blocks"] if not block["verified"]
                        ],
                        "metrics": metrics,
                        "total_amount": round(sum(item["value"] for item in metrics), 10),
                        "same_name_codes": version.get(
                            "same_name_codes", _same_name_codes(catalog, metrics)),
                    })
            return templates

    # -- 模板列表同步 ---------------------------------------------

    def _upsert_template_row(self, record: ImportRecord) -> None:
        rows = store.rows(TEMPLATE_MODULE)
        codes = sorted({
            entry["metric_code"] for block in record.blocks if block["verified"]
            for entry in block["metrics"]
        })
        values = {
            "模板编号": record.template_id,
            "报告名称": record.report_name,
            "引用指标": "、".join(codes),
            "排版格式": "pdf",
            "生成周期": "weekly",
            "推送渠道": "email",
            "最近生成": record.confirmed_at or _now(),
            "模板状态": "已发布",
            "模板版本": record.version,
            "启用范围": record.activation_scope or "",
            "status": "已发布",
            "pending": False,
            "abnormal": False,
        }
        for row in rows:
            if row.get("模板编号") == record.template_id:
                row.update(values)
                return
        row_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        rows.append({"id": row_id, **values})


def _normalise_scope(scope: Any) -> str | None:
    key = _normalise_header(_as_text(scope))
    if key in {"partial", "部分启用", "部分"}:
        return "partial"
    if key in {"whole", "整份启用", "整份", "全部启用"}:
        return "whole"
    return None


def _entries(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        entry for block in blocks if block["verified"] for entry in block["metrics"]
    ]


def _same_name_codes(
    catalog: dict[str, MetricInfo], entries: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """同名不同编号必须各自保留身份，这里按组内编号升序给出唯一一条同名对照。"""
    referenced = {entry.get("metric_code") for entry in entries if entry.get("metric_code")}
    by_name: dict[str, list[str]] = {}
    for metric in catalog.values():
        by_name.setdefault(metric.name, []).append(metric.code)
    result: dict[str, list[str]] = {}
    for name, codes in by_name.items():
        if len(codes) < 2 or not referenced.intersection(codes):
            continue
        ordered = sorted(codes)
        result[ordered[0]] = ordered[1:]
    return result


def _fingerprint(meta: dict[str, str], rows: list[ParsedRow]) -> str:
    canonical = {
        "template_id": meta["template_id"],
        "report_name": meta["report_name"],
        "rows": [
            [
                row.block, row.metric_code, row.metric_name, row.source_row_id,
                row.amount, row.amount_parseable,
            ]
            for row in rows
        ],
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


registry = ReportTemplateRegistry()


# ============================================================
# 模块服务
# ============================================================

class ReportBuilderService(BaseModuleService):
    MODULE = "report_builder"
    ENTITY = "报告模板"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["模板编号", "报告名称", "引用指标", "排版格式", "生成周期", "推送渠道", "最近生成", "模板状态"]
    REQUIRED_FIELDS = ["模板编号", "报告名称", "引用指标"]
    STATUS_ORDER = ["草稿", "已发布", "格式异常", "已停用"]
    ACTION_RULES = {"发布模板": "已发布", "标记异常": "格式异常", "停用模板": "已停用"}
    NEGATIVE_ACTIONS = ["停用模板"]
