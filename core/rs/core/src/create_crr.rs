use alloc::format;
use alloc::string::String;
use core::ffi::c_char;
use sqlite_nostd as sqlite;
use sqlite_nostd::{ResultCode, StrRef};

use crate::bootstrap::create_clock_table;
use crate::tableinfo::{is_table_compatible, pull_table_info};
use crate::triggers::create_triggers;
use crate::{backfill_table, is_crr, remove_crr_triggers_if_exist};

/**
 * CRRs are only supported in the main database: the clock tables, triggers and table info
 * lookups are all unqualified, so they would act on `main` rather than on another schema.
 * Returns the error message to report for any other schema.
 */
pub fn check_schema_is_main(schema: &str) -> Result<(), String> {
    // callers may pass a nul terminated "main\0"
    let schema = schema.trim_end_matches('\0');
    if schema.eq_ignore_ascii_case("main") {
        Ok(())
    } else {
        Err(format!(
            "crsql: schema '{schema}' is not supported, CRRs can only be used in the main database"
        ))
    }
}

/**
 * Create a new crr --
 * all triggers, views, tables
 */
pub fn create_crr(
    db: *mut sqlite::sqlite3,
    schema: &str,
    table: &str,
    is_commit_alter: bool,
    no_tx: bool,
    err: *mut *mut c_char,
) -> Result<ResultCode, ResultCode> {
    if let Err(msg) = check_schema_is_main(schema) {
        err.set(&msg);
        return Err(ResultCode::ERROR);
    }
    if !is_table_compatible(db, table, err)? {
        return Err(ResultCode::ERROR);
    }
    if is_crr(db, table)? {
        return Ok(ResultCode::OK);
    }

    // We do not / can not pull this from the cached set of table infos
    // since nothing would exist in it for a table not yet made into a crr.
    // TODO: Note: we can optimize out our `ensureTableInfosAreUpToDate` by mutating our ext data
    // when upgrading stuff to CRRs
    let table_info = pull_table_info(db, table, err)?;

    create_clock_table(db, &table_info, err)?;
    remove_crr_triggers_if_exist(db, table)?;
    create_triggers(db, &table_info, err)?;

    backfill_table(
        db,
        table,
        &table_info.pks,
        &table_info.non_pks,
        is_commit_alter,
        no_tx,
    )?;

    Ok(ResultCode::OK)
}
