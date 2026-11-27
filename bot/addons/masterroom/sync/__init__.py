# Master Room «Персональные данные и синхронизация» (ЧАСТЬ 18.x).
# Co-authored with CoCo
"""Sync subsystem package for the Master Room addon.

18.1 ships only the category model (:mod:`.categories`) — no DB / S3
connections, no side effects on import. Later parts (18.2-18.7) add the
UI screen, the PostgreSQL/S3 clients, secret protection and the actual
sync engine on top of this registry.
"""
