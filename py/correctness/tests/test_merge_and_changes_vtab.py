import os
import subprocess
import sys

import pytest
import sqlite3

from crsql_correctness import connect, close, extension

SITE_1 = bytes.fromhex("00000000000000000000000000000001")
SITE_2 = bytes.fromhex("00000000000000000000000000000002")
PK_1 = b'\x01\x09\x01'  # crsql_pack_columns(1)


def merge(c, row):
    c.execute("INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)


def sync_bit(c):
    return c.execute("SELECT crsql_internal_sync_bit()").fetchone()[0]


def run_isolated(body):
    """Runs `body` in a fresh interpreter so a panic (panic=abort) can't take pytest down."""
    code = (
        "import sqlite3\n"
        "c = sqlite3.connect(':memory:', isolation_level=None)\n"
        "c.enable_load_extension(True)\n"
        f"c.load_extension({os.path.abspath(extension)!r})\n"
        "c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)')\n"
        "c.execute(\"SELECT crsql_as_crr('t')\")\n"
        + body
    )
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)


def assert_sync_bit_cleared_and_local_writes_tracked(c):
    assert sync_bit(c) == 0
    c.execute("INSERT INTO t VALUES (1000, 1)")
    assert c.execute(
        "SELECT count(*) FROM crsql_changes WHERE pk = crsql_pack_columns(1000)").fetchone()[0] > 0


def test_sync_bit_cleared_after_failed_column_merge():
    """A failed column merge leaves the sync bit set."""
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v INTEGER CHECK (v > 0))")
    c.execute("SELECT crsql_as_crr('t')")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        merge(c, ('t', PK_1, 'v', -1, 1, 1, SITE_1, 1, 0, '0'))
    assert_sync_bit_cleared_and_local_writes_tracked(c)
    close(c)


def test_sync_bit_cleared_after_failed_sentinel_merge():
    """A failed pk-only/resurrect merge leaves the sync bit set."""
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute(
        "CREATE TRIGGER t_no_99 BEFORE INSERT ON t WHEN NEW.id = 99 BEGIN SELECT RAISE(ABORT, 'no 99'); END")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        merge(c, ('t', b'\x01\x09\x63', '-1', None, 1, 1, SITE_1, 1, 0, '0'))
    assert_sync_bit_cleared_and_local_writes_tracked(c)
    close(c)


def test_sync_bit_cleared_after_failed_delete_merge():
    """A failed delete merge leaves the sync bit set."""
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("INSERT INTO t VALUES (1, 'a')")
    c.execute(
        "CREATE TRIGGER t_no_del BEFORE DELETE ON t WHEN OLD.id = 1 BEGIN SELECT RAISE(ABORT, 'no del'); END")
    c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        merge(c, ('t', PK_1, '-1', None, 2, 5, SITE_1, 2, 0, '0'))
    assert_sync_bit_cleared_and_local_writes_tracked(c)
    close(c)


@pytest.mark.parametrize("body", [
    # empty pk
    "c.execute(\"INSERT INTO crsql_changes VALUES ('t', x'', 'v', 1, 1, 1, x'00000000000000000000000000000001', 1, 0, '0')\")",
    # NULL pk
    "c.execute(\"INSERT INTO crsql_changes VALUES ('t', NULL, 'v', 1, 1, 1, x'00000000000000000000000000000001', 1, 0, '0')\")",
    # integer with a 9 byte length
    "c.execute(\"INSERT INTO crsql_changes VALUES ('t', x'0149000000000000000001', 'v', 1, 1, 1, x'00000000000000000000000000000001', 1, 0, '0')\")",
    # blob/text with a 31 byte length prefix (and enough bytes after it)
    "c.execute(\"INSERT INTO crsql_changes VALUES ('t', x'01fc00000000000000000000000000000000000000000000000000000000000000000000000000000000', 'v', 1, 1, 1, x'00000000000000000000000000000001', 1, 0, '0')\")",
    "c.execute(\"INSERT INTO crsql_changes VALUES ('t', x'01fb00000000000000000000000000000000000000000000000000000000000000000000000000000000', 'v', 1, 1, 1, x'00000000000000000000000000000001', 1, 0, '0')\")",
    # zero pk columns
    "c.execute(\"INSERT INTO crsql_changes VALUES ('t', x'00', 'v', 1, 1, 1, x'00000000000000000000000000000001', 1, 0, '0')\")",
    "c.execute(\"SELECT * FROM crsql_unpack_columns(x'')\").fetchall()",
    "c.execute(\"SELECT * FROM crsql_unpack_columns(x'0149000000000000000001')\").fetchall()",
], ids=["empty_pk", "null_pk", "intlen_9", "textlen_31", "bloblen_31", "zero_cols", "unpack_empty", "unpack_intlen_9"])
def test_malformed_pk_blob_is_an_error_not_a_crash(body):
    """Malformed packed pks panic the process."""
    r = run_isolated(
        "try:\n"
        "    " + body + "\n"
        "except sqlite3.Error as e:\n"
        "    print('error', e)\n"
        "print('survived')\n")
    assert r.returncode == 0, r.stderr
    assert "survived" in r.stdout
    assert "error" in r.stdout


