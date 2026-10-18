"""Authentication checks for the Homework 2 endpoint.

These tests stay offline: no Langfuse, Docker, or model provider key.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server import app as server_app


@pytest.fixture(autouse=True)
def _clear_sessions(world: dict) -> None:
    server_app._SESSIONS.clear()
    yield
    server_app._SESSIONS.clear()


def test_create_session_rejects_role_that_differs_from_database() -> None:
    """User 1 is a shopper. Claiming merchant must be rejected."""
    with pytest.raises(HTTPException) as caught:
        server_app.create_session(server_app.SessionCreate(user_id=1, role="merchant"))
    assert caught.value.status_code == 403
    assert server_app._SESSIONS == {}


def test_token_from_one_session_cannot_authorize_another() -> None:
    first = server_app.create_session(server_app.SessionCreate(user_id=1, role="shopper"))
    second = server_app.create_session(server_app.SessionCreate(user_id=1, role="shopper"))

    with pytest.raises(HTTPException) as caught:
        server_app._authorize(second["session_id"], f"Bearer {first['token']}")

    assert caught.value.status_code == 403
