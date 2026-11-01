# Regression tests for table info caching,
# schema version tracking, savepoint handling, identifier quoting, automigrate,
# clset vtab and table name matching.
import ctypes
import ctypes.util
import os
import sqlite3
import subprocess
import sys

import pytest
from crsql_correctness import connect, close

EXT = os.path.abspath("../../core/dist/crsqlite")


def conn(path=":memory:"):
    c = connect(path)
    c.isolation_level = None
    return c


def q(ident):
    return '"' + ident.replace('"', '""') + '"'


def run_isolated(code, timeout=20):
    """Runs `code` in a fresh interpreter so a crash or hang can't take pytest down."""
    prelude = (
        "import sqlite3\n"
        f"EXT = {EXT!r}\n"
        "def conn(path=':memory:'):\n"
        "    c = sqlite3.connect(path, isolation_level=None)\n"
        "    c.enable_load_extension(True)\n"
        "    c.load_extension(EXT)\n"
        "    return c\n"
    )
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    return subprocess.run([sys.executable, "-c", prelude + code],
                          capture_output=True, text=True, timeout=timeout, env=env)


def test_table_infos_refreshed_after_schema_change_in_same_tx():
    """tableinfo.rs crsql_ensure_table_infos_are_up_to_date returned early once
    `updatedTableInfosThisTx == 1`, so a crsql_as_crr or crsql_commit_alter later in the
    same transaction was never seen."""
    c = conn()
    c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('a')")

    # new crr in the same tx as a prior write
    c.execute("BEGIN")
    c.execute("INSERT INTO a VALUES (1, 1)")
    c.execute("CREATE TABLE b (id PRIMARY KEY NOT NULL, y)")
    c.execute("SELECT crsql_as_crr('b')")
    c.execute("INSERT INTO b VALUES (1, 1)")
    c.execute("COMMIT")
    assert c.execute(
        "SELECT count(*) FROM crsql_changes WHERE [table] = 'b'").fetchone()[0] == 1

    # altered crr in the same tx as a prior write
    c.execute("BEGIN")
    c.execute("INSERT INTO a VALUES (2, 2)")
    c.execute("SELECT crsql_begin_alter('a')")
    c.execute("ALTER TABLE a ADD COLUMN z")
    c.execute("SELECT crsql_commit_alter('a')")
    c.execute("UPDATE a SET z = 5 WHERE id = 2")
    c.execute("COMMIT")
    assert ('z', 5) in c.execute(
        "SELECT cid, val FROM crsql_changes WHERE [table] = 'a'").fetchall()
    close(c)


def test_rolled_back_alter_does_not_leave_stale_table_info():
    """ext-data.c crsql_fetchPragmaSchemaVersion compared with `>` so a schema_version
    that went back down after a ROLLBACK was ignored and the cached table infos stayed stale."""
    c = conn()
    c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('a')")
    c.execute("BEGIN")
    c.execute("SELECT crsql_begin_alter('a')")
    c.execute("ALTER TABLE a ADD COLUMN z")
    c.execute("SELECT crsql_commit_alter('a')")
    # make sure the table infos for the altered schema got loaded
    c.execute("INSERT INTO a VALUES (100, 1, 1)")
    c.execute("ROLLBACK")

    c.execute("INSERT INTO a VALUES (1, 1)")
    c.execute("UPDATE a SET x = 2 WHERE id = 1")
    assert c.execute("SELECT cid, val FROM crsql_changes").fetchall() == [('x', 2)]

    # a later, unrelated schema change can take schema_version back to the number the
    # rolled back alteration had; the table infos must still be re-pulled.
    v0 = c.execute("PRAGMA schema_version").fetchone()[0]
    c.execute("BEGIN")
    c.execute("SELECT crsql_begin_alter('a')")
    c.execute("ALTER TABLE a ADD COLUMN z")
    c.execute("SELECT crsql_commit_alter('a')")
    c.execute("INSERT INTO a VALUES (100, 1, 1)")
    v1 = c.execute("PRAGMA schema_version").fetchone()[0]
    c.execute("ROLLBACK")
    for i in range(v1 - v0):
        c.execute(f"CREATE TABLE junk{i} (x)")
    assert c.execute("PRAGMA schema_version").fetchone()[0] == v1
    c.execute("UPDATE a SET x = 3 WHERE id = 1")
    assert c.execute("SELECT cid, val FROM crsql_changes").fetchall() == [('x', 3)]
    close(c)


