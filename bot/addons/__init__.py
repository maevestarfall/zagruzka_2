"""Bolt-on features that live alongside lilush without touching its core.

Each subpackage exposes ``build_router()`` returning an aiogram ``Router``
that lilush's dispatcher includes. State is persisted via the helpers in
:mod:`bot.addons.state`, which piggybacks on lilush's own
``storage._settings()`` dict — no extra JSON files, no schema changes.
"""

from __future__ import annotations

__all__ = ["build_addon_routers"]


def build_addon_routers():
    """Return the list of addon routers to include in lilush's dispatcher.

    Imports happen lazily so that an addon failing to import (e.g.
    missing system package) only breaks that addon, not the whole bot.
    """
    routers = []
    from .photo_chooser.handlers import build_photo_chooser_router
    routers.append(build_photo_chooser_router())
    from .thinking_style.handlers import build_thinking_style_router
    routers.append(build_thinking_style_router())
    try:
        from .helpzavr.handlers import build_helpzavr_router
        routers.append(build_helpzavr_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "helpzavr addon disabled: %s", e
        )
    try:
        from .mailbox.handlers import build_mailbox_router
        routers.append(build_mailbox_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "mailbox addon disabled: %s", e
        )
    try:
        from .slack.handlers import build_slack_router
        routers.append(build_slack_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "slack addon disabled: %s", e
        )
    try:
        from .versiff.handlers import build_versiff_router
        routers.append(build_versiff_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "versiff addon disabled: %s", e
        )
    try:
        from .algorithm import build_algorithm_router
        routers.append(build_algorithm_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "algorithm addon disabled: %s", e
        )
    from .pretty_text.handlers import build_pretty_text_router
    routers.append(build_pretty_text_router())
    from .media_toggle.handlers import build_media_toggle_router
    routers.append(build_media_toggle_router())
    from .memory.handlers import build_memory_router
    routers.append(build_memory_router())
    from .ram_guard.handlers import build_ram_router
    routers.append(build_ram_router())
    try:
        from .editor_agent.handlers import build_editor_agent_router
        routers.append(build_editor_agent_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "editor_agent addon disabled: %s", e
        )
    try:
        from .ai_check.handlers import build_ai_check_router
        routers.append(build_ai_check_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "ai_check addon disabled: %s", e
        )
    try:
        from .debates.handlers import build_debates_router
        routers.append(build_debates_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "debates addon disabled: %s", e
        )
    try:
        from .debates.viktor_v3 import build_viktor_v3_router
        routers.append(build_viktor_v3_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "viktor_v3 debates submenu disabled: %s", e
        )
    try:
        from .analyzer.handlers import build_analyzer_router
        routers.append(build_analyzer_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "analyzer addon disabled: %s", e
        )
    try:
        from .masterroom.handlers import build_masterroom_router
        routers.append(build_masterroom_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "masterroom addon disabled: %s", e
        )
    try:
        from .motivation_auto.handlers import build_motivation_auto_router
        routers.append(build_motivation_auto_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "motivation_auto addon disabled: %s", e
        )
    try:
        from .issuance.handlers import build_issuance_router
        routers.append(build_issuance_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "issuance addon disabled: %s", e
        )
    try:
        from .coder.handlers import build_coder_router
        routers.append(build_coder_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "coder addon disabled: %s", e
        )
    try:
        from .vibe.handlers import build_vibe_router
        routers.append(build_vibe_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "vibe addon disabled: %s", e
        )
    try:
        from .algo_slots.handlers import build_algo_slots_router
        routers.append(build_algo_slots_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "algo_slots addon disabled: %s", e
        )
    try:
        from .instructions.handlers import build_instructions_router
        routers.append(build_instructions_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "instructions addon disabled: %s", e
        )
    try:
        from .separation.handlers import build_separation_router
        routers.append(build_separation_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "separation addon disabled: %s", e
        )
    try:
        from .publication.handlers import build_publication_router
        routers.append(build_publication_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "publication addon disabled: %s", e
        )
    try:
        from .context.handlers import build_context_router
        routers.append(build_context_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "context addon disabled: %s", e
        )
    try:
        from .unstructured.handlers import build_unstructured_router
        routers.append(build_unstructured_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "unstructured addon disabled: %s", e
        )
    try:
        from .vault.handlers import build_vault_router
        routers.append(build_vault_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "vault addon disabled: %s", e
        )
    try:
        from .osint.handlers import build_osint_router
        routers.append(build_osint_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "osint addon disabled: %s", e
        )
    try:
        from .prompt_sources.handlers import build_prompt_sources_router
        routers.append(build_prompt_sources_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "prompt_sources addon disabled: %s", e
        )
    try:
        from .priority.handlers import build_priority_router
        routers.append(build_priority_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "priority addon disabled: %s", e
        )
    try:
        from .source_channels import build_source_channels_router
        routers.append(build_source_channels_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "source_channels addon disabled: %s", e
        )
    try:
        from .playbook import build_playbook_router
        routers.append(build_playbook_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "playbook addon disabled: %s", e
        )
    try:
        from .stealth import build_stealth_router
        routers.append(build_stealth_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "stealth addon disabled: %s", e
        )
    try:
        from .clicker import build_clicker_router
        routers.append(build_clicker_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "clicker addon disabled: %s", e
        )
    try:
        from .brain20 import build_brain20_router
        routers.append(build_brain20_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "brain20 addon disabled: %s", e
        )
    try:
        from .promptlab import build_promptlab_router
        routers.append(build_promptlab_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "promptlab addon disabled: %s", e
        )
    try:
        from .browser_panel import build_browser_panel_router
        routers.append(build_browser_panel_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "browser_panel addon disabled: %s", e
        )
    try:
        from .scenarios import build_scenarios_router
        routers.append(build_scenarios_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "scenarios addon disabled: %s", e
        )
    try:
        from .private_mode import build_private_mode_router
        routers.append(build_private_mode_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "private_mode addon disabled: %s", e
        )
    try:
        from .projectmap.handlers import build_projectmap_router
        routers.append(build_projectmap_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "projectmap addon disabled: %s", e
        )
    try:
        from .context_relay import build_context_relay_router
        routers.append(build_context_relay_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "context_relay addon disabled: %s", e
        )
    try:
        from .context_relay.orchestra_handlers import build_orchestra_router
        routers.append(build_orchestra_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "orchestra addon disabled: %s", e
        )
    try:
        from .loop_review import build_loop_review_router
        routers.append(build_loop_review_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "loop_review addon disabled: %s", e
        )
    try:
        from .guide import build_guide_router
        routers.append(build_guide_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "guide addon disabled: %s", e
        )
    try:
        from .improveproj import build_improveproj_router
        routers.append(build_improveproj_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "improveproj addon disabled: %s", e
        )
    try:
        from .promptbox.handlers import build_promptbox_router
        routers.append(build_promptbox_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "promptbox addon disabled: %s", e
        )
    try:
        from .swapface import build_swapface_router
        routers.append(build_swapface_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "swapface addon disabled: %s", e
        )
    try:
        from .plan_map.handlers import build_plan_map_router
        routers.append(build_plan_map_router())
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "plan_map addon disabled: %s", e
        )
    return routers
