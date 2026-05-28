from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24hr
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    CAMERA_BACKEND_URL: str = "https://user.symotus.com/api"

    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: Optional[str] = None

    LINE_CLIENT_ID: Optional[str] = None
    LINE_CLIENT_SECRET: Optional[str] = None
    LINE_REDIRECT_URI: Optional[str] = None

    FRONTEND_URL: str = "https://admin.symotus.com"
    INVITE_EXPIRE_HOURS: int = 168

    class Config:
        env_file = ".env"

settings = Settings()
