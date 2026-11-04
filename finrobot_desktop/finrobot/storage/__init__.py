"""SQLite infrastructure shared by every FinRobot store.

- :mod:`finrobot.storage.sqlite` — :class:`SqliteDatabase` (one per file:
  connection lifecycle, PRAGMAs, versioned migrations, ``transaction()``).
- :mod:`finrobot.storage.user_db` — the consolidated user-state database
  (runs, artifacts, coverage, portfolio, alerts) and the one-time import of
  the legacy per-store files.

Caches (``data_cache.db`` / ``quotes.db`` / ``sec_holdings_cache.db``) keep
their own files — they are disposable — but reuse the same base class.
"""

from finrobot.storage.sqlite import Migration, SqliteDatabase

__all__ = ["Migration", "SqliteDatabase"]
