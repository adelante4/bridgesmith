import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # must run before any module reads API key env vars

from fastapi import FastAPI  # noqa: E402

from app.db import init_db  # noqa: E402
from app.observability import init_langfuse  # noqa: E402
from app.routes import context, generate  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    init_langfuse()
    yield


app = FastAPI(
    title="Automated Generative Marketing Collateral",
    description="Generates tailored B2B marketing article JSON from sender/receiver company PDF context.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(context.router)
app.include_router(generate.router)
