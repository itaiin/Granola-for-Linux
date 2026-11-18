#!/usr/bin/env python3
"""Add Database.updateHook() to an upstream better-sqlite3-multiple-ciphers tree.

Newer Granola builds (7.7xx+) no longer ship the C++ source of their patched
fork inside app.asar.unpacked, only the macOS binary. Granola's JS layer calls
db.updateHook((op, dbName, table, rowid) => ...), so we re-create the native
side on top of the matching npm release.

usage: patch-update-hook.py <package-dir>
"""
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
hpp = root / "src/objects/database.hpp"
cpp = root / "src/objects/database.cpp"
h, c = hpp.read_text(), cpp.read_text()

if "JS_updateHook" in h:
    print("    updateHook already present, skipping patch")
    sys.exit(0)


def sub(text, old, new, what):
    if old not in text:
        sys.exit(f"patch-update-hook: anchor not found ({what}); upstream source changed")
    return text.replace(old, new, 1)


# Declarations. The new member goes at the very end so the State view
# (which aliases the leading fields by layout) is untouched.
h = sub(h, "\tstatic NODE_METHOD(JS_unsafeMode);\n",
        "\tstatic NODE_METHOD(JS_unsafeMode);\n"
        "\tstatic NODE_METHOD(JS_updateHook);\n"
        "\tstatic void UpdateHookCallback(void* self, int op, const char* db_name, const char* table, sqlite3_int64 rowid);\n",
        "method decl")
h = sub(h, "\tstd::set<Backup*, CompareBackup> backups;\n};",
        "\tstd::set<Backup*, CompareBackup> backups;\n"
        "\tv8::Global<v8::Function> update_hook;\n};",
        "member decl")

c = sub(c, '\tSetPrototypeMethod(isolate, data, t, "unsafeMode", JS_unsafeMode);\n',
        '\tSetPrototypeMethod(isolate, data, t, "unsafeMode", JS_unsafeMode);\n'
        '\tSetPrototypeMethod(isolate, data, t, "updateHook", JS_updateHook);\n',
        "prototype registration")

# Drop the hook before the handle is closed so sqlite never calls into a
# dead object.
c = sub(c, "\t\tstmts.clear();\n\t\tbackups.clear();\n\t\tint status = sqlite3_close(db_handle);",
        "\t\tstmts.clear();\n\t\tbackups.clear();\n"
        "\t\tsqlite3_update_hook(db_handle, NULL, NULL);\n"
        "\t\tupdate_hook.Reset();\n"
        "\t\tint status = sqlite3_close(db_handle);",
        "close handles")

c += r'''
// Granola addition: db.updateHook(fn | null). fn(op, dbName, table, rowid)
NODE_METHOD(Database::JS_updateHook) {
	Database* db = Unwrap<Database>(info.This());
	REQUIRE_DATABASE_OPEN(db);
	UseIsolate;
	if (info.Length() == 0 || (info[0]->IsNull() || info[0]->IsUndefined())) {
		sqlite3_update_hook(db->db_handle, NULL, NULL);
		db->update_hook.Reset();
		return;
	}
	REQUIRE_ARGUMENT_FUNCTION(first, v8::Local<v8::Function> fn);
	db->update_hook.Reset(isolate, fn);
	sqlite3_update_hook(db->db_handle, UpdateHookCallback, db);
}

void Database::UpdateHookCallback(void* self, int op, const char* db_name, const char* table, sqlite3_int64 rowid) {
	Database* db = static_cast<Database*>(self);
	if (db->update_hook.IsEmpty()) return;
	v8::Isolate* isolate = v8::Isolate::GetCurrent();
	v8::HandleScope scope(isolate);
	v8::Local<v8::Context> ctx = isolate->GetCurrentContext();
	const char* op_name = op == SQLITE_INSERT ? "insert" : op == SQLITE_UPDATE ? "update" : op == SQLITE_DELETE ? "delete" : "unknown";
	v8::Local<v8::Value> args[4] = {
		StringFromUtf8(isolate, op_name, -1),
		StringFromUtf8(isolate, db_name, -1),
		StringFromUtf8(isolate, table, -1),
		v8::Number::New(isolate, static_cast<double>(rowid)),
	};
	// We are in the middle of sqlite3_step(); an exception cannot propagate
	// from here, so swallow it rather than corrupt statement state.
	v8::TryCatch try_catch(isolate);
	(void)db->update_hook.Get(isolate)->Call(ctx, v8::Undefined(isolate), 4, args);
}
'''

hpp.write_text(h)
cpp.write_text(c)
print("    patched in Database::updateHook")

# Granola's build also enables SQLITE_ENABLE_BYTECODE_VTAB (the only compile
# option that differs from upstream). Its live queries read the `tables_used`
# virtual table; without it the UI dies with "no such table: tables_used".
defs = root / "deps/defines.gypi"
d = defs.read_text()
if "SQLITE_ENABLE_BYTECODE_VTAB" not in d:
    d = sub(d, "    'SQLITE_ENABLE_STAT4',\n",
            "    'SQLITE_ENABLE_STAT4',\n    'SQLITE_ENABLE_BYTECODE_VTAB',\n",
            "defines.gypi")
    defs.write_text(d)
    print("    enabled SQLITE_ENABLE_BYTECODE_VTAB")

# --- V8 15 (Electron 45+) compat ---------------------------------------------
# Upstream 12.9.0 targets V8 13/14. V8 15 dropped GetPrototypeV2/HolderV2 again
# and made v8::External require a type tag.
mac = root / "src/util/macros.cpp"
m = mac.read_text()
if "V8_MAJOR_VERSION >= 15" not in m:
    m = sub(m, "#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13\n// v8::Object::GetPrototype",
            "#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 15\n"
            "#define GET_PROTOTYPE(obj) ((obj)->GetPrototype())\n"
            "#elif defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13\n// v8::Object::GetPrototype",
            "GET_PROTOTYPE")
    m = sub(m, "#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13\n#define PROPERTY_HOLDER",
            "#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 15\n"
            "#define PROPERTY_HOLDER(info) (info).Holder()\n"
            "#elif defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 13\n#define PROPERTY_HOLDER",
            "PROPERTY_HOLDER")
    m = sub(m, "#define OnlyAddon static_cast<Addon*>(info.Data().As<v8::External>()->Value())",
            "#if defined(V8_MAJOR_VERSION) && V8_MAJOR_VERSION >= 15\n"
            "#define EXTERNAL_TAG , v8::kExternalPointerTypeTagDefault\n"
            "#define EXTERNAL_TAG_ONLY v8::kExternalPointerTypeTagDefault\n"
            "#else\n#define EXTERNAL_TAG\n#define EXTERNAL_TAG_ONLY\n#endif\n"
            "#define OnlyAddon static_cast<Addon*>(info.Data().As<v8::External>()->Value(EXTERNAL_TAG_ONLY))",
            "OnlyAddon")
    mac.write_text(m)

    for f in (root / "src").rglob("*.cpp"):
        t = f.read_text()
        t2 = t.replace("v8::External::New(isolate, addon)", "v8::External::New(isolate, addon EXTERNAL_TAG)")
        t2 = t2.replace("As<v8::External>()->Value()", "As<v8::External>()->Value(EXTERNAL_TAG_ONLY)")
        if t2 != t:
            f.write_text(t2)
    print("    applied V8 15 compat shims")