def assert_outer_tx_survives(c, failing_sql, params=()):
    c.execute("BEGIN")
    c.execute("INSERT INTO k VALUES (1)")
    with pytest.raises(sqlite3.Error):
        c.execute(failing_sql, params)
    assert c.in_transaction, failing_sql
    assert c.execute("SELECT count(*) FROM k").fetchone()[0] == 1, failing_sql
    c.execute("ROLLBACK")


def test_error_paths_do_not_rollback_outer_tx(tmp_path):
    """lib.rs (as_crr, commit_alter), automigrate.rs, backfill.rs, bootstrap.rs and
    as_ordered.rs ran a bare `ROLLBACK` on failure, killing the caller's transaction instead of
    rolling back to their own savepoint."""
    c = conn()
    c.execute("CREATE TABLE k (id)")
    c.execute("CREATE TABLE bad (x)")
    c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('a')")

    assert_outer_tx_survives(c, "SELECT crsql_as_crr('bad')")
    assert_outer_tx_survives(
        c, "SELECT crsql_begin_alter('a'), crsql_commit_alter('nope')")
    # adding a NOT NULL column without a default fails inside the migration
    assert_outer_tx_survives(c, "SELECT crsql_automigrate(?)", ("""
        CREATE TABLE IF NOT EXISTS k (id);
        CREATE TABLE IF NOT EXISTS bad (x);
        CREATE TABLE IF NOT EXISTS a (id PRIMARY KEY NOT NULL, x, y NOT NULL);
    """,))
    # re-applying the schema fails (no IF NOT EXISTS): the automigrate savepoint must not be
    # left open as a dangling transaction
    with pytest.raises(sqlite3.Error):
        c.execute("SELECT crsql_automigrate(?)", ("""
            CREATE TABLE k (id);
            CREATE TABLE IF NOT EXISTS bad (x);
            CREATE TABLE IF NOT EXISTS a (id PRIMARY KEY NOT NULL, x);
        """,))
    assert not c.in_transaction
    close(c)

    # bootstrap: loading the extension into a db written by an unsupported version fails.
    # Run isolated and skip interpreter teardown: a failed load leaves functions registered that
    # point into the unloaded library (a separate init bug), which can crash at teardown.
    path = str(tmp_path / "old.db")
    r = run_isolated(f"""
import os
raw = sqlite3.connect({path!r}, isolation_level=None)
raw.execute("CREATE TABLE crsql_master (key TEXT PRIMARY KEY, value ANY)")
raw.execute("INSERT INTO crsql_master VALUES ('crsqlite_version', 1)")
raw.execute("CREATE TABLE k (id)")
raw.execute("BEGIN")
raw.execute("INSERT INTO k VALUES (1)")
raw.enable_load_extension(True)
try:
    raw.load_extension(EXT)
    print("loaded")
except sqlite3.Error as e:
    print("load failed")
print("in_tx", raw.in_transaction)
print("count", raw.execute("SELECT count(*) FROM k").fetchone()[0])
raw.execute("ROLLBACK")
print("done", flush=True)
os._exit(0)
""")
    assert r.returncode == 0, r.stderr
    assert "load failed" in r.stdout
    assert "in_tx True" in r.stdout
    assert "count 1" in r.stdout
    assert "done" in r.stdout


