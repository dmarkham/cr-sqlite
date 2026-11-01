use alloc::format;

use sqlite::{Connection, Context};
use sqlite_nostd as sqlite;
use sqlite_nostd::{ColumnType, ManagedStmt, ResultCode, Value};

use crate::c::{crsql_ExtData, crsql_enlist_version_guard};

pub const MERGE_EQUAL_VALUES: &str = "merge-equal-values";

pub fn reload_connection_config(
    db: *mut sqlite::sqlite3,
    ext_data: *mut crsql_ExtData,
) -> Result<(), alloc::string::String> {
    let stmt = db
        .prepare_v2("SELECT value FROM crsql_master WHERE key = 'config.merge-equal-values'")
        .map_err(|rc| format!("failed to prepare config refresh: {rc}"))?;
    let value = match stmt.step() {
        // crsql_config_set only stores 0 or 1; anything else was written by
        // hand or by an older version and falls back to the default instead
        // of failing every refresh on this connection.
        Ok(ResultCode::ROW) => {
            (stmt.column_type(0) == Ok(ColumnType::Integer) && stmt.column_int(0) == 1) as i32
        }
        Ok(ResultCode::DONE) => 0,
        Ok(rc) | Err(rc) => return Err(format!("failed to read config: {rc}")),
    };
    unsafe { (*ext_data).mergeEqualValues = value };
    Ok(())
}

pub extern "C" fn crsql_config_set(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    let args = sqlite::args!(argc, argv);

    let name = args[0].text();

    let value = match name {
        MERGE_EQUAL_VALUES => {
            let value = args[1];
            if value.value_type() != ColumnType::Integer || !matches!(value.int64(), 0 | 1) {
                ctx.result_error("merge-equal-values must be 0 or 1");
                ctx.result_error_code(ResultCode::ERROR);
                return;
            }
            value
        }
        _ => {
            ctx.result_error("Unknown setting name");
            ctx.result_error_code(ResultCode::ERROR);
            return;
        }
    };

    let db = ctx.db_handle();
    let ext_data = ctx.user_data() as *mut crsql_ExtData;
    match insert_config_setting(db, name, value) {
        Ok((_stmt, value)) => {
            // Enlist the version guard so a ROLLBACK TO that undoes this write
            // makes the connection reload its config (the rollback hook covers
            // a full ROLLBACK). Only update the cache once that is in place.
            let rc = unsafe { crsql_enlist_version_guard(db, ext_data) };
            if rc != ResultCode::OK as i32 {
                // The row may be in the database regardless; reload from it.
                unsafe { (*ext_data).pragmaDataVersion = -1 };
                ctx.result_error("Could not persist config in database");
                ctx.result_error_code(ResultCode::ERROR);
                return;
            }
            unsafe { (*ext_data).mergeEqualValues = value.int() };
            ctx.result_value(value);
        }
        Err(rc) => {
            ctx.result_error("Could not persist config in database");
            ctx.result_error_code(rc);
            return;
        }
    }
}

fn insert_config_setting(
    db: *mut sqlite_nostd::sqlite3,
    name: &str,
    value: *mut sqlite::value,
) -> Result<(ManagedStmt, *mut sqlite::value), ResultCode> {
    let stmt =
        db.prepare_v2("INSERT OR REPLACE INTO crsql_master VALUES (?, ?) RETURNING value")?;

    stmt.bind_text(1, &format!("config.{name}"), sqlite::Destructor::TRANSIENT)?;
    stmt.bind_value(2, value)?;

    if let ResultCode::ROW = stmt.step()? {
        let res = stmt.column_value(0)?;
        // Res will get invalidated when stmt gets dropped
        // The lifetime of res is not currently checked by the compiler
        Ok((stmt, res))
    } else {
        Err(ResultCode::ERROR)
    }
}

pub extern "C" fn crsql_config_get(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    let args = sqlite::args!(argc, argv);

    let name = args[0].text();
    let ext_data = ctx.user_data() as *mut crsql_ExtData;
    if let Err(message) = crate::ext_data::refresh_connection_state_if_needed(ctx.db_handle(), ext_data) {
        ctx.result_error(&message);
        return;
    }

    match name {
        MERGE_EQUAL_VALUES => {
            ctx.result_int(unsafe { (*ext_data).mergeEqualValues });
        }
        _ => {
            ctx.result_error("Unknown setting name");
            ctx.result_error_code(ResultCode::ERROR);
            return;
        }
    }
}
