"""One execution gate shared by the API and scheduled/service entry points."""
import threading

# Reentrant because the API also guards test doubles and enters SyncService.run.
RUN_LOCK = threading.RLock()


class SyncAlreadyRunningError(RuntimeError):
    pass
