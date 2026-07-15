from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://4drop:4drop@db:5432/4drop"
    redis_url: str = "redis://redis:6379/0"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_ttl_minutes: int = 60 * 12

    # Fernet-ключ. Без него приложение не стартует: хранить доступы в открытом виде нельзя.
    secrets_key: str

    fourtochki_wsdl: str = "http://api-b2b.4tochki.ru/WCF/ClientService.svc?wsdl"
    fourtochki_wsdl_cache: str = "/cache/4tochki-wsdl.db"

    # Лимиты 4tochki на размер списка. В WSDL их нет — найдены замером, обе границы
    # жёсткие: сверх них API отвечает «[51] Превышен лимит элементов».
    # У методов лимиты РАЗНЫЕ, общий батч ставить нельзя.
    fourtochki_price_batch_size: int = 2000  # GetGoodsPriceRestByCode
    fourtochki_goods_batch_size: int = 200   # GetGoodsInfo

    # Параллельные SOAP-запросы. Замер: 1 поток ~760 кодов/с, 4 ~1730, 8 ~1770 —
    # выше 6 прирост упирается в их сервер, а нагрузку мы наращиваем зря.
    fourtochki_concurrency: int = 6

    bootstrap_user_email: str | None = None
    bootstrap_user_password: str | None = None

    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