def test_pack_unpack_roundtrip_lengths():
    """Blob lengths are read signed; must round trip for all sizes."""
    c = connect(":memory:")
    for n in [0, 1, 127, 128, 200, 255, 256, 40000, 70000]:
        b = bytes(i % 251 for i in range(n))
        t = 'x' * n
        got = c.execute(
            "SELECT cell FROM crsql_unpack_columns(crsql_pack_columns(?, ?, ?, ?))",
            (b, t, n, -n)).fetchall()
        assert got == [(b,), (t,), (n,), (-n,)]
    close(c)


def test_unpack_columns_results_are_not_dangling():
    """Text/blob results point into freed cursor memory."""
    c = connect(":memory:")
    c.execute("CREATE TABLE p (x)")
    c.execute("INSERT INTO p SELECT crsql_pack_columns('zzzz-' || printf('%040d', 1))")
    for i in range(2, 50):
        c.execute("INSERT INTO p SELECT crsql_pack_columns('aaaa-' || printf('%040d', ?))", (i,))
    c.execute("INSERT INTO p SELECT crsql_pack_columns(CAST(x'ffff' || randomblob(40) AS BLOB))")
    for i in range(2, 50):
        c.execute("INSERT INTO p SELECT crsql_pack_columns(CAST(x'0000' || randomblob(40) AS BLOB))")
    expected_text = 'zzzz-' + '%040d' % 1
    for _ in range(3):
        assert c.execute(
            "SELECT max(u.cell) FROM p CROSS JOIN crsql_unpack_columns(p.x) u WHERE typeof(u.cell) = 'text'"
        ).fetchone()[0] == expected_text
        assert c.execute(
            "SELECT hex(max(u.cell)) FROM p CROSS JOIN crsql_unpack_columns(p.x) u WHERE typeof(u.cell) = 'blob'"
        ).fetchone()[0].startswith("FFFF")
    close(c)


def make_two_row_db():
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v, w)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("INSERT INTO t VALUES (1, 'a1', 'b1'), (2, 'a2', 'b2')")
    c.commit()
    return c


def test_changes_cursors_do_not_share_row_stmt():
    """Cursors share the cached row stmt (self join / subquery)."""
    c = make_two_row_db()
    self_join = c.execute(
        "SELECT a.pk, a.val, b.pk, b.val FROM crsql_changes a, crsql_changes b "
        "WHERE a.cid = 'v' AND b.cid = 'v' ORDER BY a.pk, b.pk").fetchall()
    p1 = c.execute("SELECT crsql_pack_columns(1)").fetchone()[0]
    p2 = c.execute("SELECT crsql_pack_columns(2)").fetchone()[0]
    assert self_join == [(p1, 'a1', p1, 'a1'), (p1, 'a1', p2, 'a2'),
                         (p2, 'a2', p1, 'a1'), (p2, 'a2', p2, 'a2')]

    sub = c.execute(
        "SELECT a.val, (SELECT group_concat(b.val, ',') FROM crsql_changes b WHERE b.cid = 'w') "
        "FROM crsql_changes a WHERE a.cid = 'v' ORDER BY a.pk").fetchall()
    assert sub == [('a1', 'b1,b2'), ('a2', 'b1,b2')]

    corr = c.execute(
        "SELECT a.val, (SELECT b.val FROM crsql_changes b WHERE b.cid = 'w' AND b.pk = a.pk) "
        "FROM crsql_changes a WHERE a.cid = 'v' ORDER BY a.pk").fetchall()
    assert corr == [('a1', 'b1'), ('a2', 'b2')]
    close(c)


def test_changes_mixed_direction_order_by():
    """One DESC/ASC applied to every ORDER BY term."""
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v, w)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("INSERT INTO t VALUES (1, 'a1', 'b1'), (2, 'a2', 'b2')")
    c.commit()
    c.execute("INSERT INTO t VALUES (3, 'a3', 'b3')")
    c.commit()
    rows = c.execute(
        "SELECT db_version, seq FROM crsql_changes ORDER BY db_version DESC, seq ASC").fetchall()
    assert rows == sorted(rows, key=lambda r: (-r[0], r[1]))
    rows = c.execute(
        "SELECT db_version, seq FROM crsql_changes ORDER BY db_version ASC, seq DESC").fetchall()
    assert rows == sorted(rows, key=lambda r: (r[0], -r[1]))
    close(c)


