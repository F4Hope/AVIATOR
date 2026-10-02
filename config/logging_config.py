"""Reusable console logging configuration for AIE modules."""

import logging

from config.settings import LOG_LEVELS


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure the AIE logger once and return it.

    Use logging.getLogger("aie.<module>") in application modules. Configure
    only our own logger so other applications and pytest retain their handlers.
    Log known status messages, never credentials or raw session information.
    """
    normalized_level = level.strip().upper()
    if normalized_level not in LOG_LEVELS:
        raise ValueError("Unsupported logging level.")

    logger = logging.getLogger("aie")
    logger.setLevel(normalized_level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )
        logger.addHandler(handler)

    return logger
