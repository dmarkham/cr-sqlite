"""Tests for savepoint handling of
crsql_rows_impacted() and of the table info cache, and crsql_commit_alter
without crsql_begin_alter.
"""
import os
import sqlite3

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
EXT = os.path.join(REPO, "core", "dist", "crsqlite")

SITE_1 = bytes.fromhex("00000000000000000000000000000001")


def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.enable_load_extension(True)
    c.load_extension(EXT)
    return c


def close(c):
    c.execute("SELECT crsql_finalize()")
    c.close()


def pk(i):
    return b'\x01\x09' + bytes([i])  # crsql_pack_columns(i), 0 < i < 128


def change(i, site_id=SITE_1):
    return ('t', pk(i), 'v', 'x%d' % i, 1, 1, site_id, 1, 0, '0')


def merge(c, ids):
    for i in ids:
        c.execute(
            "INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            change(i))


def rows_impacted(c):
    return c.execute("SELECT crsql_rows_impacted()").fetchone()[0]


def changes_db():
    c = conn()
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
    c.execute("SELECT crsql_as_crr('t')")
    return c


def test_rows_impacted_rollback_to_savepoint():
    """rowsImpacted was only reset by a full rollback, so merges
    undone by ROLLBACK TO stayed counted."""
    c = changes_db()
    c.execute("BEGIN")
    merge(c, [1, 2])
    c.execute("SAVEPOINT s")
    merge(c, [3, 4, 5])
    assert rows_impacted(c) == 5
    c.execute("ROLLBACK TO s")
    c.execute("RELEASE s")
    assert rows_impacted(c) == 2
    c.execute("COMMIT")
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 2
    close(c)


def test_rows_impacted_nested_savepoints():
    """Nested savepoints, including ones opened before crsql_changes
    joined the transaction (SQLite never calls xSavepoint for those)."""
    c = changes_db()
    c.execute("BEGIN")
    c.execute("SAVEPOINT outer1")
    c.execute("SAVEPOINT outer2")
    merge(c, [1])
    c.execute("SAVEPOINT a")
    merge(c, [2, 3])
    c.execute("SAVEPOINT b")
    merge(c, [4])
    assert rows_impacted(c) == 4
    c.execute("ROLLBACK TO b")
    assert rows_impacted(c) == 3
    c.execute("RELEASE b")
    merge(c, [5])
    assert rows_impacted(c) == 4
    c.execute("ROLLBACK TO a")
    assert rows_impacted(c) == 1
    c.execute("RELEASE a")
    c.execute("SAVEPOINT c")
    merge(c, [6])
    c.execute("RELEASE c")
    assert rows_impacted(c) == 2
    # both opened before the vtab joined: nothing merged before them
    c.execute("ROLLBACK TO outer2")
    assert rows_impacted(c) == 0
    merge(c, [7, 8])
    assert rows_impacted(c) == 2
    c.execute("ROLLBACK TO outer1")
    assert rows_impacted(c) == 0
    merge(c, [9])
    c.execute("RELEASE outer1")
    assert rows_impacted(c) == 1
    c.execute("COMMIT")
    assert c.execute("SELECT id FROM t").fetchall() == [(9,)]
    close(c)


def test_rows_impacted_failed_multi_row_merge():
    """A multi-row merge statement that fails part way is rolled
    back by SQLite (statement rollback -> xRollbackTo); the rows it merged
    before failing must not stay counted."""
    c = changes_db()
    c.execute("BEGIN")
    merge(c, [1, 2])
    rows = [change(3), change(4), change(5, site_id=None)]
    with pytest.raises(sqlite3.Error, match="site_id must be a non-empty blob"):
        c.execute(
            "INSERT INTO crsql_changes SELECT * FROM (VALUES "
            + ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(rows)) + ")",
            [v for r in rows for v in r])
    assert c.in_transaction
    assert rows_impacted(c) == 2
    c.execute("COMMIT")
    assert c.execute("SELECT id FROM t ORDER BY id").fetchall() == [(1,), (2,)]
    close(c)


def schema_version(c):
    return c.execute("PRAGMA schema_version").fetchone()[0]


def bring_schema_version_to(c, target):
    """Runs unrelated DDL until PRAGMA schema_version is `target` again."""
    n = 0
    while schema_version(c) < target:
        c.execute(f"CREATE TABLE filler{n} (x)")
        n += 1
    assert schema_version(c) == target


def alter_db():
    c = conn()
    c.execute("CREATE TABLE a (id INTEGER PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('a')")
    c.execute("INSERT INTO a VALUES (1, 1)")
    return c


def assert_a_is_unaltered(c):
    c.execute("INSERT INTO a VALUES (3, 3)")
    c.execute("UPDATE a SET x = 9 WHERE id = 1")
    c.execute("COMMIT")
    cids = {r[0] for r in c.execute("SELECT cid FROM crsql_changes WHERE [table] = 'a'")}
    assert cids == {'x'}, cids
    assert c.execute("SELECT * FROM a ORDER BY id").fetchall() == [(1, 9), (3, 3)]


def test_table_infos_after_rollback_to_commit_alter():
    """The table infos pulled by
    crsql_commit_alter inside a savepoint survived ROLLBACK TO; once other DDL
    brought PRAGMA schema_version back to the same number they were trusted
    again and described the rolled back column z."""
    c = alter_db()
    c.execute("BEGIN")
    c.execute("SAVEPOINT s")
    c.execute("SELECT crsql_begin_alter('a')")
    c.execute("ALTER TABLE a ADD COLUMN z")
    c.execute("SELECT crsql_commit_alter('a')")
    altered = schema_version(c)
    c.execute("INSERT INTO a VALUES (2, 2, 2)")
    c.execute("ROLLBACK TO s")
    bring_schema_version_to(c, altered)
    assert_a_is_unaltered(c)
    close(c)


