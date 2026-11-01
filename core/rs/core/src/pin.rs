//! Keeps the loadable extension mapped after a failed init.
//!
//! When init fails SQLite unloads the extension. Init drops everything it
//! registered first, but SQLite refuses to drop a function while a statement
//! is running (e.g. the extension was loaded with `SELECT load_extension()`).
//! Whatever could not be dropped still points into our code, so in that case
//! the shared object is pinned: opened once more with RTLD_NODELETE, which
//! makes the dynamic loader ignore every later dlclose of it.
//!
//! Static builds are never unloaded and other platforms are left alone, so
//! this is a no-op there.

#[cfg(all(
    feature = "loadable_extension",
    any(target_os = "linux", target_os = "android", target_vendor = "apple")
))]
mod imp {
    use core::ffi::{c_char, c_int, c_void};

    #[repr(C)]
    struct DlInfo {
        dli_fname: *const c_char,
        dli_fbase: *mut c_void,
        dli_sname: *const c_char,
        dli_saddr: *mut c_void,
    }

    extern "C" {
        fn dladdr(addr: *const c_void, info: *mut DlInfo) -> c_int;
        fn dlopen(filename: *const c_char, flags: c_int) -> *mut c_void;
    }

    #[cfg(target_vendor = "apple")]
    const RTLD_NOW: c_int = 0x2;
    #[cfg(target_vendor = "apple")]
    const RTLD_NOLOAD: c_int = 0x10;
    #[cfg(target_vendor = "apple")]
    const RTLD_NODELETE: c_int = 0x80;

    #[cfg(any(target_os = "linux", all(target_os = "android", target_pointer_width = "64")))]
    const RTLD_NOW: c_int = 0x2;
    #[cfg(all(target_os = "android", target_pointer_width = "32"))]
    const RTLD_NOW: c_int = 0x0;
    #[cfg(any(target_os = "linux", target_os = "android"))]
    const RTLD_NOLOAD: c_int = 0x4;
    #[cfg(any(target_os = "linux", target_os = "android"))]
    const RTLD_NODELETE: c_int = 0x1000;

    pub fn pin_extension() {
        let mut info = DlInfo {
            dli_fname: core::ptr::null(),
            dli_fbase: core::ptr::null_mut(),
            dli_sname: core::ptr::null(),
            dli_saddr: core::ptr::null_mut(),
        };
        unsafe {
            if dladdr(pin_extension as *const c_void, &mut info) == 0 || info.dli_fname.is_null() {
                return;
            }
            // RTLD_NOLOAD: only take another reference to the copy that is
            // already loaded. The handle is leaked on purpose.
            let _ = dlopen(info.dli_fname, RTLD_NOW | RTLD_NOLOAD | RTLD_NODELETE);
        }
    }
}

#[cfg(not(all(
    feature = "loadable_extension",
    any(target_os = "linux", target_os = "android", target_vendor = "apple")
)))]
mod imp {
    pub fn pin_extension() {}
}

/// Makes sure the shared object this code lives in is never unloaded.
pub fn pin_extension() {
    imp::pin_extension()
}
