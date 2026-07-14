import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from app.api import auth, connections, products, suppliers
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import User
from app.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


async def _bootstrap_user() -> None:
    settings = get_settings()
    if not settings.bootstrap_user_email or not settings.bootstrap_user_password:
        return

    async with SessionLocal() as session:
        count = await session.scalar(select(func.count()).select_from(User))
        if count:
            return
        session.add(
            User(
                email=settings.bootstrap_user_email,
                password_hash=hash_password(settings.bootstrap_user_password),
            )
        )
        await session.commit()
        log.info("Создан первый пользователь: %s", settings.bootstrap_user_email)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _bootstrap_user()
    yield


app = FastAPI(title="4drop — интеграция 4tochki ↔ Wildberries/Ozon", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(suppliers.router)
app.include_router(connections.router)
app.include_router(products.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
