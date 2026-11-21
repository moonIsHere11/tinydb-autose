from tinydb import TinyDB
from tinydb.middlewares import CachingMiddleware
from tinydb.storages import MemoryStorage

class CountingMemoryStorage(MemoryStorage):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.write_count = 0

    def write(self, data):
        super().write(data)
        self.write_count += 1

def test_ac1_cache_modified_count_resets_after_flush():
    storage = CountingMemoryStorage()
    cache = CachingMiddleware(lambda *a, **kw: storage)
    db = TinyDB(storage=cache)
    cache.WRITE_CACHE_SIZE = 3

    for i in range(6):
        db.insert({'value': i})

    assert storage.write_count == 2

def test_ac2_manual_flush_resets_cache_modification_count():
    storage = CountingMemoryStorage()
    cache = CachingMiddleware(lambda *a, **kw: storage)
    db = TinyDB(storage=cache)
    cache.WRITE_CACHE_SIZE = 10

    db.insert({'value': 1})
    cache.flush()
    assert storage.write_count == 1

    db.insert({'value': 2})
    assert storage.write_count == 1