def test_changes_order_by_untranslatable_column():
    """ORDER BY val builds `ORDER BY  ASC` -> SQL logic error."""
    c = make_two_row_db()
    assert c.execute("SELECT val FROM crsql_changes WHERE cid = 'v' ORDER BY val DESC").fetchall() == [
        ('a2',), ('a1',)]
    assert c.execute(
        "SELECT val FROM crsql_changes WHERE cid = 'w' ORDER BY db_version, val DESC").fetchall() == [
        ('b2',), ('b1',)]
    assert len(c.execute("SELECT * FROM crsql_changes ORDER BY rowid").fetchall()) == 4
    close(c)


def test_empty_site_id_merge_updates_cl_cache():
    """Empty site_id merges leave a stale cl cache."""
    c = make_two_row_db()
    c.execute("BEGIN")
    # delete (cl 2) with an empty site id
    merge(c, ('t', PK_1, '-1', None, 2, 5, b'', 2, 0, '0'))
    assert c.execute("SELECT id FROM t WHERE id = 1").fetchall() == []
    # an older cl=1 change must be ignored, not resurrect the row
    merge(c, ('t', PK_1, 'w', 'zzz', 9, 4, SITE_1, 1, 0, '0'))
    c.execute("COMMIT")
    assert c.execute("SELECT id FROM t WHERE id = 1").fetchall() == []
    close(c)


def test_empty_site_id_merge_tie_break():
    """Empty site_id writes a NULL clock site_id which
    later fails a mergeEqualValues tie with 'clock table might be corrupt'."""
    c = connect(":memory:")
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("SELECT crsql_config_set('merge-equal-values', 1)")
    c.commit()
    merge(c, ('t', PK_1, 'v', 'x', 1, 1, b'', 1, 0, '0'))
    c.commit()
    # site ordinal must be the local one (0), like local writes, never NULL
    assert c.execute("SELECT site_id FROM t__crsql_clock WHERE col_name = 'v'").fetchall() == [(0,)]
    merge(c, ('t', PK_1, 'v', 'x', 1, 1, SITE_1, 1, 0, '0'))
    c.commit()
    assert c.execute("SELECT v FROM t").fetchall() == [('x',)]
    close(c)


def test_unpack_columns_best_index_join():
    """Unusable package constraint not rejected."""
    c = connect(":memory:")
    c.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, x)")
    c.execute("INSERT INTO p VALUES (1, crsql_pack_columns('a', 2)), (2, crsql_pack_columns(3))")
    assert c.execute(
        "SELECT p.id, u.cell FROM p, crsql_unpack_columns(p.x) u ORDER BY p.id, u.rowid").fetchall() == [
        (1, 'a'), (1, 2), (2, 3)]
    assert c.execute(
        "SELECT p.id, u.cell FROM crsql_unpack_columns(p.x) u, p ORDER BY p.id, u.rowid").fetchall() == [
        (1, 'a'), (1, 2), (2, 3)]
    # constraints on `cell` are left to sqlite
    assert c.execute(
        "SELECT p.id FROM p, crsql_unpack_columns(p.x) u WHERE u.cell = 2").fetchall() == [(1,)]
    close(c)


def test_did_cid_win_applies_column_affinity():
    """Value compared before column affinity -> divergence."""
    def node():
        c = connect(":memory:")
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY NOT NULL, v TEXT, n NUMERIC, r REAL)")
        c.execute("SELECT crsql_as_crr('t')")
        c.commit()
        return c

    changes = [
        [('t', PK_1, 'v', 'abc', 1, 1, SITE_1, 1, 0, '0'),
         ('t', PK_1, 'v', 5, 1, 1, SITE_2, 1, 0, '0')],
        [('t', PK_1, 'n', 'abc', 1, 1, SITE_1, 1, 0, '0'),
         ('t', PK_1, 'n', '7', 1, 1, SITE_2, 1, 0, '0')],
        [('t', PK_1, 'n', 7, 1, 1, SITE_1, 1, 0, '0'),
         ('t', PK_1, 'n', '7.0', 1, 1, SITE_2, 1, 0, '0'),
         ('t', PK_1, 'n', 6.5, 1, 1, SITE_2, 1, 0, '0')],
        [('t', PK_1, 'r', 7, 1, 1, SITE_1, 1, 0, '0'),
         ('t', PK_1, 'r', 6.5, 1, 1, SITE_2, 1, 0, '0')],
        [('t', PK_1, 'r', 'abc', 1, 1, SITE_1, 1, 0, '0'),
         ('t', PK_1, 'r', ' 8 ', 1, 1, SITE_2, 1, 0, '0')],
    ]
    for group in changes:
        a = node()
        b = node()
        for ch in group:
            merge(a, ch)
        for ch in reversed(group):
            merge(b, ch)
        a.commit()
        b.commit()
        q = "SELECT v, typeof(v), n, typeof(n), r, typeof(r) FROM t"
        assert a.execute(q).fetchall() == b.execute(q).fetchall(), group
        close(a)
        close(b)
