from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from investment_town.api.router import router
from investment_town.control import ProjectControl, ProjectStore
from investment_town.core.config import Settings, settings

DASHBOARD = Path(__file__).resolve().parents[2] / "static" / "index.html"


def create_app(app_settings: Settings = settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = ProjectStore(app_settings.database_path)
        store.register("investment-town", "Investment Town")
        app.state.settings = app_settings
        app.state.control = ProjectControl(store)
        yield
        store.close()

    application = FastAPI(
        title="Investment Town API",
        version="0.2.0",
        description="Control plane and multi-agent investment research backend.",
        lifespan=lifespan,
    )
    application.include_router(router)

    @application.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(DASHBOARD)

    return application


app = create_app()
