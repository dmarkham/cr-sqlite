#include "crsqlite.h"
SQLITE_EXTENSION_INIT1
#ifdef LIBSQL
LIBSQL_EXTENSION_INIT1
#endif

#include <assert.h>
#include <ctype.h>
#include <limits.h>
#include <stdint.h>
#include <string.h>

#include "changes-vtab.h"
#include "ext-data.h"
#include "version-guard.h"

int crsql_compact_post_alter(sqlite3 *db, const char *tblName,
                             crsql_ExtData *pExtData, char **errmsg);

int crsql_commit_hook(void *pUserData);
void crsql_rollback_hook(void *pUserData);

#ifdef LIBSQL
static void closeHook(void *pUserData, sqlite3 *db) {
  crsql_ExtData *pExtData = (crsql_ExtData *)pUserData;
  crsql_finalize(pExtData);
}
#endif

void *sqlite3_crsqlrustbundle_init(sqlite3 *db, char **pzErrMsg,
                                   const sqlite3_api_routines *pApi);
void crsql_rustbundle_unregister(sqlite3 *db, void *pExtData);

#ifdef _WIN32
__declspec(dllexport)
#endif
    int sqlite3_crsqlite_init(sqlite3 *db, char **pzErrMsg,
                              const sqlite3_api_routines *pApi
#ifdef LIBSQL
                              ,
                              const libsql_api_routines *pLibsqlApi
#endif
    ) {
  int rc = SQLITE_OK;

  SQLITE_EXTENSION_INIT2(pApi);
#ifdef LIBSQL
  LIBSQL_EXTENSION_INIT2(pLibsqlApi);
#endif

  // TODO: should be moved lower once we finish migrating to rust.
  // RN it is safe here since the rust bundle init is largely just reigstering
  // function pointers. we need to init the rust bundle otherwise sqlite api
  // methods are not isntalled when we start calling rust
  crsql_ExtData *pExtData = sqlite3_crsqlrustbundle_init(db, pzErrMsg, pApi);
  if (pExtData == 0) {
    return SQLITE_ERROR;
  }

  if (rc == SQLITE_OK) {
    rc = sqlite3_create_module_v2(db, "crsql_changes", &crsql_changesModule,
                                  pExtData, 0);
  }
  if (rc == SQLITE_OK) {
    rc = sqlite3_create_module_v2(db, "crsql_version_guard",
                                  &crsql_versionGuardModule, pExtData, 0);
  }
  if (rc != SQLITE_OK) {
    // A failed load unloads the extension, so drop everything that points at
    // our code or at pExtData. The modules go first: dropping the rust
    // functions frees pExtData. If SQLite refuses to drop a function (it does
    // while statements run, e.g. under `SELECT load_extension()`), the rust
    // side pins the shared object so it is never unloaded.
    crsql_finalize(pExtData);
    sqlite3_create_module_v2(db, "crsql_changes", 0, 0, 0);
    sqlite3_create_module_v2(db, "crsql_version_guard", 0, 0, 0);
    crsql_rustbundle_unregister(db, pExtData);
    return rc;
  }

  if (rc == SQLITE_OK) {
#ifdef LIBSQL
    libsql_close_hook(db, closeHook, pExtData);
#endif
    // TODO: get the prior callback so we can call it rather than replace
    // it?
    sqlite3_commit_hook(db, crsql_commit_hook, pExtData);
    sqlite3_rollback_hook(db, crsql_rollback_hook, pExtData);
  }

  return rc;
}