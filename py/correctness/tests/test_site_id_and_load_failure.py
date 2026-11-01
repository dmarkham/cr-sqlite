"""Tests for init failure under SELECT load_extension(), merges with an empty
or NULL site_id, crsql_cache_pk_cl table names and backfill rollback errors.

Anything that used to crash the process runs in a subprocess so a crash shows
up as a failed assertion instead of taking pytest down with it.
"""
import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from crsql_correctness import connect, close

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
EXT = os.path.join(REPO, "core", "dist", "crsqlite")

SITE_1 = bytes.fromhex("00000000000000000000000000000001")
PK_1 = b'\x01\x09\x01'  # crsql_pack_columns(1)


def run_py(code, env=None):
    prelude = textwrap.dedent(f"""
        import sqlite3, sys, os
        EXT = {EXT!r}
        def err(c, sql, *args):
            try:
                r = c.execute(sql, args).fetchall()
                return ("OK", r)
            except Exception as e:
                return ("ERR", str(e))
    """)
    full_env = dict(os.environ)
    full_env.pop("LD_LIBRARY_PATH", None)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(code)],
        capture_output=True,
        text=True,
        env=full_env,
        timeout=120,
    )


def make_unsupported_version_db(path):
    c = connect(path)
    close(c)
    raw = sqlite3.connect(path)
    raw.execute(
        "UPDATE crsql_master SET value = 1 WHERE key = 'crsqlite_version'")
    raw.commit()
    raw.close()


def test_sql_load_extension_failure_does_not_crash(tmp_path):
    """When the
    extension is loaded with `SELECT load_extension(?)` the load_extension
    statement is running, so SQLite refuses to drop the functions registered
    before the failure ("unable to delete/modify user-function due to active
    statements"), then unloads the .so anyway. The leftover functions and
    destructors point into unmapped code: the next call or close crashed."""
    path = str(tmp_path / "old_version.db")
    make_unsupported_version_db(path)
    p = run_py("""
        c = sqlite3.connect(os.environ["CRSQL_DB"])
        c.enable_load_extension(True)
        print(repr(err(c, "SELECT load_extension(?)", EXT)))
        print(repr(err(c, "SELECT crsql_internal_sync_bit()")))
        print(repr(err(c, "SELECT crsql_set_debug(0)")))
        print(repr(err(c, "SELECT crsql_fract_key_between(NULL, NULL)")))
        print(repr(err(c, "SELECT crsql_site_id()")))
        # A second attempt must fail the same way, not crash.
        print(repr(err(c, "SELECT load_extension(?)", EXT)))
        c.close()
        print(repr(("closed", "")))
    """, env={"CRSQL_DB": path})
    assert p.returncode == 0, p.stdout + p.stderr
    lines = [eval(l) for l in p.stdout.strip().splitlines()]
    assert lines[0][0] == "ERR" and "not supported" in lines[0][1], lines[0]
    for status, msg in lines[1:5]:
        assert status == "ERR" and "no such function" in msg, msg
    assert lines[5][0] == "ERR" and "not supported" in lines[5][1], lines[5]
    assert lines[6][0] == "closed"


def test_merge_rejects_empty_or_null_site_id():
    """A merge with an empty or NULL
    site_id was taken as our own change but did not advance our db_version.
    It is now rejected."""
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
    c.execute("SELECT crsql_as_crr('t')")
    c.commit()
    for site_id in (None, b''):
        with pytest.raises(sqlite3.Error, match="site_id must be a non-empty blob"):
            c.execute(
                "INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ('t', PK_1, 'v', 'x', 1, 5, site_id, 1, 0, '0'))
        c.rollback()
    assert c.execute("SELECT count(*) FROM t").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM t__crsql_clock").fetchone()[0] == 0
    close(c)


def test_null_site_id_merge_cannot_reuse_db_version():
    """The self-attributed clock rows of a NULL site_id merge carried
    the remote's db_version (5) while our db_version stayed at 0, so our next
    local write got db_version 1 and a peer that already synced us up to
    db_version 5 never received it."""
    a = connect(":memory:")
    b = connect(":memory:")
    for c in (a, b):
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
        c.execute("SELECT crsql_as_crr('t')")
        c.commit()
    try:
        a.execute(
            "INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('t', PK_1, 'v', 'x', 1, 5, None, 1, 0, '0'))
        a.commit()
    except sqlite3.Error:
        a.rollback()

    a_site = a.execute("SELECT crsql_site_id()").fetchone()[0]

    def sync_a_to_b(since):
        rows = a.execute(
            "SELECT \"table\", pk, cid, val, col_version, db_version, site_id, cl, seq, ts "
            "FROM crsql_changes WHERE db_version > ? AND site_id = ?",
            (since, a_site)).fetchall()
        for r in rows:
            b.execute("INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", r)
        b.commit()

    # b pulls everything a has written so far and remembers how far it got.
    sync_a_to_b(0)
    seen = b.execute(
        "SELECT coalesce(max(db_version), 0) FROM crsql_db_versions WHERE site_id = ?",
        (a_site,)).fetchone()[0]

    # a local write on a must be newer than anything a already handed out.
    a.execute("INSERT INTO t VALUES (2, 'local')")
    a.commit()
    sync_a_to_b(seen)
    assert b.execute("SELECT id, v FROM t ORDER BY id").fetchall() == \
        a.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    close(a)
    close(b)


def test_backfill_rollback_failure_keeps_original_error():
    """When the backfill failed and the
    `ROLLBACK TO backfill` failed as well, `?` returned the rollback's error
    and the original error was lost. Here a RAISE(ROLLBACK) trigger on the
    pks table ends the whole transaction (SQLITE_CONSTRAINT), so the backfill
    savepoint is gone and the rollback fails with SQLITE_ERROR."""
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.enable_load_extension(True)
    c.load_extension(EXT)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
    c.execute("INSERT INTO t VALUES (1, 'a')")
    c.execute("CREATE TABLE t__crsql_pks (__crsql_key INTEGER PRIMARY KEY, \"id\")")
    c.execute(
        "CREATE TRIGGER no_pks BEFORE INSERT ON t__crsql_pks "
        "BEGIN SELECT RAISE(ROLLBACK, 'no backfill today'); END")
    c.execute("BEGIN")
    with pytest.raises(sqlite3.Error) as e:
        c.execute("SELECT crsql_as_crr('t')")
    assert e.value.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_TRIGGER \
        or e.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_CONSTRAINT, \
        (e.value.sqlite_errorcode, e.value.sqlite_errorname, str(e.value))
    c.close()
