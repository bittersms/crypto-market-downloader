"""Progress indicator for CLI operations.

Provides a simple progress bar for long-running data fetch operations.
Uses carriage return to update the same line in terminal.
"""
import sys
import time
from threading import Lock


class ProgressBar:
    """Simple terminal progress bar for data operations."""

    def __init__(self, total: int, description: str = "", 
                 show_speed: bool = True, width: int = 50):
        self.total = total
        self.current = 0
        self.description = description
        self.show_speed = show_speed
        self.width = width
        self._lock = Lock()
        self._start_time = time.time()
        self._last_update = 0

    def update(self, increment: int = 1, message: str = ""):
        """Update progress by increment."""
        with self._lock:
            self.current += increment
            self._render(message)

    def set(self, value: int, message: str = ""):
        """Set progress to absolute value."""
        with self._lock:
            self.current = value
            self._render(message)

    def _render(self, message: str = ""):
        """Render the progress bar to stderr/stdout."""
        now = time.time()
        # Throttle to 10 FPS max
        if now - self._last_update < 0.1 and self.current < self.total:
            return
        self._last_update = now

        if self.total == 0:
            percent = 100
        else:
            percent = min(100, (self.current / self.total) * 100)

        filled = int(self.width * percent / 100)
        bar = "█" * filled + "░" * (self.width - filled)

        parts = [f"\r{self.description}: [{bar}] {percent:.0f}% ({self.current}/{self.total})"]

        if message:
            parts.append(f" {message}")

        if self.show_speed and self.current > 0:
            elapsed = now - self._start_time
            if elapsed > 0:
                rate = self.current / elapsed
                remaining = (self.total - self.current) / rate if rate > 0 else 0
                parts.append(f" ⏱ {rate:.1f}/s ETA: {remaining:.0f}s")

        sys.stdout.write("".join(parts))
        sys.stdout.flush()

    def finish(self, message: str = "Done"):
        """Finish the progress bar."""
        with self._lock:
            self.current = self.total if self.total > 0 else 0
            self._render(message)
            sys.stdout.write("\n")
            sys.stdout.flush()

    def close(self):
        """Alias for finish."""
        self.finish()
