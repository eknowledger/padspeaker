"""Shared pytest fixtures."""
import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_padspeaker_logger():
    """Undo ``setup_logging`` so one test's logging setup cannot leak into another."""
    yield
    logger = logging.getLogger("padspeaker")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
