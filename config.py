from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60   # 1hr
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # CI build 蓋上的版本碼（YYYYMMDDHHMM）；未設時為 "dev"。供 /version 回傳給前端比對是否更新
    BUILD_VERSION: str = "dev"

    CAMERA_BACKEND_URL: str = "https://user.symotus.com/api"

    # Spark Timelapse Pipeline Service（縮時影片處理）
    SPARK_API_URL: str = "https://user.symotus.com/spark"
    SPARK_API_KEY: str = "5c036bfc702e1a7998a488e1cbed2f606d67b570586e3def24dba9bbf6a18016"
    # 本服務對外網址（給 Spark 完成後 server-to-server callback 用）
    PUBLIC_BASE_URL: str = "https://auth.symotus.com"

    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    GOOGLE_REDIRECT_URI: Optional[str] = None

    # LINE OAuth - 讀取 LINE_CHANNEL_ID / LINE_CHANNEL_SECRET
    LINE_CHANNEL_ID: Optional[str] = None
    LINE_CHANNEL_SECRET: Optional[str] = None
    LINE_REDIRECT_URI: str = "https://reseller.symotus.com:9443/auth-api/auth/line/callback"

    # alias 讓舊程式碼也能用 LINE_CLIENT_ID
    @property
    def LINE_CLIENT_ID(self) -> Optional[str]:
        return self.LINE_CHANNEL_ID

    @property
    def LINE_CLIENT_SECRET(self) -> Optional[str]:
        return self.LINE_CHANNEL_SECRET

    FRONTEND_URL: str = "https://reseller.symotus.com:9443"
    INVITE_EXPIRE_HOURS: int = 168

    class Config:
        env_file = ".env"

settings = Settings()
