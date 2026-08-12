from fastapi import FastAPI

from investment_town.api.router import router

app = FastAPI(
    title="Investment Town API",
    version="0.1.0",
    description="Control plane and multi-agent investment research backend.",
)
app.include_router(router)


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "investment-town", "docs": "/docs"}
