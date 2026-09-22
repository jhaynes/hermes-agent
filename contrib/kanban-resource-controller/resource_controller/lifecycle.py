"""Track observed worker descendants across reparenting; never signal them."""
import psutil


class DrainTracker:
    def __init__(self, store):
        self.store = store
        self.known = store.read('descendants.json') or []

    def include(self, pids):
        known = {(item['pid'], item['created']) for item in self.known}
        for pid in pids:
            try:
                process = psutil.Process(pid)
                known.add((pid, process.create_time()))
            except psutil.NoSuchProcess:
                continue
        self.known = [{'pid': pid, 'created': created} for pid, created in sorted(known)]
        self.refresh()

    def refresh(self):
        living = set()
        for item in self.known:
            try:
                process = psutil.Process(item['pid'])
                if process.create_time() != item['created'] or process.status() == psutil.STATUS_ZOMBIE:
                    continue
                living.add((process.pid, item['created']))
                for child in process.children(recursive=True):
                    if child.status() != psutil.STATUS_ZOMBIE:
                        living.add((child.pid, child.create_time()))
            except psutil.NoSuchProcess:
                continue
            # AccessDenied propagates: unknown drain state is NOT a clean exit.
        self.known = [{'pid': pid, 'created': created} for pid, created in sorted(living)]
        self.store.write('descendants.json', self.known)
        return self.known

    def drained(self):
        return not self.refresh()
