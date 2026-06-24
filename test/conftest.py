"""Shared fixtures for yt-play tests.""" 

import pytest


@pytest.fixture
def sample_video_id() -> str:
    return "dQw4w9WgXcQ"


@pytest.fixture
def sample_url() -> str:
    return "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def sample_search_data() -> dict:
    return {
        "id": "dQw4w9WgXcQ",
        "title": "Rick Astley - Never Gonna Give You Up",
        "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "duration": 212,
        "uploader": "Rick Astley",
    }
