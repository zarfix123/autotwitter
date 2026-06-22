"""X Growth Engine — a compliance-first personal X growth tool.

Phase 1 (this package, so far): zero-touch original-posting core.
  git watcher -> secret scrubber -> content generator -> scheduler -> poster.

Hard rule (see plan): no code path performs a reply/follow/like/repost/DM to
another account without a per-item, fresh human-approval token. That gate is
introduced in Phase 2; nothing in Phase 1 touches other-account engagement.
"""

__version__ = "0.1.0"
