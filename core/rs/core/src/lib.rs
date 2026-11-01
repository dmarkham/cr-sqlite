#![cfg_attr(not(test), no_std)]
#![feature(vec_into_raw_parts)]

// TODO: these pub mods are exposed for the integration testing
// we should re-export in a `test` mod such that they do not become public apis
mod alter;
mod automigrate;
mod backfill;
#[cfg(feature = "test")]
pub mod bootstrap;
#[cfg(not(feature = "test"))]
mod bootstrap;
#[cfg(feature = "test")]
pub mod c;
#[cfg(not(feature = "test"))]
mod c;
mod changes_vtab;
mod changes_vtab_read;
mod changes_vtab_write;
mod commit;
mod compare_values;
mod config;
mod consts;
mod create_cl_set_vtab;
mod create_crr;
#[cfg(feature = "test")]
pub mod db_version;
#[cfg(not(feature = "test"))]
mod db_version;
mod debug;
mod ext_data;
mod is_crr;
mod local_writes;
#[cfg(feature = "test")]
pub mod pack_columns;
#[cfg(not(feature = "test"))]
mod pack_columns;
mod pin;
mod sha;
mod stmt_cache;
#[cfg(feature = "test")]
pub mod tableinfo;
#[cfg(not(feature = "test"))]
mod tableinfo;
mod teardown;
#[cfg(feature = "test")]
pub mod test_exports;
mod triggers;
mod unpack_columns_vtab;
mod util;

use alloc::ffi::CString;
use alloc::format;
use alloc::string::ToString;
use alloc::{boxed::Box, collections::BTreeMap, vec::Vec};
use core::ffi::c_char;
use core::mem;
use core::ptr::null_mut;
extern crate alloc;
use alter::crsql_compact_post_alter;
use automigrate::*;
use backfill::*;
use c::{crsql_freeExtData, crsql_initSiteIdExt, crsql_newExtData};
use config::{crsql_config_get, crsql_config_set};
use ext_data::reject_if_init_failed;
use core::ffi::{c_int, c_void, CStr};
use create_crr::{check_schema_is_main, create_crr};
use db_version::{crsql_next_db_version, crsql_peek_next_db_version, insert_db_version};
use is_crr::*;
use local_writes::after_delete::x_crsql_after_delete;
use local_writes::after_insert::x_crsql_after_insert;
use local_writes::after_update::x_crsql_after_update;
use sqlite::{Destructor, ResultCode};
use sqlite_nostd as sqlite;
use sqlite_nostd::{Connection, Context, Value};
#[cfg(feature = "test")]
use tableinfo::TableInfo;
use tableinfo::{crsql_ensure_table_infos_are_up_to_date, is_table_compatible, pull_table_info};
use teardown::*;
use triggers::create_triggers;

pub use debug::debug_log;
pub use pin::pin_extension;

#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn crsql_as_table(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    let args = sqlite::args!(argc, argv);
    let db = ctx.db_handle();
    let table = args[0].text();

    if let Err(_) = db.exec_safe("SAVEPOINT as_table;") {
        ctx.result_error("failed to start as_table savepoint");
        return;
    }

    if let Err(_) = crsql_as_table_impl(db, table) {
        ctx.result_error("failed to downgrade the crr");
        let _ = db.exec_safe("ROLLBACK TO as_table; RELEASE as_table;");
        return;
    }

    if let Err(_) = db.exec_safe("RELEASE as_table;") {
        // fine
    }
}

