from __future__ import annotations

import time
import random

def _retry_with_backoff(fn, max_retries: int = 3, base_delay: float = 1.0):
    """Call fn() with exponential backoff on rate-limit or server errors."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            msg = str(e).lower()
            is_rate_limit = "429" in msg or "rate limit" in msg or "too many requests" in msg
            is_server_error = "500" in msg or "502" in msg or "503" in msg or "server error" in msg
            if (is_rate_limit or is_server_error) and attempt < max_retries:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                time.sleep(delay)
                continue
            raise
    raise last_exc  # type: ignore[misc]
