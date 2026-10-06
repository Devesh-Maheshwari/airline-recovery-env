"""ASGI import target used by OpenEnv CLI and deployment tooling."""

from .server import build_app

app = build_app()
