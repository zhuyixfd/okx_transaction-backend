
from pydantic import BaseSettings

STATUS = "localhost"


class DbConfig(BaseSettings):
    """
        Handles the variables for database configuration
    """
    if STATUS == "localhost":
        POSTGRES_USER: str = ""
        POSTGRES_PASSWORD: str = ""
        POSTGRES_SERVER: str = ""
        POSTGRES_PORT: str = ""
        POSTGRES_DB: str = ""

    elif STATUS == "staging":
        POSTGRES_USER: str = ""
        POSTGRES_PASSWORD: str = ""
        POSTGRES_SERVER: str = ""
        POSTGRES_PORT: str = ""
        POSTGRES_DB: str = ""


config = DbConfig()
