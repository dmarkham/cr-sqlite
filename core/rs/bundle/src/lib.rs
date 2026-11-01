#![no_std]
#![feature(core_intrinsics)]
#![feature(lang_items)]

extern crate alloc;

use core::ffi::c_char;
use core::panic::PanicInfo;
use crsql_core;
use crsql_core::sqlite3_crsqlcore_init;
#[cfg(feature = "test")]
pub use crsql_core::test_exports;
use crsql_fractindex_core::{crsql_fractindex_unregister, sqlite3_crsqlfractionalindex_init};
#[cfg(feature = "test")]
use libc_print::std_name::println;
use sqlite_nostd as sqlite;
use sqlite_nostd::SQLite3Allocator;

// This must be our allocator so we can transfer ownership of memory to SQLite and have SQLite free that memory for us.
// This drastically reduces copies when passing strings and blobs back and forth between Rust and C.
#[global_allocator]
static ALLOCATOR: SQLite3Allocator = SQLite3Allocator {};

// This must be our panic handler for WASM builds. For simplicity, we make it our panic handler for
// all builds. Abort is also more portable than unwind, enabling us to go to more embedded use cases.
#[cfg(not(feature = "test"))]
#[panic_handler]
fn panic(_info: &PanicInfo) -> ! {
    core::intrinsics::abort()
}

// Print panic info for tests
#[cfg(feature = "test")]
#[panic_handler]
fn panic(info: &PanicInfo) -> ! {
    println!("PANIC!: {}", info);
    core::intrinsics::abort();
}

// A Rust program that links the static library has its own.
#[cfg(not(any(target_family = "wasm", feature = "omit_eh_personality")))]
#[lang = "eh_personality"]
extern "C" fn eh_personality() {}

#[no_mangle]
pub extern "C" fn sqlite3_crsqlrustbundle_init(
    db: *mut sqlite::sqlite3,
    err_msg: *mut *mut c_char,
    api: *mut sqlite::api_routines,
) -> *mut ::core::ffi::c_void {
    sqlite::EXTENSION_INIT2(api);

    // Core first: it does the checks that can fail before it registers
    // anything (see sqlite3_crsqlcore_init).
    let ext_data = sqlite3_crsqlcore_init(db, err_msg, api);
    if ext_data.is_null() {
        return core::ptr::null_mut();
    }

    let rc = sqlite3_crsqlfractionalindex_init(db, err_msg, api);
    if rc != 0 {
        crsql_rustbundle_unregister(db, ext_data);
        return core::ptr::null_mut();
    }
    ext_data
}

/// Undoes a successful sqlite3_crsqlrustbundle_init when the rest of the
/// extension init fails: drops every function and module it registered and,
/// through crsql_db_version's destructor, frees the ext data. The C caller
/// drops its own modules (which point at the ext data) first.
#[no_mangle]
pub extern "C" fn crsql_rustbundle_unregister(
    db: *mut sqlite::sqlite3,
    ext_data: *mut ::core::ffi::c_void,
) {
    crsql_core::crsql_mark_init_failed(ext_data);
    if !crsql_fractindex_unregister(db) {
        // Left registered, so the shared object must stay loaded.
        crsql_core::pin_extension();
    }
    crsql_core::crsql_unregister(db, true);
}
