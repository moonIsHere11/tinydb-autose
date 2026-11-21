from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage

def test_ac1_batch_upsert_updates_inserts_single_write():
    db = TinyDB(storage=MemoryStorage)
    table = db.table('test')

    # Initial data
    doc_id = table.insert({'name': 'Alice', 'age': 25})
    
    User = Query()
    doc_update = {'name': 'Alice', 'age': 26}
    cond_match = (User.name == 'Alice')
    
    doc_new = {'name': 'Bob', 'age': 30}
    cond_no_match = (User.name == 'Bob')

    # Perform upsert_multiple
    result = table.upsert_multiple([
        (doc_update, cond_match),
        (doc_new, cond_no_match)
    ])

    # Check return value structure: list of lists of doc IDs
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == [doc_id]
    assert len(result[1]) == 1
    new_doc_id = result[1][0]
    assert new_doc_id != doc_id

    # Check database state
    assert table.get(doc_id=doc_id)['age'] == 26
    assert table.get(doc_id=new_doc_id)['name'] == 'Bob'

    # Let's test again with a fresh table to measure exactly the upsert_multiple write call(s)
    db2 = TinyDB(storage=MemoryStorage)
    t2 = db2.table('test')
    id1 = t2.insert({'x': 1})
    
    writes = []
    orig_w = db2.storage.write
    db2.storage.write = lambda data: (writes.append(data), orig_w(data))[1]
    
    t2.upsert_multiple([
        ({'x': 2}, Query().x == 1),
        ({'x': 3}, Query().x == 99)
    ])
    
    assert len(writes) == 1

def test_ac2_single_upsert_unaffected():
    db = TinyDB(storage=MemoryStorage)
    table = db.table('test')
    
    User = Query()
    doc_id = table.upsert({'name': 'Charlie', 'age': 40}, User.name == 'Charlie')
    assert isinstance(doc_id, list)
    assert len(doc_id) == 1
    
    doc_id2 = table.upsert({'name': 'Charlie', 'age': 41}, User.name == 'Charlie')
    assert doc_id2 == doc_id
    assert table.get(doc_id=doc_id[0])['age'] == 41