@pytest.mark.parametrize("tbl", ["my table", "order", "a'b", 'a"b'])
def test_identifier_quoting(tbl):
    """Table / column names were not quoted or were escaped for the wrong context in
    backfill.rs (fill_column), tableinfo.rs (pull_table_info, is_table_compatible),
    bootstrap.rs (__crsql_pks), triggers.rs (insert/delete trigger) and alter.rs (pk cols)."""
    pk = 'i"d'
    x = "x'y"
    y = 'q"r'
    new_col = "n'e\"w"

    def setup():
        c = conn()
        c.execute(
            f"CREATE TABLE {q(tbl)} ({q(pk)} PRIMARY KEY NOT NULL, {q(x)}, {q(y)} DEFAULT 3)")
        return c

    a = setup()
    a.execute(f"INSERT INTO {q(tbl)} VALUES (1, 'a', 'b')")
    a.execute("SELECT crsql_as_crr(?)", (tbl,))  # backfill
    a.execute(f"INSERT INTO {q(tbl)} VALUES (2, 'c', 'd')")
    a.execute(f"INSERT INTO {q(tbl)} VALUES (3, 'e', 'f')")
    a.execute(f"UPDATE {q(tbl)} SET {q(x)} = 'A' WHERE {q(pk)} = 1")
    a.execute(f"DELETE FROM {q(tbl)} WHERE {q(pk)} = 2")
    changes = a.execute(
        "SELECT [table], pk, cid, val, col_version, db_version, site_id, cl, seq, ts FROM crsql_changes").fetchall()
    assert {r[0] for r in changes} == {tbl}
    assert {r[2] for r in changes} == {x, y, '-1'}

    b = setup()
    b.execute("SELECT crsql_as_crr(?)", (tbl,))
    b.executemany(
        "INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", changes)
    expected = [(1, 'A', 'b'), (3, 'e', 'f')]
    assert b.execute(f"SELECT * FROM {q(tbl)} ORDER BY 1").fetchall() == expected

    a.execute("SELECT crsql_begin_alter(?)", (tbl,))
    a.execute(f"ALTER TABLE {q(tbl)} ADD COLUMN {q(new_col)}")
    a.execute("SELECT crsql_commit_alter(?)", (tbl,))
    a.execute(f"UPDATE {q(tbl)} SET {q(new_col)} = 9 WHERE {q(pk)} = 3")
    assert a.execute(
        "SELECT cid, val FROM crsql_changes WHERE cid = ?", (new_col,)).fetchall() == [(new_col, 9)]
    close(a)
    close(b)


def test_identifier_quoting_pk_col_commit_alter():
    """alter.rs compact_post_alter used the pk column name unescaped."""
    c = conn()
    c.execute('CREATE TABLE t ("a""b" PRIMARY KEY NOT NULL, x)')
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("INSERT INTO t VALUES (1, 1)")
    c.execute("SELECT crsql_begin_alter('t')")
    c.execute("ALTER TABLE t ADD COLUMN y")
    c.execute("SELECT crsql_commit_alter('t')")
    c.execute("UPDATE t SET y = 2")
    assert ('y', 2) in c.execute("SELECT cid, val FROM crsql_changes").fetchall()
    close(c)


def test_automigrate_detects_index_column_change():
    """automigrate.rs maybe_recreate_index never bound the index name to IDX_COLS_SQL,
    so a change of the indexed columns went unnoticed."""
    c = conn()
    c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x, y)")
    c.execute("CREATE INDEX ai ON a (x)")
    c.execute("SELECT crsql_automigrate(?)", ("""
        CREATE TABLE IF NOT EXISTS a (id PRIMARY KEY NOT NULL, x, y);
        CREATE INDEX IF NOT EXISTS ai ON a (y);
    """,))
    assert c.execute("SELECT name FROM pragma_index_info('ai')").fetchall() == [('y',)]
    close(c)


