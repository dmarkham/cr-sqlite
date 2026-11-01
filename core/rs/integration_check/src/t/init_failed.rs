extern crate alloc;
use alloc::{format, string::String, vec::Vec};
use sqlite::Connection;
use sqlite_nostd as sqlite;

const INIT_FAILED: &str = "crsqlite failed to initialize on this connection";

// Runs `sql` and expects it to fail with INIT_FAILED.
fn expect_init_failed(db: *mut sqlite::sqlite3, sql: &str) -> Result<(), String> {
    match db.exec_safe(sql) {
        Ok(_) => Err(format!("`{sql}` succeeded on a failed connection")),
        Err(_) => {
            let msg = db.errmsg().unwrap_or_default();
            if msg == INIT_FAILED {
                Ok(())
            } else {
                Err(format!("`{sql}` failed with `{msg}`, expected `{INIT_FAILED}`"))
            }
        }
    }
}

fn count(db: *mut sqlite::sqlite3, sql: &str) -> Result<i32, String> {
    let stmt = db.prepare_v2(sql).map_err(|e| format!("prepare `{sql}`: {e:?}"))?;
    stmt.step().map_err(|e| format!("step `{sql}`: {e:?}"))?;
    Ok(stmt.column_int(0))
}

// A failed init can leave functions registered that SQLite refused to drop
// (see crsql_unregister). Everything still callable on such a connection must
// refuse to run, without changing the schema, and the connection must still
// close.
fn test_functions_refuse_after_init_failed() -> Result<(), String> {
    let conn = crate::opendb().map_err(|e| format!("open: {e:?}"))?;
    let db = conn.db.db;
    // Close by hand to check the result.
    core::mem::forget(conn);
    let res = refuse_after_init_failed(db);
    let finalized = db.exec_safe("SELECT crsql_finalize();");
    let rc = sqlite::close(db);
    res?;
    finalized.map_err(|e| format!("crsql_finalize: {e:?}"))?;
    if rc != 0 {
        return Err(format!("sqlite3_close returned {rc}"));
    }
    Ok(())
}

fn refuse_after_init_failed(db: *mut sqlite::sqlite3) -> Result<(), String> {
    let setup = "CREATE TABLE items (a PRIMARY KEY NOT NULL, b);
        SELECT crsql_as_crr('items');
        INSERT INTO items VALUES (1, 2);
        CREATE TABLE plain (a PRIMARY KEY NOT NULL, b);";
    db.exec_safe(setup).map_err(|e| format!("setup: {e:?}"))?;
    let triggers_sql = "SELECT count(*) FROM sqlite_master WHERE type = 'trigger'";
    let tables_sql = "SELECT count(*) FROM sqlite_master WHERE type = 'table'";
    let triggers = count(db, triggers_sql)?;
    let tables = count(db, tables_sql)?;

    db.exec_safe("SELECT crsql_test_mark_init_failed();")
        .map_err(|e| format!("crsql_test_mark_init_failed: {e:?}"))?;

    let mut failures: Vec<String> = Vec::new();
    // Every function registered with ext_data as its user data.
    for sql in [
        "SELECT crsql_db_version();",
        "SELECT crsql_update_site_id(x'00', 1);",
        "SELECT crsql_site_id();",
        "SELECT crsql_next_db_version();",
        "SELECT crsql_peek_next_db_version();",
        "SELECT crsql_increment_and_get_seq();",
        "SELECT crsql_get_seq();",
        "SELECT crsql_set_ts('1');",
        "SELECT crsql_cache_site_ordinal(x'00');",
        "SELECT crsql_cache_pk_cl('items', 1);",
        "SELECT crsql_cache_db_version(x'00');",
        "SELECT crsql_set_db_version(1);",
        "SELECT crsql_get_ts();",
        "SELECT crsql_commit_alter('items');",
        "SELECT crsql_after_update('items');",
        "SELECT crsql_after_insert('items');",
        "SELECT crsql_after_delete('items');",
        "SELECT crsql_rows_impacted();",
        "SELECT crsql_config_set('merge-equal-values', 1);",
        "SELECT crsql_config_get('merge-equal-values');",
        // These change the schema.
        "SELECT crsql_automigrate('CREATE TABLE other (a PRIMARY KEY NOT NULL);', 'SELECT crsql_finalize();');",
        "SELECT crsql_as_table('items');",
        "SELECT crsql_as_crr('plain');",
        "SELECT crsql_begin_alter('items');",
        "CREATE VIRTUAL TABLE foo_schema USING clset (a PRIMARY KEY NOT NULL, b);",
        // Writes to a crr run its triggers.
        "INSERT INTO items VALUES (2, 3);",
        "UPDATE items SET b = 4 WHERE a = 1;",
        "DELETE FROM items WHERE a = 1;",
    ] {
        if let Err(e) = expect_init_failed(db, sql) {
            failures.push(e);
        }
    }
    if !failures.is_empty() {
        return Err(failures.join("\n"));
    }

    // Nothing above changed the schema.
    assert_eq!(count(db, triggers_sql)?, triggers);
    assert_eq!(count(db, tables_sql)?, tables);
    Ok(())
}

pub fn run_suite() -> Result<(), String> {
    test_functions_refuse_after_init_failed()
}
