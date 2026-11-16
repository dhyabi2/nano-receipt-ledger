"""The ledger's storage: one JSON file, rewritten atomically.

There is no schema migration and no database because there is nothing here
a database would do better. What matters is that the file is only ever
replaced wholesale, so a crash halfway through a write leaves the previous
ledger intact rather than a truncated one.
"""

import json
import os
import tempfile
import threading


class Store:
    def __init__(self, path=None):
        self.path = path
        self._lock = threading.RLock()
        self._data = {"receipts": [], "corrections": []}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            self._data = {
                "receipts": loaded.get("receipts", []),
                "corrections": loaded.get("corrections", []),
            }

    @property
    def lock(self):
        return self._lock

    def receipts(self):
        return self._data["receipts"]

    def corrections(self):
        return self._data["corrections"]

    def by_id(self, receipt_id):
        for receipt in self._data["receipts"]:
            if receipt["id"] == receipt_id:
                return receipt
        return None

    def flush(self):
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", dir=directory, delete=False, encoding="utf-8", suffix=".tmp")
        try:
            json.dump(self._data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self.path)