def test_automigrate_drop_crr_and_clock_table_discovery():
    """automigrate.rs drop_tables left a__crsql_clock / a__crsql_pks behind; the
    `LIKE '%__crsql_clock'` discovery in tableinfo.rs matched user tables and could slice a
    UTF-8 name mid-character; a clock table without its base table made every crsql_changes
    read ABORT."""
    c = conn()
    c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('a')")
    c.execute("CREATE TABLE b (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('b')")
    c.execute("INSERT INTO a VALUES (1, 1)")
    c.execute("INSERT INTO b VALUES (1, 1)")
    c.execute("SELECT crsql_automigrate(?)",
              ("CREATE TABLE IF NOT EXISTS b (id PRIMARY KEY NOT NULL, x);",))
    names = [r[0] for r in c.execute("SELECT name FROM sqlite_master")]
    assert not [n for n in names if n.startswith("a__crsql")]
    assert c.execute("SELECT [table], cid FROM crsql_changes").fetchall() == [('b', 'x')]
    close(c)

    # user table that only matches `%__crsql_clock` through the `_` wildcard
    c = conn()
    c.execute("CREATE TABLE my_crsql_clock (id)")
    c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('a')")
    c.execute("INSERT INTO a VALUES (1, 1)")
    assert c.execute("SELECT [table], cid FROM crsql_changes").fetchall() == [('a', 'x')]

    # clock table left without its base table
    c.execute("CREATE TABLE b (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('b')")
    c.execute("INSERT INTO b VALUES (1, 1)")
    c.execute("DROP TABLE b")
    assert c.execute("SELECT [table], cid FROM crsql_changes").fetchall() == [('a', 'x')]
    close(c)

    # multi-byte character in the position the wildcard matched: used to panic
    r = run_isolated("""
c = conn()
c.execute("CREATE TABLE \\"a日crsql_clock\\" (id)")
c.execute("CREATE TABLE a (id PRIMARY KEY NOT NULL, x)")
c.execute("SELECT crsql_as_crr('a')")
c.execute("INSERT INTO a VALUES (1, 1)")
print(c.execute("SELECT [table] FROM crsql_changes").fetchall())
""")
    assert r.returncode == 0, r.stderr
    assert "[('a',)]" in r.stdout


def test_finalize_releases_col_site_id_stmt():
    """tableinfo.rs clear_stmts never finalized col_site_id_stmt, so sqlite3_close
    returned SQLITE_BUSY after crsql_finalize() once an equal-value merge had run."""
    lib = ctypes.CDLL(ctypes.util.find_library("sqlite3"))
    lib.sqlite3_errmsg.restype = ctypes.c_char_p
    db = ctypes.c_void_p()
    assert lib.sqlite3_open(b":memory:", ctypes.byref(db)) == 0
    lib.sqlite3_enable_load_extension(db, 1)
    err = ctypes.c_char_p()
    assert lib.sqlite3_load_extension(db, EXT.encode(), None, ctypes.byref(err)) == 0, err.value

    def ex(sql):
        rc = lib.sqlite3_exec(db, sql, None, None, ctypes.byref(err))
        assert rc == 0, (sql, err.value)

    ex(b"SELECT crsql_config_set('merge-equal-values', 1)")
    ex(b"CREATE TABLE t (id PRIMARY KEY NOT NULL, x); SELECT crsql_as_crr('t'); INSERT INTO t VALUES (1, 1);")
    # same value + same version from another site -> site id tie-break path
    ex(b"INSERT INTO crsql_changes ([table], pk, cid, val, col_version, db_version, site_id, cl, seq) "
       b"SELECT [table], pk, cid, val, col_version, db_version, X'01010101010101010101010101010101', cl, seq "
       b"FROM crsql_changes")
    ex(b"SELECT crsql_finalize()")
    assert lib.sqlite3_close(db) == 0


def test_clset_vtab_eof_and_destroy():
    """create_cl_set_vtab.rs xEof always returned 0 (any scan of foo_schema looped
    forever) and xDestroy freed the vtab before the DROPs succeeded, so a failed DROP was
    followed by a double free on xDisconnect."""
    r = run_isolated("""
c = conn()
c.execute("CREATE VIRTUAL TABLE foo_schema USING clset(id PRIMARY KEY NOT NULL, x)")
print(c.execute("SELECT count(*) FROM foo_schema").fetchall())
""", timeout=10)
    assert r.returncode == 0, r.stderr
    assert "[(0,)]" in r.stdout

    r = run_isolated("""
c = conn()
c.execute("CREATE VIRTUAL TABLE foo_schema USING clset(id PRIMARY KEY NOT NULL, x)")
c.execute("INSERT INTO foo VALUES (1, 1)")
c.execute("INSERT INTO foo VALUES (2, 2)")
cur = c.execute("SELECT * FROM foo")
cur.fetchone()  # active read stmt -> DROP TABLE foo fails with SQLITE_LOCKED
try:
    c.execute("DROP TABLE foo_schema")
    print("dropped")
except sqlite3.Error as e:
    print("drop failed", e)
cur.close()
del cur
c.execute("DROP TABLE foo_schema")
print([r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name LIKE 'foo%'")])
c.execute("SELECT crsql_finalize()")
c.close()
print("closed")
""", timeout=10)
    assert r.returncode == 0, r.stderr
    assert "drop failed" in r.stdout
    assert "[]" in r.stdout
    assert "closed" in r.stdout


