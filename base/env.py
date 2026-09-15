from functools import lru_cache
from pathlib import Path

from decouple import Config, RepositoryEnv, config as decouple_config


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE_PATH = BASE_DIR / ".env"


@lru_cache()
def get_config():
    if ENV_FILE_PATH.exists():
        return Config(RepositoryEnv(str(ENV_FILE_PATH)))
    return decouple_config


config = get_config()
