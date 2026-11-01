"""Tests for argument handling, fractional index,
extension init failure paths, function flags and build issues.

Anything that used to crash the process runs in a subprocess so a crash shows
up as a failed assertion instead of taking pytest down with it.
"""
import os
import re
import subprocess
import sys
import textwrap

import pytest

from crsql_correctness import connect, close

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
CORE = os.path.join(REPO, "core")
EXT = os.path.join(CORE, "dist", "crsqlite")
SO = EXT + ".so"


def run_py(code, env=None):
    """Run `code` in a fresh interpreter that has `EXT` and `connect` defined.
    Returns the CompletedProcess."""
    prelude = textwrap.dedent(f"""
        import sqlite3, sys, os
        EXT = {EXT!r}
        def connect(path, **kw):
            c = sqlite3.connect(path, **kw)
            c.enable_load_extension(True)
            c.load_extension(EXT)
            return c
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


def test_empty_and_null_table_name_args():
    """sqlite3_capi::raw_slice (sqlite-rs-embedded capi.rs) returned a
    dangling `&[]` for '' and NULL text, and lib.rs
    passed `text().as_ptr()` to C as a NUL-terminated string, so
    crsql_as_crr('') / crsql_commit_alter('') / NULL table names crashed."""
    p = run_py("""
        c = connect(":memory:")
        out = []
        for sql in [
            "SELECT crsql_as_crr('')",
            "SELECT crsql_as_crr(NULL)",
            "SELECT crsql_as_crr('main', '')",
            "SELECT crsql_as_crr(NULL, 'foo')",
            "SELECT crsql_begin_alter('')",
            "SELECT crsql_begin_alter(NULL)",
            "SELECT crsql_commit_alter('')",
            "SELECT crsql_commit_alter(NULL)",
            "SELECT crsql_commit_alter('main', '')",
            "SELECT crsql_commit_alter('', 1, 1)",
        ]:
            out.append((sql, err(c, sql)))
        # The connection must still work for a normal table.
        c.execute("CREATE TABLE foo (a INTEGER PRIMARY KEY NOT NULL, b)")
        out.append(("as_crr foo", err(c, "SELECT crsql_as_crr('foo')")))
        for o in out:
            print(repr(o))
    """)
    assert p.returncode == 0, p.stdout + p.stderr
    lines = [eval(l) for l in p.stdout.strip().splitlines()]
    for sql, (status, msg) in lines[:-1]:
        assert status == "ERR", (sql, status, msg)
        assert "table name" in msg.lower(), (sql, msg)
    assert lines[-1][1] == ("OK", [("OK",)])


@pytest.mark.parametrize("a,b", [
    ("a0", "a0"),
    ("a0P", "a0P"),
    ("", None),
    (None, ""),
    ("", ""),
    ("aé", None),
    (None, "aé"),
    ("a0", "aé"),
    ("é", None),
    ("a1", "a0"),
    ("b125", "a0"),
    ("a0\x01", None),
])
def test_fract_key_between_bad_input(a, b):
    """crsql_fract_key_between (fractindex-core fractindex.rs)
    panicked (process abort) on equal keys, empty keys, non-ASCII keys and
    a >= b. It must return an SQL error instead."""
    p = run_py(f"""
        c = connect(":memory:")
        print(repr(err(c, "SELECT crsql_fract_key_between(?, ?)", {a!r}, {b!r})))
    """)
    assert p.returncode == 0, p.stdout + p.stderr
    status, _ = eval(p.stdout.strip())
    assert status == "ERR"


def test_fract_key_between_long_key():
    """fractindex.rs compared against `key.len() as u8`, which wraps,
    so valid keys of 256+ characters were rejected."""
    p = run_py("""
        c = connect(":memory:")
        # 256 and 257 wrap to 0 and 1 as u8, below the integer part length.
        for n in (254, 255):
            key = "a0" + "P" * n
            print(repr(err(c, "SELECT crsql_fract_key_between(?, NULL)", key)))
            print(repr(err(c, "SELECT crsql_fract_key_between(NULL, ?)", key)))
            print(repr(err(c, "SELECT crsql_fract_key_between('a0', ?)", key)))
    """)
    assert p.returncode == 0, p.stdout + p.stderr
    for line in p.stdout.strip().splitlines():
        status, rows = eval(line)
        assert status == "OK", rows
        res = rows[0][0]
        assert res is not None


def test_fract_pend_trigger_with_bad_synced_order():
    """The __crsql_<t>_fractindex_pend_trig trigger feeds the min/max
    order of the list to crsql_fract_key_between. A malformed order value
    (e.g. synced from a peer) must make the insert fail, not abort."""
    p = run_py("""
        c = connect(":memory:")
        c.execute("CREATE TABLE foo (a INTEGER PRIMARY KEY NOT NULL, spot TEXT, list)")
        c.execute("SELECT crsql_as_crr('foo')")
        c.execute("SELECT crsql_fract_as_ordered('foo', 'spot', 'list')")
        c.commit()
        # Simulate a bad value arriving from a peer.
        c.execute("INSERT INTO foo VALUES (1, 'aé', 'l')")
        c.commit()
        print(repr(err(c, "INSERT INTO foo VALUES (2, 1, 'l')")))
        c.rollback()
        c.execute("INSERT INTO foo VALUES (3, 'a0', 'm')")
        c.execute("INSERT INTO foo VALUES (4, '', 'n')")
        c.commit()
        print(repr(err(c, "INSERT INTO foo VALUES (5, -1, 'n')")))
        c.rollback()
        print(repr(err(c, "INSERT INTO foo VALUES (6, 1, 'm')")))
    """)
    assert p.returncode == 0, p.stdout + p.stderr
    lines = [eval(l) for l in p.stdout.strip().splitlines()]
    assert lines[0][0] == "ERR"
    assert lines[1][0] == "ERR"
    assert lines[2][0] == "OK"


def test_fract_fix_conflict_composite_pk():
    """fractindex_view.rs joined pk predicates with ", AND", so the
    conflict fixup for a composite primary key always failed with
    "Failed fixing up ordering conflicts on insert"."""
    c = connect(":memory:")
    c.execute(
        "CREATE TABLE foo (a INTEGER NOT NULL, b INTEGER NOT NULL, spot TEXT, list, PRIMARY KEY (a, b))")
    c.execute("SELECT crsql_as_crr('foo')")
    c.execute("SELECT crsql_fract_as_ordered('foo', 'spot', 'list')")
    c.commit()
    # Two rows share an order key -> inserting after one of them hits the
    # conflict path.
    c.execute("INSERT INTO foo VALUES (1, 1, 'a1', 'l')")
    c.execute("INSERT INTO foo VALUES (2, 2, 'a1', 'l')")
    c.execute("INSERT INTO foo VALUES (9, 9, 'a3', 'l')")
    c.commit()
    c.execute(
        "INSERT INTO foo_fractindex (a, b, list, after_a, after_b) VALUES (3, 3, 'l', 1, 1)")
    c.commit()
    rows = dict(((a, b), spot) for (a, b, spot) in c.execute(
        "SELECT a, b, spot FROM foo").fetchall())
    # (1, 1) moves down and the new row goes between it and (2, 2).
    assert rows[(1, 1)] < rows[(3, 3)] < rows[(2, 2)] == "a1"
    close(c)


def test_fract_update_trigger_escapes_identifiers():
    """fractindex_view.rs and util.rs where_predicates put
    table/column names into trigger SQL unescaped, so names containing a
    double quote broke crsql_fract_as_ordered."""
    c = connect(":memory:")
    c.execute(
        'CREATE TABLE "fo""o" ("i""d" INTEGER PRIMARY KEY NOT NULL, "sp""ot" TEXT, "li""st")')
    c.execute("SELECT crsql_fract_as_ordered('fo\"o', 'sp\"ot', 'li\"st')")
    c.commit()
    c.execute('INSERT INTO "fo""o" VALUES (1, -1, \'x\')')
    c.execute('INSERT INTO "fo""o" VALUES (2, 1, \'x\')')
    c.execute('INSERT INTO "fo""o" VALUES (3, 1, \'x\')')
    c.commit()
    c.execute('UPDATE "fo""o_fractindex" SET "after_i""d" = 1 WHERE "i""d" = 3')
    c.commit()
    rows = c.execute('SELECT "i""d" FROM "fo""o" ORDER BY "sp""ot"').fetchall()
    assert rows == [(1,), (3,), (2,)]


def _prepare_db(path, drop):
    import sqlite3
    c = connect(path)
    c.execute("CREATE TABLE foo (a INTEGER PRIMARY KEY NOT NULL, b)")
    c.execute("SELECT crsql_as_crr('foo')")
    c.commit()
    close(c)
    raw = sqlite3.connect(path)
    for sql in drop:
        raw.execute(sql)
    raw.commit()
    raw.close()


INIT_FAIL_CODE = """
    import ctypes
    lib = ctypes.CDLL("libsqlite3.so.0")
    lib.sqlite3_memory_used.restype = ctypes.c_int64
    path = os.environ["CRSQL_DB"]
    holder = sqlite3.connect(path, isolation_level=None)
    # A RESERVED lock: other connections can read but not write.
    holder.execute("BEGIN IMMEDIATE")

    def attempt():
        c = sqlite3.connect(path, timeout=0)
        c.enable_load_extension(True)
        try:
            c.load_extension(EXT)
            loaded = True
        except Exception as e:
            loaded = False
        # Keep using the connection after the failed init.
        for sql in ["SELECT crsql_update_site_id(x'0102', 7)",
                    "SELECT crsql_db_version()",
                    "SELECT crsql_site_id()",
                    "SELECT crsql_get_seq()"]:
            err(c, sql)
        c.close()
        return loaded

    attempt()
    before = lib.sqlite3_memory_used()
    loaded = [attempt() for _ in range(50)]
    after = lib.sqlite3_memory_used()
    holder.execute("ROLLBACK")
    holder.close()
    # After the lock is gone, the extension loads and works.
    c = connect(path)
    c.execute("INSERT INTO foo VALUES (1, 2)")
    c.commit()
    print(repr((any(loaded), after - before, c.execute("SELECT count(*) FROM crsql_changes").fetchone()[0])))
    c.execute("SELECT crsql_finalize()")
    c.close()
"""


@pytest.mark.parametrize("drop", [
    # init fails creating the site id table: ext_data was freed
    # while crsql_update_site_id still pointed at it.
    ["DROP TABLE crsql_site_id"],
    # init fails creating the site id triggers: site_id_buffer
    # was freed (ext_data still pointed at it) and ext_data leaked.
    ["DROP TRIGGER crsql_site_id_insert_trig",
     "DROP TRIGGER crsql_site_id_update_trig",
     "DROP TRIGGER crsql_site_id_delete_trig"],
], ids=["site_id_table", "site_id_triggers"])
def test_init_failure_paths(tmp_path, drop):
    """Init failure paths in lib.rs (sqlite3_crsqlcore_init)
    freed ext_data while registered functions still held it (UAF), freed
    site_id_buffer while ext_data still owned it and leaked ext_data with its
    prepared statements. Provoke the failure by holding an exclusive lock from
    another connection while the extension loads with busy_timeout 0."""
    path = str(tmp_path / "init_fail.db")
    _prepare_db(path, drop)
    p = run_py(INIT_FAIL_CODE, env={
        "CRSQL_DB": path,
        # Scribble over freed memory so a use-after-free shows up.
        "GLIBC_TUNABLES": "glibc.malloc.perturb=165",
    })
    assert p.returncode == 0, p.stdout + p.stderr
    loaded, growth, changes = eval(p.stdout.strip().splitlines()[-1])
    assert not loaded
    # 50 failed loads must not leak the ext data and its statements.
    assert growth < 50 * 256, growth
    assert changes == 2


def test_function_flags():
    """lib.rs registered crsql_internal_sync_bit as INNOCUOUS, so a
    view/trigger could set it even with trusted_schema=OFF; lib.rs
    and others registered state-changing / state-reading functions
    (crsql_as_crr, crsql_set_ts, crsql_get_seq, crsql_get_ts,
    crsql_config_get, crsql_set_db_version) as DETERMINISTIC."""
    c = connect(":memory:")
    c.execute("PRAGMA trusted_schema = OFF")
    c.execute("CREATE VIEW setter AS SELECT crsql_internal_sync_bit(1) AS x")
    with pytest.raises(Exception):
        c.execute("SELECT * FROM setter").fetchall()
    assert c.execute("SELECT crsql_internal_sync_bit()").fetchone()[0] == 0
    # Reading the bit from schema is what cr-sqlite's own triggers do.
    c.execute("CREATE VIEW getter AS SELECT crsql_internal_sync_bit() AS x")
    assert c.execute("SELECT * FROM getter").fetchall() == [(0,)]

    c.execute("PRAGMA trusted_schema = ON")
    with pytest.raises(Exception):
        c.execute("SELECT * FROM setter").fetchall()
    assert c.execute("SELECT crsql_internal_sync_bit()").fetchone()[0] == 0
    # Direct use still works.
    assert c.execute("SELECT crsql_internal_sync_bit(1)").fetchone()[0] == 1
    assert c.execute("SELECT crsql_internal_sync_bit(0)").fetchone()[0] == 0

    # Non-deterministic functions are rejected in generated columns.
    for expr in ["crsql_get_seq()", "crsql_get_ts()",
                 "crsql_config_get('merge-equal-values')",
                 "crsql_set_ts('1')", "crsql_as_crr('x')",
                 "crsql_set_db_version(x'00', 1)"]:
        c.execute("DROP TABLE IF EXISTS g")
        c.execute(f"CREATE TABLE g (a, b AS ({expr}))")
        with pytest.raises(Exception):
            c.execute("INSERT INTO g (a) VALUES (1)")
    close(c)


def test_as_crr_error_message_not_leaked():
    """crsql_as_crr leaked err_msg on failure (lib.rs x_crsql_as_crr)."""
    p = run_py("""
        import ctypes
        lib = ctypes.CDLL("libsqlite3.so.0")
        lib.sqlite3_memory_used.restype = ctypes.c_int64
        c = connect(":memory:")
        c.execute("CREATE TABLE nope (a)")
        for _ in range(100):
            err(c, "SELECT crsql_as_crr('nope')")
        before = lib.sqlite3_memory_used()
        for _ in range(2000):
            e = err(c, "SELECT crsql_as_crr('nope')")
        after = lib.sqlite3_memory_used()
        print(repr((e[0], after - before)))
    """)
    assert p.returncode == 0, p.stdout + p.stderr
    status, growth = eval(p.stdout.strip())
    assert status == "ERR"
    assert growth < 4096, growth


def test_set_debug_roundtrip():
    """debug.rs DEBUG_ENABLED was a process-wide `static mut`; it is
    now atomic. The race itself cannot be shown from Python; this only checks
    that behaviour is unchanged."""
    c = connect(":memory:")
    assert c.execute("SELECT crsql_set_debug(1)").fetchone()[0] == 1
    assert c.execute("SELECT crsql_set_debug(0)").fetchone()[0] == 0
    close(c)


def test_build_config():
    """core/rs/bundle/Cargo.lock pinned bindgen 0.68.1 while the other
    lockfiles use 0.72.1; core/Makefile's dbg static rule ignored
    rs_extra_features; core/rs/bundle/src/lib.rs used `Layout` without
    importing it; the loadable .so exported Rust allocator symbols."""
    def bindgen_versions(lock):
        with open(lock) as f:
            text = f.read()
        return set(re.findall(r'name = "bindgen"\nversion = "([^"]+)"', text))

    bundle = bindgen_versions(os.path.join(CORE, "rs", "bundle", "Cargo.lock"))
    check = bindgen_versions(os.path.join(
        CORE, "rs", "integration_check", "Cargo.lock"))
    assert bundle == check == {"0.72.1"}

    with open(os.path.join(CORE, "Makefile")) as f:
        mk = f.read()
    dbg_rule = mk.split("$(rs_lib_dbg_static_cpy): FORCE")[1].split("\n\n")[0]
    assert "$(rs_extra_features)" in dbg_rule

    with open(os.path.join(CORE, "rs", "bundle", "src", "lib.rs")) as f:
        lib = f.read()
    assert re.search(r"use (core|alloc)::alloc::Layout;", lib)

    out = subprocess.run(["nm", "-D", "--defined-only", SO],
                         capture_output=True, text=True, check=True).stdout
    exported = {l.split()[-1] for l in out.splitlines() if l.strip()}
    assert "sqlite3_crsqlite_init" in exported
    assert not any(s.startswith("__rust") or s.startswith("_ZN") for s in exported), \
        sorted(s for s in exported if s.startswith("__rust") or s.startswith("_ZN"))