fn crsql_as_table_impl(db: *mut sqlite::sqlite3, table: &str) -> Result<ResultCode, ResultCode> {
    remove_crr_clock_table_if_exists(db, table)?;
    remove_crr_triggers_if_exist(db, table)
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn sqlite3_crsqlcore_init(
    db: *mut sqlite::sqlite3,
    err_msg: *mut *mut c_char,
    api: *mut sqlite::api_routines,
) -> *mut c_void {
    sqlite::EXTENSION_INIT2(api);

    // Everything that can fail for a reason other than OOM (an unsupported
    // db version, a read-only or corrupt db, ...) runs before anything is
    // registered: SQLite cannot always drop what a failed init registered
    // (see crsql_unregister).
    let rc = crate::bootstrap::crsql_init_peer_tracking_table(db);
    if rc != ResultCode::OK as c_int {
        return null_mut();
    }

    let rc = crate::bootstrap::crsql_init_db_versions_table(db);
    if rc != ResultCode::OK as c_int {
        return null_mut();
    }

    let rc = crate::bootstrap::crsql_maybe_update_db(db, err_msg);
    if rc != ResultCode::OK as c_int {
        return null_mut();
    }

    let mut site_id = [0u8; consts::SITE_ID_LEN as usize];
    let rc = crate::bootstrap::crsql_init_site_id(db, site_id.as_mut_ptr());
    if rc != ResultCode::OK as c_int {
        return null_mut();
    }

    if let Err(_) = crate::bootstrap::create_site_id_triggers(db) {
        return null_mut();
    }

    let rc = db
        .create_function_v2(
            "crsql_set_debug",
            1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            None,
            Some(debug::x_crsql_set_debug),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    let rc = db
        .create_function_v2(
            "crsql_automigrate",
            -1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            None,
            Some(crsql_automigrate),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    let rc = db
        .create_function_v2(
            "crsql_pack_columns",
            -1,
            sqlite::UTF8,
            None,
            Some(pack_columns::crsql_pack_columns),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    let rc = db
        .create_function_v2(
            "crsql_as_table",
            1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            None,
            Some(crsql_as_table),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    let rc = unpack_columns_vtab::create_module(db).unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    let rc = create_cl_set_vtab::create_module(db).unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    let sync_bit_ptr = sqlite::malloc(mem::size_of::<c_int>()) as *mut c_int;
    if sync_bit_ptr.is_null() {
        return init_failed(db, false);
    }
    unsafe {
        *sync_bit_ptr = 0;
    }
    // Function to allow us to disable triggers when syncing remote changes
    // to base tables. Setting the bit, crsql_internal_sync_bit(x), must not
    // be reachable from schema (views, triggers); reading it,
    // crsql_internal_sync_bit(), is what the crr triggers do. The setter owns
    // sync_bit_ptr.
    let rc = db
        .create_function_v2(
            "crsql_internal_sync_bit",
            1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            Some(sync_bit_ptr as *mut c_void),
            Some(x_crsql_sync_bit),
            None,
            None,
            Some(crsql_sqlite_free),
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }
    let rc = db
        .create_function_v2(
            "crsql_internal_sync_bit",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(sync_bit_ptr as *mut c_void),
            Some(x_crsql_sync_bit),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed(db, false);
    }

    // allocate ext data earlier in the init process because we need its
    // pointer to be available for the crsql_update_site_id function.
    let ext_data = unsafe { crsql_newExtData(db) };
    if ext_data.is_null() {
        return init_failed(db, false);
    }

    // crsql_db_version owns ext_data: SQLite calls x_free_connection_ext_data
    // when the function is dropped (connection close, re-registration, or
    // a failed registration). Register it first so ext_data is never freed
    // by hand; every later failure goes through init_failed.
    let rc = db
        .create_function_v2(
            "crsql_db_version",
            0,
            sqlite::INNOCUOUS | sqlite::UTF8,
            Some(ext_data as *mut c_void),
            Some(x_crsql_db_version),
            None,
            None,
            Some(x_free_connection_ext_data),
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        // SQLite already ran the destructor.
        return init_failed(db, false);
    }

    let rc = db
        .create_function_v2(
            "crsql_update_site_id",
            2,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_update_site_id),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let site_id_buffer = sqlite::malloc(consts::SITE_ID_LEN as usize);
    if site_id_buffer.is_null() {
        return init_failed_with_ext_data(db, ext_data);
    }
    unsafe {
        core::ptr::copy_nonoverlapping(site_id.as_ptr(), site_id_buffer, site_id.len());
    }

    // From here on ext_data owns site_id_buffer and frees it with itself.
    let rc = unsafe { crsql_initSiteIdExt(db, ext_data, site_id_buffer as *mut c_char) };
    if rc != ResultCode::OK as c_int {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_site_id",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS | sqlite::DETERMINISTIC,
            Some(ext_data as *mut c_void),
            Some(x_crsql_site_id),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_next_db_version",
            -1,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_next_db_version),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_peek_next_db_version",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_peek_next_db_version),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_sha",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS | sqlite::DETERMINISTIC,
            None,
            Some(x_crsql_sha),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_version",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS | sqlite::DETERMINISTIC,
            None,
            Some(x_crsql_version),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_increment_and_get_seq",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_increment_and_get_seq),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_get_seq",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_get_seq),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_as_crr",
            -1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            None,
            Some(x_crsql_as_crr),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_set_ts",
            1,
            sqlite::UTF8,
            Some(ext_data as *mut c_void),
            Some(x_crsql_set_ts),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    #[cfg(feature = "test")]
    if let Err(_) = db.create_function_v2(
        "crsql_cache_site_ordinal",
        1,
        sqlite::UTF8,
        Some(ext_data as *mut c_void),
        Some(x_crsql_cache_site_ordinal),
        None,
        None,
        None,
    ) {
        return init_failed_with_ext_data(db, ext_data);
    }

    #[cfg(feature = "test")]
    if let Err(_) = db.create_function_v2(
        "crsql_cache_pk_cl",
        2,
        sqlite::UTF8,
        Some(ext_data as *mut c_void),
        Some(x_crsql_cache_pk_cl),
        None,
        None,
        None,
    ) {
        return init_failed_with_ext_data(db, ext_data);
    }

    #[cfg(feature = "test")]
    if let Err(_) = db.create_function_v2(
        "crsql_cache_db_version",
        1,
        sqlite::UTF8,
        Some(ext_data as *mut c_void),
        Some(x_crsql_cache_db_version),
        None,
        None,
        None,
    ) {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_set_db_version",
            -1,
            sqlite::UTF8,
            Some(ext_data as *mut c_void),
            Some(x_crsql_set_db_version),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_get_ts",
            -1,
            sqlite::UTF8,
            Some(ext_data as *mut c_void),
            Some(x_crsql_get_ts),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_begin_alter",
            -1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            None,
            Some(x_crsql_begin_alter),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_commit_alter",
            -1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            Some(ext_data as *mut c_void),
            Some(x_crsql_commit_alter),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_finalize",
            -1,
            sqlite::UTF8 | sqlite::DIRECTONLY,
            Some(ext_data as *mut c_void),
            Some(x_crsql_finalize),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_after_update",
            -1,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_after_update),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_after_insert",
            -1,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_after_insert),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_after_delete",
            -1,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_after_delete),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_rows_impacted",
            0,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(x_crsql_rows_impacted),
            None,
            None,
            None,
        )
        .unwrap_or(ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_config_set",
            2,
            sqlite::UTF8,
            Some(ext_data as *mut c_void),
            Some(crsql_config_set),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    let rc = db
        .create_function_v2(
            "crsql_config_get",
            1,
            sqlite::UTF8 | sqlite::INNOCUOUS,
            Some(ext_data as *mut c_void),
            Some(crsql_config_get),
            None,
            None,
            None,
        )
        .unwrap_or(sqlite::ResultCode::ERROR);
    if rc != ResultCode::OK {
        return init_failed_with_ext_data(db, ext_data);
    }

    return ext_data as *mut c_void;
}

/// Every function registered with ext_data as its user data, other than
/// crsql_db_version, as (name, n_arg).
const EXT_DATA_FUNCTIONS: &[(&str, c_int)] = &[
    ("crsql_update_site_id", 2),
    ("crsql_site_id", 0),
    ("crsql_next_db_version", -1),
    ("crsql_peek_next_db_version", 0),
    ("crsql_increment_and_get_seq", 0),
    ("crsql_get_seq", 0),
    ("crsql_set_ts", 1),
    ("crsql_cache_site_ordinal", 1),
    ("crsql_cache_pk_cl", 2),
    ("crsql_cache_db_version", 1),
    ("crsql_set_db_version", -1),
    ("crsql_get_ts", -1),
    ("crsql_commit_alter", -1),
    ("crsql_finalize", -1),
    ("crsql_after_update", -1),
    ("crsql_after_insert", -1),
    ("crsql_after_delete", -1),
    ("crsql_rows_impacted", 0),
    ("crsql_config_set", 2),
    ("crsql_config_get", 1),
];

/// Functions registered by sqlite3_crsqlcore_init without ext_data, as
/// (name, n_arg).
const CORE_FUNCTIONS: &[(&str, c_int)] = &[
    ("crsql_set_debug", 1),
    ("crsql_automigrate", -1),
    ("crsql_pack_columns", -1),
    ("crsql_as_table", 1),
    ("crsql_internal_sync_bit", 0),
    ("crsql_internal_sync_bit", 1),
    ("crsql_sha", 0),
    ("crsql_version", 0),
    ("crsql_as_crr", -1),
    ("crsql_begin_alter", -1),
];

const CORE_MODULES: &[&str] = &["crsql_unpack_columns", "clset"];

fn init_failed(db: *mut sqlite::sqlite3, owns_ext_data: bool) -> *mut c_void {
    crsql_unregister(db, owns_ext_data);
    null_mut()
}

/// init_failed once crsql_db_version owns ext_data.
fn init_failed_with_ext_data(
    db: *mut sqlite::sqlite3,
    ext_data: *mut c::crsql_ExtData,
) -> *mut c_void {
    crsql_mark_init_failed(ext_data as *mut c_void);
    init_failed(db, true)
}

/// Marks ext_data as belonging to a failed init. Whatever crsql_unregister
/// cannot drop keeps it as user data and then refuses to run
/// (see reject_if_init_failed).
pub fn crsql_mark_init_failed(ext_data: *mut c_void) {
    unsafe {
        (*(ext_data as *mut c::crsql_ExtData)).initFailed = 1;
    }
}

/// Drops every function and module sqlite3_crsqlcore_init registers. When
/// init fails SQLite unloads the extension while the connection stays
/// usable, so nothing may be left pointing at our code or at ext_data.
///
/// `owns_ext_data` says crsql_db_version was registered by this init: it is
/// dropped last and its destructor frees ext_data. SQLite refuses to drop a
/// function while statements are running (e.g. the extension is being loaded
/// with `SELECT load_extension()`); if any drop fails, crsql_db_version is
/// kept so ext_data stays alive for whatever still holds it, and is freed
/// when the connection closes. What is left points into our code, so the
/// shared object is pinned to stop SQLite from unloading it.
pub fn crsql_unregister(db: *mut sqlite::sqlite3, owns_ext_data: bool) {
    let drop_function = |name: &str, n_arg: c_int| {
        db.create_function_v2(name, n_arg, sqlite::UTF8, None, None, None, None, None)
            .is_ok()
    };
    let mut all_dropped = true;
    for (name, n_arg) in CORE_FUNCTIONS.iter().chain(EXT_DATA_FUNCTIONS) {
        all_dropped &= drop_function(name, *n_arg);
    }
    for name in CORE_MODULES {
        let _ = db.create_module_v2(name, core::ptr::null(), None, None);
    }
    if !all_dropped {
        pin_extension();
    } else if owns_ext_data {
        drop_function("crsql_db_version", 0);
    }
}

/**
 * return the uuid which uniquely identifies this database.
 *
 * `select crsql_site_id()`
 */
unsafe extern "C" fn x_crsql_site_id(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let site_id = (*ext_data).siteId;
    sqlite::result_blob(ctx, site_id, consts::SITE_ID_LEN, Destructor::STATIC);
}

/**
 * update in-memory map of site ids to ordinals. Only valid within a transaction.
 *
 * `select crsql_update_site_id(site_id, ordinal)`
 */
unsafe extern "C" fn x_crsql_update_site_id(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let args = sqlite::args!(argc, argv);
    let site_id = args[0].blob();
    let ordinal = args[1].int64();
    let mut ordinals: mem::ManuallyDrop<Box<BTreeMap<Vec<u8>, i64>>> = mem::ManuallyDrop::new(
        Box::from_raw((*ext_data).ordinalMap as *mut BTreeMap<Vec<u8>, i64>),
    );

    if ordinal == -1 {
        ordinals.remove(&site_id.to_vec());
    } else {
        ordinals.insert(site_id.to_vec(), ordinal);
    }
    ctx.result_text_static("OK");
}

unsafe extern "C" fn x_crsql_finalize(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    c::crsql_finalize(ext_data);
    ctx.result_text_static("finalized");
}

/// Reads the `[schema_name,] table_name` arguments of crsql_as_crr,
/// crsql_begin_alter and crsql_commit_alter. NULL or empty names are
/// rejected: they can never name a table.
fn schema_and_table_args(
    args: &[*mut sqlite::value],
    has_schema: bool,
) -> Result<(&str, &str), &'static str> {
    let is_valid = |v: &*mut sqlite::value| v.value_type() != sqlite::ColumnType::Null && v.bytes() > 0;
    if has_schema {
        if !is_valid(&args[0]) || !is_valid(&args[1]) {
            return Err("The schema name and table name must be non-empty text");
        }
        Ok((args[0].text(), args[1].text()))
    } else {
        if !is_valid(&args[0]) {
            return Err("The table name must be non-empty text");
        }
        Ok(("main", args[0].text()))
    }
}

/**
 * Takes a table name and turns it into a CRR.
 *
 * This allows users to create and modify tables as normal.
 */
unsafe extern "C" fn x_crsql_as_crr(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if argc == 0 {
        ctx.result_error(
            "Wrong number of args provided to crsql_as_crr. Provide the schema
          name and table name or just the table name.",
        );
        return;
    }

    let args = sqlite::args!(argc, argv);
    let (schema_name, table_name) = match schema_and_table_args(args, argc == 2) {
        Ok(names) => names,
        Err(msg) => {
            ctx.result_error(msg);
            return;
        }
    };

    let db = ctx.db_handle();
    let mut err_msg = null_mut();
    let rc = db.exec_safe("SAVEPOINT as_crr");
    if rc.is_err() {
        ctx.result_error("failed to start as_crr savepoint");
        return;
    }

    let rc = create_crr(db, schema_name, table_name, false, false, &mut err_msg as *mut _)
        .unwrap_or_else(|rc| rc) as c_int;
    if rc != ResultCode::OK as c_int {
        sqlite::result_error(ctx, err_msg, -1);
        sqlite::result_error_code(ctx, rc);
        // result_error copied the message.
        sqlite::free(err_msg as *mut c_void);
        let _ = db.exec_safe("ROLLBACK TO as_crr; RELEASE as_crr;");
        return;
    }

    let rc = db.exec_safe("RELEASE as_crr");
    if rc.is_err() {
        ctx.result_error("failed to release as_crr savepoint");
        return;
    }
    ctx.result_text_static("OK");
}

unsafe extern "C" fn x_crsql_rows_impacted(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let rows_impacted = (*ext_data).rowsImpacted;
    sqlite::result_int(ctx, rows_impacted);
}

unsafe extern "C" fn x_crsql_begin_alter(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if argc == 0 {
        ctx.result_error(
            "Wrong number of args provided to crsql_begin_alter. Provide the
          schema name and table name or just the table name.",
        );
        return;
    }

    let args = sqlite::args!(argc, argv);
    let (schema_name, table_name) = match schema_and_table_args(args, argc == 2) {
        Ok(names) => names,
        Err(msg) => {
            ctx.result_error(msg);
            return;
        }
    };
    if let Err(msg) = check_schema_is_main(schema_name) {
        ctx.result_error(&msg);
        return;
    }

    let db = ctx.db_handle();
    let rc = db.exec_safe("SAVEPOINT alter_crr");
    if rc.is_err() {
        ctx.result_error("failed to start alter_crr savepoint");
        return;
    }
    let rc = remove_crr_triggers_if_exist(db, table_name);
    if rc.is_err() {
        sqlite::result_error_code(ctx, rc.unwrap_err() as c_int);
        let _ = db.exec_safe("ROLLBACK TO alter_crr; RELEASE alter_crr;");
        return;
    }
    ctx.result_text_static("OK");
}

unsafe extern "C" fn x_crsql_commit_alter(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    if argc == 0 {
        ctx.result_error(
            "Wrong number of args provided to crsql_commit_alter. Provide the
          schema name and table name or just the table name.",
        );
        return;
    }

    let args = sqlite::args!(argc, argv);
    let (schema_name, table_name) = match schema_and_table_args(args, argc >= 2) {
        Ok(names) => names,
        Err(msg) => {
            ctx.result_error(msg);
            return;
        }
    };

    if let Err(msg) = check_schema_is_main(schema_name) {
        ctx.result_error(&msg);
        return;
    }

    let non_destructive = if argc >= 3 { args[2].int() == 1 } else { false };

    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let mut err_msg = null_mut();
    let db = ctx.db_handle();

    // crsql_begin_alter's alter_crr savepoint is optional, so take one of our own to undo a
    // partial commit_alter.
    if db.exec_safe("SAVEPOINT commit_alter").is_err() {
        ctx.result_error("failed to start commit_alter savepoint");
        return;
    }

    // The schema_version alone can't tell this alteration apart from an earlier one that was
    // rolled back to the same savepoint, so always re-pull the table infos.
    (*ext_data).pragmaSchemaVersionForTableInfos = -1;

    let rc = if non_destructive {
        match pull_table_info(db, table_name, &mut err_msg as *mut _) {
            Ok(table_info) => {
                match create_triggers(db, &table_info, &mut err_msg) {
                    Ok(ResultCode::OK) => {
                        // need to ensure the right table infos in ext data
                        crsql_ensure_table_infos_are_up_to_date(
                            db,
                            ext_data,
                            &mut err_msg as *mut _,
                        )
                    }
                    Ok(rc) | Err(rc) => rc as c_int,
                }
            }
            Err(rc) => rc as c_int,
        }
    } else {
        // crsql_compact_post_alter takes a C string; a &str from SQLite is
        // not guaranteed to be NUL-terminated.
        let rc = match CString::new(table_name) {
            Ok(c_table_name) => crsql_compact_post_alter(
                db,
                c_table_name.as_ptr(),
                ext_data,
                &mut err_msg as *mut _,
            ),
            Err(_) => ResultCode::MISUSE as c_int,
        };

        if rc == ResultCode::OK as c_int {
            create_crr(db, schema_name, table_name, true, false, &mut err_msg as *mut _)
                .unwrap_or_else(|rc| rc) as c_int
        } else {
            rc
        }
    };

    let rc = if rc == ResultCode::OK as c_int {
        match db.exec_safe("RELEASE commit_alter") {
            Ok(_) => release_alter_crr(db),
            Err(rc) => rc as c_int,
        }
    } else {
        rc
    };
    if rc != ResultCode::OK as c_int {
        let error_str = if !err_msg.is_null() {
            let msg = unsafe { CStr::from_ptr(err_msg).to_string_lossy().into_owned() };
            sqlite::free(err_msg as *mut c_void);
            msg
        } else {
            // nothing more specific was recorded, fall back to the connection's last error
            format!("{} (code {})", db.errmsg().unwrap_or_default(), rc)
        };
        ctx.result_error(&format!(
            "failed compacting tables post alteration: {}",
            error_str
        ));
        sqlite::result_error_code(ctx, rc);
        // undo our own work, then the alteration since the savepoint crsql_begin_alter opened
        // (if any), never the caller's transaction
        let _ = db.exec_safe("ROLLBACK TO commit_alter; RELEASE commit_alter;");
        let _ = db.exec_safe("ROLLBACK TO alter_crr; RELEASE alter_crr;");
        return;
    }
}

/// Releases the savepoint crsql_begin_alter opened. crsql_commit_alter may be
/// used without crsql_begin_alter, in which case there is none.
fn release_alter_crr(db: *mut sqlite::sqlite3) -> c_int {
    match db.exec_safe("RELEASE alter_crr") {
        Ok(_) => ResultCode::OK as c_int,
        Err(_) if db
            .errmsg()
            .map_or(false, |msg| msg.starts_with("no such savepoint")) =>
        {
            ResultCode::OK as c_int
        }
        Err(rc) => rc as c_int,
    }
}

unsafe extern "C" fn x_crsql_get_seq(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    ctx.result_int((*ext_data).seq);
}

unsafe extern "C" fn x_crsql_increment_and_get_seq(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    ctx.result_int((*ext_data).seq);
    (*ext_data).seq += 1;
}

/**
 * Set the timestamp for the current transaction.
 */
unsafe extern "C" fn x_crsql_set_ts(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    if argc == 0 {
        ctx.result_error("Wrong number of args provided to x_crsql_set_ts. Provide the timestamp.");
        return;
    }

    let args = sqlite::args!(argc, argv);
    let ts = args[0].text();
    // we expect a string that we can parse as a u64
    let ts_u64 = match ts.parse::<u64>() {
        Ok(ts_u64) => ts_u64,
        Err(_) => {
            ctx.result_error("Timestamp cannot be parsed as a valid u64");
            return;
        }
    };
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    if let Err(message) =
        ext_data::refresh_connection_state_if_needed(ctx.db_handle(), ext_data)
    {
        ctx.result_error(&message);
        return;
    }
    (*ext_data).timestamp = ts_u64;
    ctx.result_text_static("OK");
}

/**
 * Get the site ordinal cached in the ext data for the current transaction.
 * only used for test to inspect the ordinal map.
 */
#[cfg(feature = "test")]
unsafe extern "C" fn x_crsql_cache_site_ordinal(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    if argc == 0 {
        ctx.result_error(
            "Wrong number of args provided to crsql_cache_site_ordinal. Provide the site id.",
        );
        return;
    }

    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let args = sqlite::args!(argc, argv);
    let site_id = args[0].blob();

    let ord_map = mem::ManuallyDrop::new(Box::from_raw(
        (*ext_data).ordinalMap as *mut BTreeMap<Vec<u8>, i64>,
    ));
    let res = ord_map.get(site_id).cloned().unwrap_or(-1);
    sqlite::result_int64(ctx, res);
}

/**
 * Get the pk cl cached in the ext data for the current transaction.
 * only used for test to inspect the cl cache.
 */
#[cfg(feature = "test")]
unsafe extern "C" fn x_crsql_cache_pk_cl(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    if argc < 2 {
        ctx.result_error(
            "Wrong number of args provided to crsql_cache_pk_cl. Provide the table name and pk key.",
        );
        return;
    }

    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let args = sqlite::args!(argc, argv);
    let table_name = args[0].text();
    let pk_key = args[1].int64();

    let table_infos =
        mem::ManuallyDrop::new(Box::from_raw((*ext_data).tableInfos as *mut Vec<TableInfo>));
    let table_info =
        tableinfo::find_table_info_index(&table_infos, table_name).map(|i| &table_infos[i]);

    if let Some(table_info) = table_info {
        let cl = table_info.get_cl(pk_key).cloned().unwrap_or(-1);
        sqlite::result_int64(ctx, cl);
    } else {
        ctx.result_error("table not found");
    }
}

/**
 * Get the db_version cached in the ext data for the current transaction for a given site_id.
 * only used for test to inspect the lastDbVersions map.
 */
#[cfg(feature = "test")]
unsafe extern "C" fn x_crsql_cache_db_version(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    if argc == 0 {
        ctx.result_error(
            "Wrong number of args provided to crsql_cache_db_version. Provide the site id.",
        );
        return;
    }

    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let args = sqlite::args!(argc, argv);
    let site_id = args[0].blob();

    let db_versions_map = mem::ManuallyDrop::new(Box::from_raw(
        (*ext_data).lastDbVersions as *mut BTreeMap<Vec<u8>, i64>,
    ));
    let res = db_versions_map.get(site_id).cloned().unwrap_or(-1);
    sqlite::result_int64(ctx, res);
}

/**
 * Return the timestamp for the current transaction.
 */
unsafe extern "C" fn x_crsql_get_ts(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let ts = (*ext_data).timestamp.to_string();
    ctx.result_text_transient(&ts);
}

/**
 * Return the current version of the database.
 *
 * `select crsql_db_version()`
 */
unsafe extern "C" fn x_crsql_db_version(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let db = ctx.db_handle();
    if let Err(message) = ext_data::refresh_connection_state_if_needed(db, ext_data) {
        ctx.result_error(&message);
        return;
    }
    sqlite::result_int64(ctx, (*ext_data).dbVersion);
}

/**
 * Return the next version of the database for use in inserts/updates/deletes
 *
 * `select crsql_next_db_version()`
 *
 * Nit: this should be same as `crsql_db_version`
 * If you change this behavior you need to change trigger behaviors
 * as each invocation to `nextVersion` should return the same version
 * when in the same transaction.
 */
unsafe extern "C" fn x_crsql_next_db_version(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let db = ctx.db_handle();
    let mut err_msg = null_mut();

    let ret = crsql_next_db_version(db, ext_data, &mut err_msg as *mut _);
    if ret < 0 {
        // TODO: use err_msg!
        ctx.result_error("Unable to determine the next db version");
        return;
    }

    ctx.result_int64(ret);
}

/**
 * Return the next version of the database for use in inserts/updates/deletes
 *
 * `select crsql_set_db_version()`
 *
 * This is used to set the db version for a particular site_id.
 * This is useful for exposing a way for the application to set a db_version
 * that might not get passed to crsqlite.
 */
unsafe extern "C" fn x_crsql_set_db_version(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    if argc < 2 {
        ctx.result_error(
            "Wrong number of args provided to crsql_set_db_version. Provide the
        site id and db version.",
        );
        return;
    }

    let args = sqlite::args!(argc, argv);
    let (site_id, db_version) = (args[0].blob(), args[1].int64());
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;

    let ret = insert_db_version(ext_data, site_id, db_version);
    if ret.is_err() {
        ctx.result_error("Unable to set the db version");
        return;
    }
    ctx.result_text_static("OK");
}

/**
 * Return the next version of the database for use in inserts/updates/deletes
 * without updating the the database or value in ext_data.
 *
 * `select crsql_peek_next_db_version()`
 */
unsafe extern "C" fn x_crsql_peek_next_db_version(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    if reject_if_init_failed(ctx) {
        return;
    }
    let ext_data = ctx.user_data() as *mut c::crsql_ExtData;
    let db = ctx.db_handle();
    let mut err_msg = null_mut();

    let ret = crsql_peek_next_db_version(db, ext_data, &mut err_msg as *mut _);
    if ret < 0 {
        // TODO: use err_msg!
        ctx.result_error("Unable to determine the next db version");
        return;
    }

    ctx.result_int64(ret);
}

/**
 * The sha of the commit that this version of crsqlite was built from.
 */
unsafe extern "C" fn x_crsql_sha(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    ctx.result_text_static(sha::SHA);
}

unsafe extern "C" fn x_crsql_version(
    ctx: *mut sqlite::context,
    _argc: i32,
    _argv: *mut *mut sqlite::value,
) {
    ctx.result_int64(consts::CRSQLITE_VERSION as i64);
}

unsafe extern "C" fn x_free_connection_ext_data(data: *mut c_void) {
    let ext_data = data as *mut c::crsql_ExtData;
    crsql_freeExtData(ext_data);
}

pub unsafe extern "C" fn crsql_sqlite_free(ptr: *mut c_void) {
    sqlite::free(ptr);
}

unsafe extern "C" fn x_crsql_sync_bit(
    ctx: *mut sqlite::context,
    argc: i32,
    argv: *mut *mut sqlite::value,
) {
    let sync_bit_ptr = ctx.user_data() as *mut c_int;
    if argc != 1 {
        ctx.result_int(*sync_bit_ptr);
        return;
    }

    let args = sqlite::args!(argc, argv);
    let new_value = args[0].int();
    *sync_bit_ptr = new_value;

    ctx.result_int(*sync_bit_ptr);
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn crsql_is_crr(db: *mut sqlite::sqlite3, table: *const c_char) -> c_int {
    if let Ok(table) = unsafe { CStr::from_ptr(table).to_str() } {
        match is_crr(db, table) {
            Ok(b) => {
                if b {
                    1
                } else {
                    0
                }
            }
            Err(c) => (c as c_int) * -1,
        }
    } else {
        (ResultCode::NOMEM as c_int) * -1
    }
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn crsql_is_table_compatible(
    db: *mut sqlite::sqlite3,
    table: *const c_char,
    err: *mut *mut c_char,
) -> c_int {
    if let Ok(table) = unsafe { CStr::from_ptr(table).to_str() } {
        is_table_compatible(db, table, err)
            .map(|x| x as c_int)
            .unwrap_or_else(|err| (err as c_int) * -1)
    } else {
        (ResultCode::NOMEM as c_int) * -1
    }
}

#[no_mangle]
#[allow(clippy::not_unsafe_ptr_arg_deref)]
pub extern "C" fn crsql_create_crr(
    db: *mut sqlite::sqlite3,
    schema: *const c_char,
    table: *const c_char,
    is_commit_alter: c_int,
    no_tx: c_int,
    err: *mut *mut c_char,
) -> c_int {
    let schema = unsafe { CStr::from_ptr(schema).to_str() };
    let table = unsafe { CStr::from_ptr(table).to_str() };

    match (table, schema) {
        (Ok(table), Ok(schema)) => {
            create_crr(db, schema, table, is_commit_alter != 0, no_tx != 0, err)
                .unwrap_or_else(|err| err) as c_int
        }
        _ => ResultCode::NOMEM as c_int,
    }
}
