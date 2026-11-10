from __future__ import annotations


class Recovery:
    """Restart budget for consecutive failures, independent from menu retries."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.wanted = False
        self.pending = False
        self.attempts = 0
        self.healthy_since = None

    def activate(self):
        self.wanted = True
        self.pending = False
        self.attempts = 0
        self.healthy_since = None

    def cancel(self):
        self.wanted = False
        self.pending = False
        self.healthy_since = None

    def failure(self) -> bool:
        self.healthy_since = None
        self.pending = self.wanted and self.cfg["enabled"] and self.attempts < self.cfg["max_restarts"]
        return self.pending

    def reserve(self) -> bool:
        if not self.pending or not self.wanted or self.attempts >= self.cfg["max_restarts"]:
            return False
        self.pending = False
        self.attempts += 1
        return True

    def healthy(self, now: float, confirmed: bool):
        if not confirmed:
            self.healthy_since = None
        elif self.healthy_since is None:
            self.healthy_since = now
        elif now - self.healthy_since >= self.cfg["stable_seconds"]:
            self.attempts = 0
