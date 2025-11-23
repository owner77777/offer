import os
from pydantic import BaseModel
from typing import Union


class Config(BaseModel):
    BOT_TOKEN: str = "8346884521:AAGvOZdAJA4O3ohHzB2lFI5oTZnz3lWyxLY"
    OWNER_ID: int = 6493670021
    CHANNEL_PREDLOZHKA_ID: Union[int, str] = -1003287891557
    CHANNEL_FINAL_ID: Union[int, str] = -1003479497567
    CHANNEL_LOG_ID: Union[int, str] = -1003494833745
    MAX_POSTS_PER_DAY: int = 5
    TIMEZONE_NAME: str = "Europe/Moscow"
    DB_NAME: str = "bot_data.db"
    LOG_FILE: str = "bot_log.log"
    PORT: int = int(os.getenv("PORT", 8080))  # Для Render


SETTINGS = Config()
