"""Improvement service package."""

__app__ = "Language Creation - Text Generation App"
__author__ = "AI Apps GBB Team"
__version__ = "0.1.0"

import os
import logging
import sys
from logging.handlers import RotatingFileHandler

from .improvement import improve_image  # noqa: F401
from .main import app  # noqa: F401

try:  # pragma: no cover - optional dependency for local tooling
    from dotenv import load_dotenv  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - provide fallback
    def load_dotenv(*args, **kwargs):  # type: ignore[override]
        return False


def setup_logging() -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_format)

    file_handler = RotatingFileHandler(
        "improvement.log", maxBytes=10*1024*1024, backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    file_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger

logger = setup_logging()
logger.info(f"{__app__} - {__author__} - Version: {__version__} initialized")

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if not load_dotenv(dotenv_path=env_path):  # pragma: no cover - optional env file
    logger.debug("dotenv skipped or not found at %s", env_path)