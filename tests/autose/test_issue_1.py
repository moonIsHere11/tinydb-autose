from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage


def test_ac1_or_query_after_and_query_returns_correct_results():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'a': 1, 'b': 1},
        {'a': 1, 'b': 0},
        {'a': 0, 'b': 1},
        {'a': 0, 'b': 0}
    ])
    q = Query()
    
    res_and = db.search((q.a == 1) & (q.b == 1))
    assert len(res_and) == 1
    
    res_or = db.search((q.a == 1) | (q.b == 1))
    assert len(res_or) == 3


def test_ac2_repeated_identical_queries_use_query_cache_correctly():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'a': 1, 'b': 1},
        {'a': 1, 'b': 0},
        {'a': 0, 'b': 1},
        {'a': 0, 'b': 0}
    ])
    q = Query()
    
    res1 = db.search((q.a == 1) & (q.b == 1))
    assert len(res1) == 1
    
    res2 = db.search((q.a == 1) & (q.b == 1))
    assert len(res2) == 1