def test_table_infos_after_rollback_to_as_crr():
    """The as_crr variant. After ROLLBACK TO, b is no CRR anymore but
    the cached table infos still listed it, so reading crsql_changes queried
    the rolled back b__crsql_clock."""
    c = alter_db()
    c.execute("CREATE TABLE b (id INTEGER PRIMARY KEY NOT NULL, y)")
    c.execute("BEGIN")
    c.execute("SAVEPOINT s")
    c.execute("SELECT crsql_as_crr('b')")
    upgraded = schema_version(c)
    c.execute("INSERT INTO b VALUES (1, 1)")
    c.execute("ROLLBACK TO s")
    bring_schema_version_to(c, upgraded)
    assert c.execute("SELECT [table], cid FROM crsql_changes").fetchall() == [('a', 'x')]
    c.execute("INSERT INTO b VALUES (2, 2)")
    assert_a_is_unaltered(c)
    close(c)


def test_table_infos_after_rollback_to_plain_alter():
    """A plain ALTER TABLE on a CRR inside a savepoint that is rolled
    back. The next write pulled the table infos with column z; after ROLLBACK
    TO and other DDL to the same schema version they were trusted again."""
    c = alter_db()
    c.execute("BEGIN")
    c.execute("SAVEPOINT s")
    c.execute("ALTER TABLE a ADD COLUMN z")
    altered = schema_version(c)
    c.execute("INSERT INTO a VALUES (2, 2, 2)")
    c.execute("ROLLBACK TO s")
    bring_schema_version_to(c, altered)
    assert_a_is_unaltered(c)
    close(c)


def commit_alter_failure_db():
    """a has a stale clock entry for a column `gone` (compacted away by
    commit_alter) and a unique index (rejected by commit_alter after the
    compaction ran)."""
    c = alter_db()
    c.execute(
        "INSERT INTO a__crsql_clock (key, col_name, col_version, db_version, site_id, seq) "
        "VALUES (1, 'gone', 1, 1, 0, 0)")
    c.execute("CREATE UNIQUE INDEX ux ON a (x)")
    return c


def gone_entries(c):
    return c.execute(
        "SELECT count(*) FROM a__crsql_clock WHERE col_name = 'gone'").fetchone()[0]


def test_commit_alter_standalone():
    """Without crsql_begin_alter there
    is no alter_crr savepoint. A successful commit_alter failed with "no such
    savepoint: alter_crr"; a failing one could not roll back what it had
    already done (the compaction stayed applied)."""
    c = alter_db()
    c.execute("BEGIN")
    c.execute("INSERT INTO a VALUES (2, 2)")
    assert c.execute("SELECT crsql_commit_alter('a')").fetchall() == [(None,)]
    assert c.in_transaction
    c.execute("COMMIT")

    c.execute(
        "INSERT INTO a__crsql_clock (key, col_name, col_version, db_version, site_id, seq) "
        "VALUES (1, 'gone', 1, 1, 0, 0)")
    c.execute("CREATE UNIQUE INDEX ux ON a (x)")
    c.execute("BEGIN")
    c.execute("INSERT INTO a VALUES (3, 3)")
    with pytest.raises(sqlite3.Error, match="has unique indices") as e:
        c.execute("SELECT crsql_commit_alter('a')")
    assert "savepoint" not in str(e.value)
    assert c.in_transaction
    assert gone_entries(c) == 1
    assert c.execute("SELECT id FROM a ORDER BY id").fetchall() == [(1,), (2,), (3,)]
    c.execute("INSERT INTO a VALUES (4, 4)")
    c.execute("COMMIT")
    assert c.execute(
        "SELECT count(*) FROM crsql_changes WHERE [table] = 'a' AND cid = 'x'").fetchone()[0] == 4
    close(c)


def test_commit_alter_paired_failure():
    """With crsql_begin_alter, a failing commit_alter still rolls back
    the whole alteration (triggers come back) and releases alter_crr."""
    c = commit_alter_failure_db()
    c.execute("BEGIN")
    c.execute("INSERT INTO a VALUES (2, 2)")
    c.execute("SELECT crsql_begin_alter('a')")
    c.execute("ALTER TABLE a ADD COLUMN z")
    with pytest.raises(sqlite3.Error, match="has unique indices"):
        c.execute("SELECT crsql_commit_alter('a')")
    assert c.in_transaction
    with pytest.raises(sqlite3.Error, match="no such savepoint"):
        c.execute("RELEASE alter_crr")
    assert gone_entries(c) == 1
    assert [r[1] for r in c.execute("PRAGMA table_info(a)")] == ['id', 'x']
    c.execute("INSERT INTO a VALUES (3, 3)")
    c.execute("COMMIT")
    assert c.execute(
        "SELECT count(*) FROM crsql_changes WHERE [table] = 'a' AND cid = 'x'").fetchone()[0] == 3
    close(c)


def test_commit_alter_paired_success():
    """The paired path keeps releasing alter_crr on success."""
    c = alter_db()
    c.execute("BEGIN")
    c.execute("SELECT crsql_begin_alter('a')")
    c.execute("ALTER TABLE a ADD COLUMN z")
    c.execute("SELECT crsql_commit_alter('a')")
    with pytest.raises(sqlite3.Error, match="no such savepoint"):
        c.execute("RELEASE alter_crr")
    c.execute("INSERT INTO a VALUES (2, 2, 2)")
    c.execute("COMMIT")
    assert ('z', 2) in c.execute("SELECT cid, val FROM crsql_changes").fetchall()
    close(c)
