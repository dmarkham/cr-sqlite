use alloc::format;
use alloc::string::String;
use alloc::string::ToString;
use core::ffi::c_int;
use sqlite::value;
use sqlite::Value;
use sqlite_nostd as sqlite;

// TODO: add an integration test that ensures NULL == NULL!
pub fn crsql_compare_sqlite_values(l: *mut sqlite::value, r: *mut sqlite::value) -> c_int {
    let l_type = l.value_type();
    let r_type = r.value_type();

    if l_type != r_type {
        // We swap the compare since we want null to be _less than_ all things
        // and null is assigned to ordinal 5 (greatest thing).
        return (r_type as i32) - (l_type as i32);
    }

    match l_type {
        sqlite::ColumnType::Blob => l.blob().cmp(r.blob()) as c_int,
        sqlite::ColumnType::Float => {
            let l_double = l.double();
            let r_double = r.double();
            if l_double < r_double {
                return -1;
            } else if l_double > r_double {
                return 1;
            }
            return 0;
        }
        sqlite::ColumnType::Integer => {
            let l_int = l.int64();
            let r_int = r.int64();
            // no subtraction since that could overflow the c_int return type
            if l_int < r_int {
                return -1;
            } else if l_int > r_int {
                return 1;
            }
            return 0;
        }
        sqlite::ColumnType::Null => 0,
        sqlite::ColumnType::Text => l.text().cmp(r.text()) as c_int,
    }
}

pub fn any_value_changed(left: &[*mut value], right: &[*mut value]) -> Result<bool, String> {
    if left.len() != right.len() {
        return Err(format!(
            "left and right values must have the same length: {} != {}",
            left.len(),
            right.len()
        ));
    }

    for (l, r) in left.iter().zip(right.iter()) {
        if crsql_compare_sqlite_values(*l, *r) != 0 {
            return Ok(true);
        }
    }

    Ok(false)
}

#[derive(Debug, PartialEq, Clone, Copy)]
pub enum Affinity {
    Blob,
    Text,
    Numeric,
    Integer,
    Real,
}

/// The column affinity sqlite derives from a declared type
/// (https://www.sqlite.org/datatype3.html#determination_of_column_affinity).
/// `ANY` in a STRICT table has no affinity.
pub fn affinity_of_decl_type(decl_type: &str, strict: bool) -> Affinity {
    let decl = decl_type.to_ascii_uppercase();
    if decl.contains("INT") {
        Affinity::Integer
    } else if decl.contains("CHAR") || decl.contains("CLOB") || decl.contains("TEXT") {
        Affinity::Text
    } else if decl.contains("BLOB") || decl.is_empty() || (strict && decl == "ANY") {
        Affinity::Blob
    } else if decl.contains("REAL") || decl.contains("FLOA") || decl.contains("DOUB") {
        Affinity::Real
    } else {
        Affinity::Numeric
    }
}

/// A SQL expression that yields `param` converted the way storing it in a column with
/// `affinity` would convert it, i.e. the value that column would read back.
/// Merges compare incoming values against stored ones; without this an incoming 5 and a
/// stored '5' (TEXT column) compare as different types and nodes can pick different winners.
pub fn affinity_conversion_sql(affinity: Affinity, param: &str) -> String {
    match affinity {
        Affinity::Blob => param.to_string(),
        Affinity::Text => format!(
            "CASE WHEN typeof({p}) IN ('integer', 'real') THEN CAST({p} AS TEXT) ELSE {p} END",
            p = param
        ),
        // `{p} = CAST({p} AS ...)` is true only for text that looks like a number: the CAST
        // gives the comparison numeric affinity, which converts {p} only if it is numeric.
        // Reals that are exact integers are stored as integers.
        Affinity::Numeric | Affinity::Integer => format!(
            "(SELECT CASE WHEN typeof(x) = 'real' AND x = CAST(x AS INTEGER)
                AND x > -9223372036854775808 AND x < 9223372036854775807
              THEN CAST(x AS INTEGER) ELSE x END
              FROM (SELECT CASE WHEN typeof({p}) = 'text' AND {p} = CAST({p} AS NUMERIC)
                THEN CAST({p} AS NUMERIC) ELSE {p} END AS x))",
            p = param
        ),
        Affinity::Real => format!(
            "CASE WHEN typeof({p}) = 'integer' THEN CAST({p} AS REAL)
              WHEN typeof({p}) = 'text' AND {p} = CAST({p} AS REAL) THEN CAST({p} AS REAL)
              ELSE {p} END",
            p = param
        ),
    }
}
