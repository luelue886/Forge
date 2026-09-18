from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(jobs_root: Path | None = None, llm_client_factory=None,
               com_export: bool = True) -> FastAPI:
    """jobs_root / llm_client_factory / com_export 供测试注入；生产默认走 DATA_DIR 与真实 LLM。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.services.jobs import JobManager

        app.state.jobs = JobManager(root=jobs_root)
        app.state.llm_client_factory = llm_client_factory
        app.state.com_export = com_export
        yield

    app = FastAPI(title="AIGC 文档仿写 PPT Agent", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    from app.api.routes_jobs import router

    app.include_router(router)
    return app


app = create_app()
