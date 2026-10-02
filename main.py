from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import Base, engine
from app.web.routes import router
from app.isdb.manager import eit_manager


Base.metadata.create_all(bind=engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start all configured EIT broadcasters.
    eit_manager.start()

    try:
        yield

    finally:
        # Clean shutdown of all EIT broadcasters.
        eit_manager.stop()


app = FastAPI(
    title="ISDB-Tb EPG",
    version="0.2.0",
    lifespan=lifespan,
)

app.mount(
    "/static",
    StaticFiles(directory="app/web/static"),
    name="static",
)

app.include_router(router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "0.2.0",
        "eit": eit_manager.status(),
    }
