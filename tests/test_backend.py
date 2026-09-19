"""Contract tests for the private data-plane backend boundary."""

from pathlib import Path
from typing import cast

from anyhop import backend, singbox
from anyhop.backend import RuntimeBackend
from anyhop.engine import Engine
from anyhop.state import Store


def test_default_runtime_backend_is_the_singbox_adapter():
    assert isinstance(backend.runtime_backend(), singbox.Runner)


def test_engine_accepts_an_injected_runtime_backend():
    class FakeBackend:
        pass

    fake = FakeBackend()
    assert Engine(Store.load(), runner=cast(RuntimeBackend, fake)).runner is fake


def test_public_openapi_does_not_expose_backend_control_details():
    spec = Path("docs/openapi.yaml").read_text()
    for private_name in ("clash_api", "external_controller", "singbox.json"):
        assert private_name not in spec
