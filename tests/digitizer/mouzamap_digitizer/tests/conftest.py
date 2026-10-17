import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.pipeline import run_detection          # noqa: E402
from core.settings import DetectionParams        # noqa: E402
from models.project import Project               # noqa: E402


def make_project(img: np.ndarray, params: DetectionParams | None = None, path: str = "synthetic.png") -> Project:
    """Project with detection already run on `img` (working image == source image)."""
    proj = Project(path)
    if params:
        proj.detect = params
    h, w = img.shape[:2]
    proj.set_geometry_state((w, h), (w, h), np.eye(3))
    proj.store.replace_all(run_detection(img, proj.detect, proj.validation).items)
    proj.store.clear_history()
    return proj


@pytest.fixture
def make_proj():
    return make_project
