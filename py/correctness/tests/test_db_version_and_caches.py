import pathlib
import sqlite3

import pytest

from crsql_correctness import connect, close

CHANGES_INSERT = "INSERT INTO crsql_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"


def fresh(tmp_path, name):
    db_path = tmp_path / name
    for suffix in ("", "-wal", "-journal"):
        pathlib.Path(str(db_path) + suffix).unlink(missing_ok=True)
    c = connect(str(db_path))
    c.isolation_level = None
    return c, str(db_path)


def crr(c):
    c.execute("CREATE TABLE foo (a PRIMARY KEY NOT NULL, b)")
    c.execute("SELECT crsql_as_crr('foo')")


def merge(c, changes):
    for change in changes:
        c.execute(CHANGES_INSERT, change)


def test_next_db_version_in_autocommit_select(tmp_path):
    """crsql_enlist_db_version sets
    versionGuardEnlisted only after its nested INSERT already autocommitted
    when called from a read-only autocommit statement, so later local writes
    got db_version -1."""
    c, path = fresh(tmp_path, "v1.db")
    crr(c)
    c.execute("SELECT crsql_next_db_version()").fetchone()
    c.execute("INSERT INTO foo VALUES (1, 1)")
    c.execute("INSERT INTO foo VALUES (2, 2)")

    versions = [r[0] for r in c.execute(
        "SELECT db_version FROM foo__crsql_clock ORDER BY key")]
    assert all(v > 0 for v in versions), versions
    assert versions[0] < versions[1]
    db_version = c.execute("SELECT crsql_db_version()").fetchone()[0]
    assert db_version == versions[1]
    persisted = c.execute(
        "SELECT db_version FROM crsql_db_versions WHERE site_id = crsql_site_id()"
    ).fetchone()
    assert persisted == (db_version,)
    close(c)

    c = connect(path)
    c.isolation_level = None
    assert c.execute("SELECT crsql_db_version()").fetchone()[0] == db_version
    c.execute("INSERT INTO foo VALUES (3, 3)")
    assert c.execute(
        "SELECT db_version FROM foo__crsql_clock WHERE key = 3").fetchone()[0] > db_version
    close(c)


def test_cl_cache_after_rollback_to_local_delete(tmp_path):
    """The causal-length cache filled by local writes
    survived ROLLBACK TO,
    so a later merge compared against a stale cl and was dropped."""
    src, _ = fresh(tmp_path, "src2.db")
    dst, _ = fresh(tmp_path, "dst2.db")
    crr(src)
    crr(dst)
    src.execute("INSERT INTO foo VALUES (1, 'orig')")
    merge(dst, src.execute("SELECT * FROM crsql_changes").fetchall())
    src.execute("UPDATE foo SET b = 'remote-new' WHERE a = 1")
    update = src.execute(
        "SELECT * FROM crsql_changes WHERE db_version = 2").fetchall()
    assert len(update) == 1

    dst.execute("BEGIN")
    dst.execute("SAVEPOINT s")
    dst.execute("DELETE FROM foo WHERE a = 1")
    dst.execute("ROLLBACK TO s")
    dst.execute("RELEASE s")
    merge(dst, update)
    dst.execute("COMMIT")

    assert dst.execute("SELECT * FROM foo").fetchall() == [(1, 'remote-new')]
    close(src)
    close(dst)


def test_last_db_versions_after_rollback_to(tmp_path):
    """crsql_changes_rollback_to
    did not clear the lastDbVersions cache, so a re-merge after ROLLBACK TO
    skipped the crsql_db_versions write (db_version.rs insert_db_version)."""
    src, _ = fresh(tmp_path, "src3.db")
    dst, _ = fresh(tmp_path, "dst3.db")
    crr(src)
    crr(dst)
    src.execute("INSERT INTO foo VALUES (1, 'x')")
    changes = src.execute("SELECT * FROM crsql_changes").fetchall()
    src_site = src.execute("SELECT crsql_site_id()").fetchone()[0]

    dst.execute("BEGIN")
    dst.execute("SAVEPOINT s")
    merge(dst, changes)
    dst.execute("ROLLBACK TO s")
    dst.execute("RELEASE s")
    merge(dst, changes)
    dst.execute("COMMIT")

    assert dst.execute("SELECT * FROM foo").fetchall() == [(1, 'x')]
    assert dst.execute(
        "SELECT db_version FROM crsql_db_versions WHERE site_id = ?",
        (src_site,)).fetchall() == [(1,)]
    close(src)
    close(dst)


