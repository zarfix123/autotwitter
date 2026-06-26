"""X Growth Engine — a compliance-first personal X growth tool.

Phase 1 (this package, so far): zero-touch original-posting core.
  git watcher -> secret scrubber -> content generator -> scheduler -> poster.

Hard rule (see plan): no code path performs a reply/follow/like/repost/DM to
another account without a per-item, fresh human-approval token. That gate is
introduced in Phase 2; nothing in Phase 1 touches other-account engagement.
"""

import datetime as _datetime

# Python 3.10 compatibility: ``datetime.UTC`` was added in 3.11, but modules in
# this package import it as ``from datetime import UTC``. Package __init__ runs
# before any submodule import, so defining the alias here makes those imports
# work on 3.10 (no-op on 3.11+, which already provides it).
if not hasattr(_datetime, "UTC"):  # pragma: no cover - only exercised on <3.11
    _datetime.UTC = _datetime.timezone.utc

__version__ = "0.1.0"
