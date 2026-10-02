"""Shared isolation for Phase 1 and Phase 2 tests."""

import logging
from pathlib import Path
from typing import Iterator

import pytest

from config.settings import Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate .env changes and logging handlers between tests."""
    for key in (
        "AIE_ENVIRONMENT", "AIE_LOG_LEVEL", "AIE_DATABASE_FILENAME", "PYTHON_DOTENV_DISABLED"
    ):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    logger = logging.getLogger("aie")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagation = logger.propagate
    logger.handlers = []
    try:
        yield
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagation


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Use a temporary project so database-writing tests never affect real data."""
    return load_settings(project_root=tmp_path)
