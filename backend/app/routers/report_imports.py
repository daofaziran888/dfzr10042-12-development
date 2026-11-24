"""报告模板 CSV 导入、核验确认与指标报告接口。

资源路由里的 `/{module}/{entry_id}` 是动态路径，这里的固定子路径必须
在它之前注册，否则会被当成条目编号抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.schemas import ActionResult, ReportImportPayload, ReportScopePayload
from app.services.report_builder import SAMPLE_CSV, registry

router = APIRouter(prefix="/api", tags=["报告导入"])


def _require_import(import_id: str) -> None:
    if registry.get_import(import_id) is None:
        raise HTTPException(status_code=404, detail=f"导入记录 {import_id} 不存在")


@router.get("/report_builder/imports")
def list_imports() -> dict[str, Any]:
    return {"items": registry.list_imports()}


@router.post("/report_builder/imports", response_model=ActionResult)
async def create_import(request: Request) -> ActionResult:
    """接收模板 CSV：优先读 JSON 里的 csv_text，也接受直接以 text/csv 推送的请求体。"""
    raw = await request.body()
    content_type = request.headers.get("content-type", "")
    csv_text = ""
    file_name = request.headers.get("x-file-name", "")
    if "application/json" in content_type:
        try:
            payload = ReportImportPayload.model_validate_json(raw or b"{}")
            csv_text = payload.csv_text
            file_name = payload.file_name or file_name
        except ValueError:
            return ActionResult(ok=False, message="JSON 请求体缺少 csv_text")
    else:
        csv_text = raw.decode("utf-8-sig", errors="replace")
    if not csv_text.strip():
        return ActionResult(ok=False, message="未收到 CSV 内容")
    result = registry.import_csv(csv_text, file_name)
    entry = result.get("import") or {}
    return ActionResult(
        ok=bool(result["ok"]),
        message=result["message"],
        entry=entry if result["ok"] else None,
        data={"import": entry} if result["ok"] else None,
    )


@router.get("/report_builder/sample-csv", response_class=PlainTextResponse)
def sample_csv() -> PlainTextResponse:
    return PlainTextResponse(
        SAMPLE_CSV, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="report_template_sample.csv"'},
    )


@router.get("/report_builder/computable-templates")
def computable_templates() -> dict[str, Any]:
    return {"items": registry.computable_templates()}


@router.get("/report_builder/report")
def report(template_id: str = Query(default="REPO-SALES-2026")) -> dict[str, Any]:
    return registry.build_report(template_id)


@router.get("/report_builder/imports/{import_id}")
def get_import(import_id: str) -> dict[str, Any]:
    _require_import(import_id)
    return registry.get_import(import_id)


@router.post("/report_builder/imports/{import_id}/confirm", response_model=ActionResult)
def confirm_import(import_id: str, payload: ReportScopePayload) -> ActionResult:
    _require_import(import_id)
    result = registry.confirm(import_id, payload.scope)
    entry = result.get("import") or {}
    return ActionResult(
        ok=bool(result["ok"]),
        message=result["message"],
        entry=entry if result["ok"] else None,
        data={
            "import": entry,
            **({"report": result["report"]} if result.get("report") else {}),
        } if result["ok"] else None,
    )
