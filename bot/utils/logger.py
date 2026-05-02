import logging
import os

def get_logger(name: str):
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logger = logging.getLogger(name)

    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)s │ %(message)s",
            "%H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(level)
    return logger
