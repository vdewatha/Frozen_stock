from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import retry_strategy_learning_failures
from app.core.config import Settings
from app.core.security import AuthenticationMiddleware


def test_retry_control_authorization_and_bounded_queue():
    roles = ("viewer", "researcher", "operator", "admin")
    config = Settings(
        _env_file=None,
        auth_mode="local_role_keys",
        **{f"auth_{role}_key": role.ljust(40, "-") for role in roles},
    )
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, configuration=config)
    path = "/system/strategy-learning/retry-failures"
    app.add_api_route(path, retry_strategy_learning_failures, methods=["POST"], status_code=202)
    with TestClient(app) as client, patch(
        "app.tasks.jobs.retry_failed_strategy_learning_scopes_job.apply_async",
        return_value=SimpleNamespace(id="isolated-retry-task"),
    ) as enqueue:
        assert client.post(path).status_code == 401
        enqueue.assert_not_called()
        for role in roles:
            enqueue.reset_mock()
            response = client.post(path, headers={"Authorization": "Bearer " + role.ljust(40, "-")})
            if role in {"viewer", "researcher"}:
                assert response.status_code == 403
                enqueue.assert_not_called()
            else:
                assert response.status_code == 202
                assert response.json() == {
                    "status": "queued",
                    "task_id": "isolated-retry-task",
                    "paper_only": True,
                    "live_authorized": False,
                }
                enqueue.assert_called_once_with(queue="learning", expires=900)
