import pytest

from toy_agent import open_demo


@pytest.fixture
def agent(tmp_path):
    return open_demo(tmp_path / "state")
