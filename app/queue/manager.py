import threading
from queue import Queue
from typing import Callable, Dict, Any, Optional

class TaskResult:
    def __init__(self):
        self.event = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None

    def set(self, value: Any):
        self.value = value
        self.event.set()

    def set_error(self, err: BaseException):
        self.error = err
        self.event.set()

    def get(self):
        self.event.wait()
        if self.error:
            raise self.error
        return self.value

class KeyedTaskQueue:
    """Per-key serialization: tasks with same key execute in order; different keys parallel."""
    def __init__(self, worker_count: int = 4):
        self.global_lock = threading.Lock()
        self.queues: Dict[str, Queue] = {}
        self.active: Dict[str, bool] = {}
        self.worker_count = worker_count

    def submit(self, key: str, func: Callable[[], Any]) -> TaskResult:
        result = TaskResult()
        with self.global_lock:
            q = self.queues.setdefault(key, Queue())
            q.put((func, result))
            if not self.active.get(key):
                self.active[key] = True
                threading.Thread(target=self._worker, args=(key,), daemon=True).start()
        return result

    def _worker(self, key: str):
        while True:
            with self.global_lock:
                q = self.queues.get(key)
                if q is None:
                    self.active[key] = False
                    return
            try:
                task, result = q.get(timeout=0.1)
            except Exception:
                with self.global_lock:
                    # queue empty, cleanup
                    if q.empty():
                        self.queues.pop(key, None)
                        self.active[key] = False
                        return
                continue
            try:
                val = task()
                result.set(val)
            except BaseException as e:  # noqa
                result.set_error(e)
            finally:
                q.task_done()

# Singleton queue manager
queue_manager = KeyedTaskQueue()
