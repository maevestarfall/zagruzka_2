"""Durable, model-independent Master Room tool runtime."""

from .api import create_app
from .service import CoderService

# Backwards-compatible public name used by existing launchers.
create_coder_server_app = create_app

__all__ = ["CoderService", "create_app", "create_coder_server_app"]