def test_last_db_versions_after_statement_rollback(tmp_path):
    """Statement rollback: a multi-row merge that fails midway rolls
    its earlier rows back, which must also drop their lastDbVersions and cl
    cache entries."""
    src, _ = fresh(tmp_path, "src3b.db")
    dst, _ = fresh(tmp_path, "dst3b.db")
    crr(src)
    crr(dst)
    src.execute("INSERT INTO foo VALUES (1, 'x')")
    changes = src.execute("SELECT * FROM crsql_changes").fetchall()
    src_site = src.execute("SELECT crsql_site_id()").fetchone()[0]

    dst.execute(
        "CREATE TEMP TABLE pending (ord, \"table\", pk, cid, val, col_version, db_version, site_id, cl, seq, ts)")
    for i, change in enumerate(changes):
        dst.execute("INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (i,) + tuple(change))
    bad = list(changes[0])
    bad[0] = "no_such_table"
    dst.execute("INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (len(changes),) + tuple(bad))

    dst.execute("BEGIN")
    with pytest.raises(sqlite3.Error):
        dst.execute(
            "INSERT INTO crsql_changes SELECT \"table\", pk, cid, val, col_version, db_version, site_id, cl, seq, ts FROM pending ORDER BY ord")
    assert dst.execute("SELECT count(*) FROM foo").fetchone() == (0,)
    merge(dst, changes)
    dst.execute("COMMIT")

    assert dst.execute("SELECT * FROM foo").fetchall() == [(1, 'x')]
    assert dst.execute(
        "SELECT db_version FROM crsql_db_versions WHERE site_id = ?",
        (src_site,)).fetchall() == [(1,)]
    close(src)
    close(dst)


def test_pk_change_update_cl_cache(tmp_path):
    """A PK-changing UPDATE
    resurrects the new key but left a stale cl for it in the cache, so a
    concurrent remote resurrection of that key was merged as an automatic win
    and the replicas diverged."""
    src, _ = fresh(tmp_path, "src4.db")
    dst, _ = fresh(tmp_path, "dst4.db")
    crr(src)
    crr(dst)
    src.execute("INSERT INTO foo VALUES (2, 'x')")
    src.execute("DELETE FROM foo WHERE a = 2")
    deleted = src.execute("SELECT * FROM crsql_changes").fetchall()

    dst.execute("INSERT INTO foo VALUES (1, 'local')")
    merge(dst, deleted)
    src.execute("INSERT INTO foo VALUES (2, 'remote')")

    dst.execute("BEGIN")
    # Re-merging the known delete caches cl=2 for key 2.
    merge(dst, deleted)
    dst.execute("UPDATE foo SET a = 2 WHERE a = 1")
    assert dst.execute(
        "SELECT cl FROM crsql_changes WHERE pk = crsql_pack_columns(2) AND cid = '-1'"
    ).fetchall() == [(3,)]
    merge(dst, src.execute("SELECT * FROM crsql_changes").fetchall())
    dst.execute("COMMIT")

    # Both sides are at cl=3 and the local column version (2) beats the remote
    # one (1): the same result as without the primed cache.
    assert dst.execute("SELECT * FROM foo").fetchall() == [(2, 'local')]
    merge(src, dst.execute("SELECT * FROM crsql_changes").fetchall())
    assert src.execute("SELECT * FROM foo ORDER BY a").fetchall() == \
        dst.execute("SELECT * FROM foo ORDER BY a").fetchall()
    close(src)
    close(dst)


@pytest.mark.parametrize("value", ["true", "x", 1.5, None, 2, -1])
def test_config_set_rejects_invalid_values(tmp_path, value):
    """crsql_config_set accepted any
    value, cached it before persisting, and a non-integer value bricked every
    later connection."""
    c, path = fresh(tmp_path, "cfg_invalid.db")
    with pytest.raises(sqlite3.Error):
        c.execute("SELECT crsql_config_set('merge-equal-values', ?)",
                  (value,)).fetchone()
    assert c.execute(
        "SELECT crsql_config_get('merge-equal-values')").fetchone() == (0,)
    assert c.execute(
        "SELECT count(*) FROM crsql_master WHERE key = 'config.merge-equal-values'"
    ).fetchone() == (0,)
    close(c)

    c = connect(path)
    assert c.execute(
        "SELECT crsql_config_get('merge-equal-values')").fetchone() == (0,)
    close(c)


def test_config_set_cache_follows_transaction(tmp_path):
    """The mergeEqualValues cache written by crsql_config_set
    was never reverted when the write failed
    or was rolled back."""
    c, _ = fresh(tmp_path, "cfg_tx.db")
    crr(c)

    def get():
        return c.execute(
            "SELECT crsql_config_get('merge-equal-values')").fetchone()[0]

    c.execute("PRAGMA query_only = 1")
    with pytest.raises(sqlite3.Error):
        c.execute("SELECT crsql_config_set('merge-equal-values', 1)").fetchone()
    c.execute("PRAGMA query_only = 0")
    assert get() == 0

    c.execute("BEGIN")
    c.execute("SELECT crsql_config_set('merge-equal-values', 1)").fetchone()
    assert get() == 1
    c.execute("ROLLBACK")
    assert get() == 0

    c.execute("BEGIN")
    c.execute("INSERT INTO foo VALUES (1, 1)")
    c.execute("SAVEPOINT s")
    c.execute("SELECT crsql_config_set('merge-equal-values', 1)").fetchone()
    assert get() == 1
    c.execute("ROLLBACK TO s")
    c.execute("RELEASE s")
    assert get() == 0
    c.execute("COMMIT")
    assert get() == 0

    c.execute("SELECT crsql_config_set('merge-equal-values', 1)").fetchone()
    assert get() == 1
    close(c)


def test_stored_bad_config_does_not_brick(tmp_path):
    """An invalid merge-equal-values row already stored in crsql_master
    made every connection fail with "error during initialization"
    and reload_connection_config fail."""
    c, path = fresh(tmp_path, "cfg_bad.db")
    crr(c)
    c.execute(
        "INSERT OR REPLACE INTO crsql_master VALUES ('config.merge-equal-values', 'true')")
    close(c)

    c = connect(path)
    c.isolation_level = None
    assert c.execute(
        "SELECT crsql_config_get('merge-equal-values')").fetchone() == (0,)
    c.execute("INSERT INTO foo VALUES (1, 1)")
    assert c.execute("SELECT crsql_db_version()").fetchone()[0] == 1
    c.execute("SELECT crsql_config_set('merge-equal-values', 1)").fetchone()
    assert c.execute(
        "SELECT crsql_config_get('merge-equal-values')").fetchone() == (1,)
    close(c)


def test_rows_impacted_reset_on_rollback(tmp_path):
    """rowsImpacted was only reset in crsql_changes_commit
    (core/rs/core/src/changes_vtab.rs; core/src/changes-vtab.c has no
    xRollback), so a rolled back merge was counted in the next transaction."""
    src, _ = fresh(tmp_path, "src7.db")
    dst, _ = fresh(tmp_path, "dst7.db")
    crr(src)
    crr(dst)
    src.execute("INSERT INTO foo VALUES (1, 'x')")
    changes = src.execute("SELECT * FROM crsql_changes").fetchall()
    assert len(changes) == 1

    dst.execute("BEGIN")
    merge(dst, changes)
    assert dst.execute("SELECT crsql_rows_impacted()").fetchone() == (1,)
    dst.execute("ROLLBACK")

    dst.execute("BEGIN")
    merge(dst, changes)
    assert dst.execute("SELECT crsql_rows_impacted()").fetchone() == (1,)
    dst.execute("COMMIT")
    close(src)
    close(dst)
