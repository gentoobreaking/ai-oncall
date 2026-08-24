"""FastAPI 應用工廠：唯讀讀頁面（僅 GET），資料源僅 readapi。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from oncall_ui.client import ReadApiClient, default_readapi_url

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# 路由白名單（唯讀斷言測試據此掃描）
GET_ROUTES: tuple[str, ...] = ("/healthz", "/incidents", "/incidents/{incident_id}", "/runbooks")


def create_app(readapi_url: str | None = None) -> FastAPI:
    app = FastAPI(title="oncall-ui", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    client = ReadApiClient(base_url=readapi_url or default_readapi_url())
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request=request, name="incidents.html", context={"request_url": "/incidents"}
        )

    @app.get("/incidents", response_class=HTMLResponse)
    def incidents(request: Request, status: str = "", q: str = "", page: int = 1):
        data = client.incidents(status=status or None, page=page)
        items = data["items"]
        if q:
            ql = q.lower()
            items = [
                i
                for i in items
                if ql in (i.get("title") or "").lower() or ql in (i.get("id") or "").lower()
            ]
        return templates.TemplateResponse(
            request=request,
            name="incidents.html",
            context={
                "items": items,
                "total": data["total"],
                "status": status,
                "q": q,
                "page": page,
            },
        )

    @app.get("/incidents/{incident_id}", response_class=HTMLResponse)
    def incident_detail(request: Request, incident_id: str):
        detail = client.incident(incident_id)
        if detail is None:
            return HTMLResponse("<h1>404 not found</h1>", status_code=404)
        action_items = client.action_items()
        related = [a for a in action_items.get("items", []) if a.get("incident_id") == incident_id]
        return templates.TemplateResponse(
            request=request,
            name="incident_detail.html",
            context={"inc": detail, "action_items": related},
        )

    @app.get("/runbooks", response_class=HTMLResponse)
    def runbooks(request: Request):
        data = client.runbooks()
        stats = client.stats()
        return templates.TemplateResponse(
            request=request,
            name="runbooks.html",
            context={"runbooks": data["items"], "stats": stats},
        )

    return app
