from __future__ import annotations

import logging
import os
from .persona import assert_persona_namespace, list_personas

logger = logging.getLogger(__name__)

def assert_service_manifest() -> None:
    """Selfcheck asserts the persona-key contract and namespace isolation at boot."""
    logger.info("Starting boot-time deploy self-check...")
    
    # 1. Check LILUSH_INSTALL_ROOT env var
    install_root = os.environ.get("LILUSH_INSTALL_ROOT")
    if not install_root:
        logger.critical("Boot failed: LILUSH_INSTALL_ROOT environment variable is required but unset.")
        raise RuntimeError("LILUSH_INSTALL_ROOT environment variable is required.")
        
    # 2. Check persona keys contract
    registered_keys = list_personas()
    try:
        assert_persona_namespace(registered_keys)
        logger.info("Deploy self-check passed successfully. Persona namespace is isolated.")
    except Exception as e:
        logger.critical("Deploy self-check failed: %s", e)
        raise e