def test_table_name_matching_is_case_insensitive():
    """Table names were matched case-sensitively (local_writes/mod.rs, alter.rs,
    changes_vtab_write.rs, is_crr) although SQLite identifiers are case-insensitive."""
    a = conn()
    b = conn()
    a.execute("CREATE TABLE Foo (id PRIMARY KEY NOT NULL, x)")
    a.execute("SELECT crsql_as_crr('Foo')")
    b.execute("CREATE TABLE Foo (id PRIMARY KEY NOT NULL, x)")
    b.execute("SELECT crsql_as_crr('foo')")
    a.execute("INSERT INTO Foo VALUES (1, 1)")
    rows = a.execute(
        "SELECT [table], pk, cid, val, col_version, db_version, site_id, cl, seq, ts FROM crsql_changes").fetchall()
    b.executemany(
        "INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    assert b.execute("SELECT * FROM Foo").fetchall() == [(1, 1)]
    close(a)
    close(b)

    c = conn()
    c.execute("CREATE TABLE foo (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('FOO')")
    c.execute("INSERT INTO foo VALUES (1, 1)")
    c.execute("SELECT crsql_begin_alter('foo')")
    c.execute("ALTER TABLE foo ADD COLUMN y")
    c.execute("SELECT crsql_commit_alter('foo')")
    c.execute("UPDATE foo SET y = 2")
    assert ('y', 2) in c.execute("SELECT cid, val FROM crsql_changes").fetchall()
    # as_crr under another spelling is a no-op, not a second set of triggers
    c.execute("SELECT crsql_as_crr('Foo')")
    assert c.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 3
    close(c)


def test_generated_not_null_column_is_compatible():
    """tableinfo.rs is_table_compatible used pragma_table_xinfo, so a NOT NULL
    generated column (excluded from non_pks) got the table rejected."""
    c = conn()
    c.execute(
        "CREATE TABLE t (id PRIMARY KEY NOT NULL, x, g GENERATED ALWAYS AS (x + 1) NOT NULL)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("INSERT INTO t (id, x) VALUES (1, 1)")
    assert c.execute("SELECT cid, val FROM crsql_changes").fetchall() == [('x', 1)]
    close(c)


def test_non_main_schema_is_rejected():
    """create_crr.rs ignored the schema argument of crsql_as_crr / commit_alter, so
    tables in attached databases were mishandled. Non-main schemas are now rejected."""
    c = conn()
    c.execute("ATTACH ':memory:' AS aux")
    c.execute("CREATE TABLE aux.t (id PRIMARY KEY NOT NULL, x)")
    with pytest.raises(sqlite3.Error, match="schema"):
        c.execute("SELECT crsql_as_crr('aux', 't')")
    with pytest.raises(sqlite3.Error, match="schema"):
        c.execute("SELECT crsql_commit_alter('aux', 't')")
    assert c.execute(
        "SELECT count(*) FROM main.sqlite_master WHERE name LIKE 't%'").fetchone()[0] == 0

    c.execute("CREATE TABLE main.u (id PRIMARY KEY NOT NULL, x)")
    c.execute("SELECT crsql_as_crr('main', 'u')")
    c.execute("SELECT crsql_as_crr('MAIN', 'u')")
    c.execute("INSERT INTO u VALUES (1, 1)")
    assert c.execute("SELECT [table] FROM crsql_changes").fetchall() == [('u',)]
    close(c)
