from tinydb import TinyDB, Query, where
from tinydb.storages import MemoryStorage

def test_ac1_range_query_inclusive():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'age': 15},
        {'age': 18},
        {'age': 25},
        {'age': 30},
        {'age': 35}
    ])
    results = db.search(Query().age.between(18, 30))
    ages = [doc['age'] for doc in results]
    assert sorted(ages) == [18, 25, 30]

def test_ac2_where_shorthand_and_nested_fields():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'stats': {'score': 3}},
        {'stats': {'score': 10}}
    ])
    results = db.search(where('stats.score').between(1, 5))
    assert len(results) == 1
    assert results[0]['stats']['score'] == 3

def test_ac3_missing_fields_and_uncomparable_types():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {},
        {'age': 'twenty'},
        {'age': 25}
    ])
    results = db.search(Query().age.between(18, 30))
    assert len(results) == 1
    assert results[0]['age'] == 25

def test_ac4_combine_with_logical_operators():
    db = TinyDB(storage=MemoryStorage)
    db.insert_multiple([
        {'age': 15},
        {'age': 22},
        {'age': 35}
    ])
    results = db.search(~(Query().age.between(20, 30)))
    ages = sorted([doc['age'] for doc in results])
    assert ages == [15, 35]
