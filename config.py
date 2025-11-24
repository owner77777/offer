# config.py

from pydantic import BaseModel
from typing import Union

# --- КОНФИГУРАЦИЯ ---
# ! ВАЖНО: Рекомендуется вынести BOT_TOKEN в переменную окружения Render.
# ! Для этого бота я оставил токен здесь, но в реальном проекте используйте os.environ.get('BOT_TOKEN')
class Config(BaseModel):
    # Замените на os.environ.get('BOT_TOKEN') в реальном проекте
    BOT_TOKEN: str = "8346884521:AAGvOZdAJA4O3ohHzB2lFI5oTZn3zWyxLY" 
    OWNER_ID: int = 6493670021
    CHANNEL_PREDLOZHKA_ID: Union[int, str] = -1003287891557
    CHANNEL_FINAL_ID: Union[int, str] = -1003479497567
    CHANNEL_LOG_ID: Union[int, str] = -1003494833745
    MAX_POSTS_PER_DAY: int = 5
    TIMEZONE_NAME: str = "Europe/Moscow"
    DB_NAME: str = "bot_data.db"
    LOG_FILE: str = "bot_log.log"

SETTINGS = Config()
# В Render переменная окружения PORT будет автоматически предоставлена.
RENDER_PORT = 8080 # Вы можете использовать любой порт, например 8080.
