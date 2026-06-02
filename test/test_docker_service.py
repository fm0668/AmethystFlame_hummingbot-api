from unittest.mock import MagicMock

from services.docker_service import DockerService


def _build_service(source_path="/hummingbot-api"):
    service = DockerService.__new__(DockerService)
    service.SOURCE_PATH = source_path
    service.client = MagicMock()
    return service


def test_get_host_project_root_prefers_bots_path(monkeypatch):
    service = _build_service()
    monkeypatch.setenv("BOTS_PATH", "/opt/amethystflame/AmethystFlame_hummingbot-api")
    monkeypatch.delenv("HOSTNAME", raising=False)

    assert service._get_host_project_root() == "/opt/amethystflame/AmethystFlame_hummingbot-api"


def test_get_host_project_root_infers_host_mount_from_current_container(monkeypatch):
    service = _build_service()
    current_container = MagicMock()
    current_container.attrs = {
        "Mounts": [
            {
                "Source": "/opt/amethystflame/AmethystFlame_hummingbot-api/bots",
                "Destination": "/hummingbot-api/bots",
            }
        ]
    }
    service.client.containers.get.return_value = current_container

    monkeypatch.delenv("BOTS_PATH", raising=False)
    monkeypatch.setenv("HOSTNAME", "api-container-id")

    assert service._get_host_project_root() == "/opt/amethystflame/AmethystFlame_hummingbot-api"
