# TinyDB architecture (as of commit edff96a)

## 1. Overview

| Component | File | Job |
|---|---|---|
| `TinyDB` | `tinydb/database.py` | Entry point. Instantiates the storage, keeps a registry `_tables` of the `Table` objects it has handed out (one reused instance per name), forwards unknown attributes to the default table. |
| `Table` | `tinydb/table.py` | CRUD on one table's documents. Holds **per-instance in-memory state**: the query cache (`_query_cache`, an `LRUCache`) and the document-ID counter (`_next_id`). All writes go through `_update_table`. |
| `Query` / `QueryInstance` | `tinydb/queries.py` | Query builder (`Query().a.b == 1`) producing callable `QueryInstance` objects that carry a stable hash. Pure logic, no I/O. |
| `Storage` (+ `JSONStorage`, `MemoryStorage`) | `tinydb/storages.py` | Abstract persistence: `read()` / `write()` / `close()` of the *whole* database dict. |
| `Middleware` (+ `CachingMiddleware`) | `tinydb/middlewares.py` | Wraps a storage class and intercepts read/write while looking like a storage. `CachingMiddleware` serves reads from its in-memory `cache`, counts writes, writes through to the real storage only every `WRITE_CACHE_SIZE` writes and on `close()`. |
| `utils` (`LRUCache`, `freeze`, `FrozenDict`) | `tinydb/utils.py` | Helpers used by Table (cache) and Query (hashable values). |
| `operations` | `tinydb/operations.py` | Update callables (`add`, `delete`, `set`, ...) passed to `Table.update`. |

```mermaid
flowchart LR
  User --> TinyDB
  TinyDB -->|creates| Storage
  TinyDB -->|creates / caches| Table
  Table -->|read/write whole dict| Storage
  Table -->|uses as cache key| Query
  Table --> LRUCache[utils.LRUCache]
  Query --> freeze[utils.freeze]
  CachingMiddleware -->|wraps| Storage
  TinyDB -.storage=Middleware(...).-> CachingMiddleware
```

Call direction: `TinyDB -> Table -> Storage`. `Query` is passed *into* `Table` by the user and never calls `Table` or `Storage`.

## 2. Architecture Decision Records

### ADR-1: Storage interface reads and writes the whole database
- **Status:** Verified for the decision (explained in the comment of `Table._update_table`, `tinydb/table.py:804`). **Inferred** for the live-dict / deep-copy consequence (derived by reading `MemoryStorage.read`, `CachingMiddleware.read` and the updaters, not stated in any comment or commit).
- **Context:** TinyDB must support very different backends (file, memory, user-defined).
- **Decision:** `Storage` exposes only `read()` returning the full `{table: {id: doc}}` dict (or `None` if empty) and `write(data)` for the full dict. `Table` does read-modify-write.
- **Alternatives:** per-document / per-table storage APIs; incremental writes.
- **Consequences:** Writing a new storage is trivial, but every write is O(database size); document IDs become `str` keys when stored (Table converts back); middlewares can intercept just two methods. **`read()` may return the storage's live internal dict** (`MemoryStorage.memory`, `CachingMiddleware.cache`) and table updaters mutate documents in place (`table[doc_id].update(...)`), so any code that needs a snapshot of the database must deep-copy it.

### ADR-2: Queries carry a stable hash; results are cached per table and the cache is dropped on every write
- **Status:** Verified for the hash and cache design (docstrings of `QueryInstance`, `queries.py:59`, and `Table`, `table.py:35`; `clear_cache()` at `table.py:854`). **Inferred** for the last consequence (stale cache / `_next_id` after out-of-band changes): it follows from the code but is not documented anywhere in it.
- **Context:** Repeating the same search should be cheap.
- **Decision:** Each `QueryInstance` has a `_hash` tuple describing operator + path + frozen arguments; `None` means "not cacheable". `Table.search` stores results in an `LRUCache` keyed by the query; `_update_table` clears the cache after each write.
- **Alternatives:** no cache; invalidating selectively; caching by object identity.
- **Consequences:** Two different queries must never share a hash (otherwise `search` returns another query's results); `&`/`|` hashes use a `frozenset` since they are commutative; unhashable arguments disable caching rather than fail. The cache (and the ID counter `_next_id`) live on each `Table` *instance*: if the stored data is changed by anything other than that table's own `_update_table`, the code doing so must call `Table.clear_cache()` and reset `_next_id` for the open tables in `TinyDB._tables`, or the table will serve stale results / wrong IDs.

### ADR-3: Middlewares wrap a storage *class* and are instantiated lazily through `__call__`
- **Status:** Verified (docstring of `Middleware.__call__`, `middlewares.py:10-65`).
- **Context:** TinyDB does `storage(*args, **kwargs)`; extensions like caching must slot in without changing `TinyDB`.
- **Decision:** `Middleware(storage_cls)` is an instance used where a class is expected; calling it builds the real storage, stores it in `self.storage` and returns itself. Unknown attributes are forwarded to the wrapped storage.
- **Alternatives:** subclassing each storage; hooks in `TinyDB`.
- **Consequences:** Middlewares compose by nesting; any storage works under any middleware as long as it honours the `Storage` contract (`read`/`write`/`close`).

## 3. Constraints (do not break)
1. **New storages must subclass `Storage`** and implement `read` (return `None` when empty) and `write`; override `close` if they hold resources. — `tinydb/storages.py:36`
2. **`queries.py` must not import `table.py`, `database.py` or `storages.py`;** it may only depend on `utils`. — `tinydb/queries.py:19-23`
3. **Every query test must supply a hash tuple that uniquely identifies its operator, path and (frozen) arguments, or `None` if it can't be hashed.** Combined queries derive their hash from their operands and operator. — `tinydb/queries.py:76-85, 116-140, 254-264`
4. **All table mutations must go through `Table._update_table`,** which does one read-modify-write (exactly one `storage.read()` and one `storage.write()`) and then clears the query cache. Batch operations must therefore use a single `_update_table` call, not loop over single-document methods (each would be a separate storage write and, under `CachingMiddleware`, a counted write). — `tinydb/table.py:804-854`
5. **Persisted data must be JSON-compatible and use string document IDs;** the storage never sees `int` ids and must not depend on them. — `tinydb/table.py:840-852`
