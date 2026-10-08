import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.queue_manager import QueueManager  # noqa: E402


@pytest.fixture
def fresh_queue():
    """QueueManager is a singleton; give each test its own instance."""
    QueueManager._instance = None
    qm = QueueManager()
    yield qm
    qm.cancel_all()
    QueueManager._instance = None
