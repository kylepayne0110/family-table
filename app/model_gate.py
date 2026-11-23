# Keeps slow model calls from slowing down everything else.
#
# Model requests run on their own small thread pool (not the one that serves pages), at
# most `size` at a time and one per person. Extra requests get an immediate "busy" answer
# instead of queueing, so one stalled conversation can't tie up the planner, the grocery
# list, or anyone else's assistant. Nothing here holds a database transaction.

# asyncio hands the blocking call to the pool; threading guards the per-person set.
import asyncio
import threading

# ThreadPoolExecutor is the model's own pool.
from concurrent.futures import ThreadPoolExecutor


class Busy(Exception):
    """The model is already working on this person's last request, or on as many as it can."""


class ModelGate:
    def __init__(self, size: int = 2):
        self.size = max(1, size)
        self.executor = ThreadPoolExecutor(max_workers=self.size, thread_name_prefix="model")
        self._slots = threading.BoundedSemaphore(self.size)
        self._people: set = set()
        self._lock = threading.Lock()

    async def run(self, person, fn, *args):
        """Run fn(*args) on the model pool, or raise Busy right away."""
        with self._lock:
            if person in self._people:
                raise Busy("Still working on your last question. One moment.")
            if not self._slots.acquire(blocking=False):
                raise Busy("The cooking assistant is helping someone else right now. Try again in a moment.")
            self._people.add(person)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self.executor, fn, *args)
        finally:
            with self._lock:
                self._people.discard(person)
                self._slots.release()

    @property
    def in_use(self) -> int:
        with self._lock:
            return len(self._people)
