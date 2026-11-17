#define Py_NO_LINK_LIB 1
#include <Python.h>

#include "mock_platform.h"
#include "mock_python.h"

#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef struct MockPythonApi {
    PyInitConfig *(*init_config_create)(void);
    void (*init_config_free)(PyInitConfig *config);
    int (*init_config_get_error)(PyInitConfig *config, const char **message);
    int (*init_config_set_int)(PyInitConfig *config, const char *name, int64_t value);
    int (*init_config_set_str)(PyInitConfig *config, const char *name, const char *value);
    int (*init_config_set_str_list)(PyInitConfig *config, const char *name,
                                    size_t length, char *const *items);
    int (*initialize_from_init_config)(PyInitConfig *config);
    int (*is_initialized)(void);
    const char *(*get_version)(void);
    PyThreadState *(*eval_save_thread)(void);
    PyGILState_STATE (*gil_ensure)(void);
    void (*gil_release)(PyGILState_STATE state);
    PyObject *(*import_module)(const char *name);
    PyObject *(*get_attr_string)(PyObject *object, const char *name);
    int (*callable_check)(PyObject *object);
    PyObject *(*call_object)(PyObject *callable, PyObject *args);
    PyObject *(*call_one_arg)(PyObject *callable, PyObject *arg);
    PyObject *(*unicode_from_string)(const char *value);
    const char *(*unicode_as_utf8)(PyObject *unicode);
    PyObject *(*float_from_double)(double value);
    double (*float_as_double)(PyObject *value);
    PyObject *(*long_from_long)(long value);
    PyObject *(*tuple_pack)(Py_ssize_t count, ...);
    Py_ssize_t (*tuple_size)(PyObject *tuple);
    PyObject *(*tuple_get_item)(PyObject *tuple, Py_ssize_t index);
    PyObject *(*err_occurred)(void);
    PyObject *(*err_get_raised_exception)(void);
    void (*err_clear)(void);
    PyObject *(*object_str)(PyObject *object);
    void (*dec_ref)(PyObject *object);
} MockPythonApi;

typedef struct MockPythonState {
    MockLibrary library;
    MockPythonApi api;
    int api_ready;
    int interpreter_ready;
    int backend_ready;
    int fatal;
    char fatal_message[MOCK_ERROR_MAX];
    char package_root[MOCK_PATH_MAX];
    char runtime_root[MOCK_PATH_MAX];
    char profile[32];
    PyObject *module;
    PyObject *scalar_fn;
    PyObject *numpy_fn;
    PyObject *fenics_fn;
    PyObject *runtime_fn;
} MockPythonState;

static MockPythonState mock_python_state;

static void mock_python_set_error(char *error, size_t error_size,
                                  const char *context, const char *detail)
{
    if (error == NULL || error_size == 0) {
        return;
    }
    if (detail != NULL && detail[0] != '\0') {
        snprintf(error, error_size, "%s: %s", context, detail);
    } else {
        snprintf(error, error_size, "%s", context);
    }
    error[error_size - 1] = '\0';
}

static int mock_python_copy(const char *value, char *out, size_t out_size,
                            char *error, size_t error_size, const char *label)
{
    size_t length;
    if (value == NULL || out == NULL || out_size == 0) {
        mock_python_set_error(error, error_size, "invalid Python text result", label);
        return -1;
    }
    length = strlen(value);
    if (length + 1 > out_size) {
        mock_python_set_error(error, error_size, "Python text result exceeds bridge buffer", label);
        return -1;
    }
    memcpy(out, value, length + 1);
    return 0;
}

static void mock_python_capture_exception(const char *context,
                                          char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyObject *exception = state->api.err_get_raised_exception();
    PyObject *text_object = NULL;
    const char *text = NULL;
    if (exception != NULL) {
        text_object = state->api.object_str(exception);
        if (text_object != NULL) {
            text = state->api.unicode_as_utf8(text_object);
        }
    }
    if (text == NULL) {
        if (state->api.err_occurred() != NULL) {
            state->api.err_clear();
        }
        text = "Python operation failed without a printable exception";
    }
    mock_python_set_error(error, error_size, context, text);
    if (text_object != NULL) {
        state->api.dec_ref(text_object);
    }
    if (exception != NULL) {
        state->api.dec_ref(exception);
    }
}

#define MOCK_LOAD_PY_API(state, field, type, symbol)                                \
    do {                                                                            \
        void *mock_symbol__ = mock_platform_library_symbol((state)->library, symbol);\
        if (mock_symbol__ == NULL) {                                                 \
            mock_python_set_error(error, error_size,                                \
                                  "CPython provider is missing required API", symbol);\
            return -1;                                                              \
        }                                                                           \
        (state)->api.field = (type)mock_symbol__;                                    \
    } while (0)

static int mock_python_load_api(const char *provider,
                                char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    if (state->api_ready) {
        return 0;
    }
    if (state->library == NULL) {
        state->library = mock_platform_load_python(provider, error, error_size);
        if (state->library == NULL) {
            return -1;
        }
    }
    memset(&state->api, 0, sizeof(state->api));
    MOCK_LOAD_PY_API(state, init_config_create, PyInitConfig *(*)(void), "PyInitConfig_Create");
    MOCK_LOAD_PY_API(state, init_config_free, void (*)(PyInitConfig *), "PyInitConfig_Free");
    MOCK_LOAD_PY_API(state, init_config_get_error,
                     int (*)(PyInitConfig *, const char **), "PyInitConfig_GetError");
    MOCK_LOAD_PY_API(state, init_config_set_int,
                     int (*)(PyInitConfig *, const char *, int64_t), "PyInitConfig_SetInt");
    MOCK_LOAD_PY_API(state, init_config_set_str,
                     int (*)(PyInitConfig *, const char *, const char *), "PyInitConfig_SetStr");
    MOCK_LOAD_PY_API(state, init_config_set_str_list,
                     int (*)(PyInitConfig *, const char *, size_t, char *const *),
                     "PyInitConfig_SetStrList");
    MOCK_LOAD_PY_API(state, initialize_from_init_config,
                     int (*)(PyInitConfig *), "Py_InitializeFromInitConfig");
    MOCK_LOAD_PY_API(state, is_initialized, int (*)(void), "Py_IsInitialized");
    MOCK_LOAD_PY_API(state, get_version, const char *(*)(void), "Py_GetVersion");
    MOCK_LOAD_PY_API(state, eval_save_thread, PyThreadState *(*)(void), "PyEval_SaveThread");
    MOCK_LOAD_PY_API(state, gil_ensure, PyGILState_STATE (*)(void), "PyGILState_Ensure");
    MOCK_LOAD_PY_API(state, gil_release, void (*)(PyGILState_STATE), "PyGILState_Release");
    MOCK_LOAD_PY_API(state, import_module, PyObject *(*)(const char *), "PyImport_ImportModule");
    MOCK_LOAD_PY_API(state, get_attr_string,
                     PyObject *(*)(PyObject *, const char *), "PyObject_GetAttrString");
    MOCK_LOAD_PY_API(state, callable_check, int (*)(PyObject *), "PyCallable_Check");
    MOCK_LOAD_PY_API(state, call_object,
                     PyObject *(*)(PyObject *, PyObject *), "PyObject_CallObject");
    MOCK_LOAD_PY_API(state, call_one_arg,
                     PyObject *(*)(PyObject *, PyObject *), "PyObject_CallOneArg");
    MOCK_LOAD_PY_API(state, unicode_from_string,
                     PyObject *(*)(const char *), "PyUnicode_FromString");
    MOCK_LOAD_PY_API(state, unicode_as_utf8,
                     const char *(*)(PyObject *), "PyUnicode_AsUTF8");
    MOCK_LOAD_PY_API(state, float_from_double,
                     PyObject *(*)(double), "PyFloat_FromDouble");
    MOCK_LOAD_PY_API(state, float_as_double,
                     double (*)(PyObject *), "PyFloat_AsDouble");
    MOCK_LOAD_PY_API(state, long_from_long,
                     PyObject *(*)(long), "PyLong_FromLong");
    MOCK_LOAD_PY_API(state, tuple_pack,
                     PyObject *(*)(Py_ssize_t, ...), "PyTuple_Pack");
    MOCK_LOAD_PY_API(state, tuple_size,
                     Py_ssize_t (*)(PyObject *), "PyTuple_Size");
    MOCK_LOAD_PY_API(state, tuple_get_item,
                     PyObject *(*)(PyObject *, Py_ssize_t), "PyTuple_GetItem");
    MOCK_LOAD_PY_API(state, err_occurred, PyObject *(*)(void), "PyErr_Occurred");
    MOCK_LOAD_PY_API(state, err_get_raised_exception,
                     PyObject *(*)(void), "PyErr_GetRaisedException");
    MOCK_LOAD_PY_API(state, err_clear, void (*)(void), "PyErr_Clear");
    MOCK_LOAD_PY_API(state, object_str, PyObject *(*)(PyObject *), "PyObject_Str");
    MOCK_LOAD_PY_API(state, dec_ref, void (*)(PyObject *), "Py_DecRef");
    state->api_ready = 1;
    return 0;
}

static int mock_init_config_error(PyInitConfig *config, const char *context,
                                  char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    const char *message = NULL;
    if (config != NULL && state->api.init_config_get_error(config, &message) > 0 && message != NULL) {
        mock_python_set_error(error, error_size, context, message);
    } else {
        mock_python_set_error(error, error_size, context, "unknown CPython initialization error");
    }
    return -1;
}

static int mock_init_set_int(PyInitConfig *config, const char *name, int64_t value,
                             char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    if (state->api.init_config_set_int(config, name, value) < 0) {
        return mock_init_config_error(config, "cannot configure isolated CPython", error, error_size);
    }
    return 0;
}

static int mock_init_set_str(PyInitConfig *config, const char *name, const char *value,
                             char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    if (state->api.init_config_set_str(config, name, value) < 0) {
        return mock_init_config_error(config, "cannot configure isolated CPython", error, error_size);
    }
    return 0;
}

static int mock_python_build_paths(const char *package_root, const char *runtime_root,
                                   char *provider, char *home, char *executable,
                                   char module_paths[][MOCK_PATH_MAX], size_t *module_count,
                                   char *error, size_t error_size)
{
    char backend[MOCK_PATH_MAX];
    char stdlib[MOCK_PATH_MAX];
    char dynload[MOCK_PATH_MAX];
    char site[MOCK_PATH_MAX];
    size_t count = 0;

    if (mock_platform_join(package_root, "python", backend, sizeof(backend), error, error_size) != 0) {
        return -1;
    }
#ifdef _WIN32
    if (mock_platform_join(runtime_root, "python314.dll", provider, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_python_copy(runtime_root, home, MOCK_PATH_MAX, error, error_size, "runtime home") != 0 ||
        mock_platform_join(runtime_root, "python.exe", executable, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_platform_join(runtime_root, "Lib", stdlib, sizeof(stdlib), error, error_size) != 0 ||
        mock_platform_join(runtime_root, "DLLs", dynload, sizeof(dynload), error, error_size) != 0 ||
        mock_platform_join(runtime_root, "Lib\\site-packages", site, sizeof(site), error, error_size) != 0) {
        return -1;
    }
    {
        char zip_path[MOCK_PATH_MAX];
        if (mock_platform_join(runtime_root, "python314.zip", zip_path, sizeof(zip_path), error, error_size) != 0) {
            return -1;
        }
        if (mock_platform_is_file(zip_path)) {
            if (mock_python_copy(zip_path, module_paths[count++], MOCK_PATH_MAX,
                                 error, error_size, "python314.zip") != 0) {
                return -1;
            }
        }
    }
#elif defined(__APPLE__)
    if (mock_platform_join(runtime_root, "Python.framework/Versions/3.14/Python",
                           provider, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_platform_join(runtime_root, "Python.framework/Versions/3.14",
                           home, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_platform_join(home, "bin/python3.14", executable, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_platform_join(home, "lib/python3.14", stdlib, sizeof(stdlib), error, error_size) != 0 ||
        mock_platform_join(stdlib, "lib-dynload", dynload, sizeof(dynload), error, error_size) != 0 ||
        mock_platform_join(stdlib, "site-packages", site, sizeof(site), error, error_size) != 0) {
        return -1;
    }
#else
    if (mock_platform_join(runtime_root, "lib/libpython3.14.so.1.0",
                           provider, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_python_copy(runtime_root, home, MOCK_PATH_MAX, error, error_size, "runtime home") != 0 ||
        mock_platform_join(runtime_root, "bin/python3.14", executable, MOCK_PATH_MAX, error, error_size) != 0 ||
        mock_platform_join(runtime_root, "lib/python3.14", stdlib, sizeof(stdlib), error, error_size) != 0 ||
        mock_platform_join(stdlib, "lib-dynload", dynload, sizeof(dynload), error, error_size) != 0 ||
        mock_platform_join(stdlib, "site-packages", site, sizeof(site), error, error_size) != 0) {
        return -1;
    }
#endif
    if (!mock_platform_is_file(provider)) {
        mock_python_set_error(error, error_size, "CPython provider is missing", provider);
        return -1;
    }
    if (!mock_platform_is_file(executable)) {
        mock_python_set_error(error, error_size, "CPython executable is missing", executable);
        return -1;
    }
    if (!mock_platform_is_directory(stdlib) || !mock_platform_is_directory(dynload) ||
        !mock_platform_is_directory(site) || !mock_platform_is_directory(backend)) {
        mock_python_set_error(error, error_size, "staged CPython import roots are incomplete", runtime_root);
        return -1;
    }
    if (mock_python_copy(stdlib, module_paths[count++], MOCK_PATH_MAX, error, error_size, "stdlib") != 0 ||
        mock_python_copy(dynload, module_paths[count++], MOCK_PATH_MAX, error, error_size, "lib-dynload") != 0 ||
        mock_python_copy(site, module_paths[count++], MOCK_PATH_MAX, error, error_size, "site-packages") != 0 ||
        mock_python_copy(backend, module_paths[count++], MOCK_PATH_MAX, error, error_size, "mock backend") != 0) {
        return -1;
    }
    *module_count = count;
    return 0;
}

static void mock_python_clear_backend_locked(void)
{
    MockPythonState *state = &mock_python_state;
    PyObject **objects[] = {
        &state->runtime_fn, &state->fenics_fn, &state->numpy_fn,
        &state->scalar_fn, &state->module
    };
    size_t index;
    for (index = 0; index < sizeof(objects) / sizeof(objects[0]); ++index) {
        if (*objects[index] != NULL) {
            state->api.dec_ref(*objects[index]);
            *objects[index] = NULL;
        }
    }
    state->backend_ready = 0;
}

static PyObject *mock_python_callable_locked(PyObject *module, const char *name,
                                             char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyObject *value = state->api.get_attr_string(module, name);
    if (value == NULL) {
        mock_python_capture_exception("mock backend is missing required operation", error, error_size);
        return NULL;
    }
    if (!state->api.callable_check(value)) {
        state->api.dec_ref(value);
        mock_python_set_error(error, error_size, "mock backend operation is not callable", name);
        return NULL;
    }
    return value;
}

static int mock_python_prepare_backend_locked(const char *package_root,
                                              const char *runtime_root,
                                              const char *profile,
                                              char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyObject *module = NULL;
    PyObject *configure = NULL;
    PyObject *package = NULL;
    PyObject *runtime = NULL;
    PyObject *profile_object = NULL;
    PyObject *args = NULL;
    PyObject *configured = NULL;
    const char *configured_profile = NULL;

    mock_python_clear_backend_locked();
    module = state->api.import_module("mock_backend");
    if (module == NULL) {
        mock_python_capture_exception("cannot import packaged mock_backend", error, error_size);
        goto fail;
    }
    configure = mock_python_callable_locked(module, "_configure", error, error_size);
    if (configure == NULL) {
        goto fail;
    }
    package = state->api.unicode_from_string(package_root);
    runtime = state->api.unicode_from_string(runtime_root);
    profile_object = state->api.unicode_from_string(profile);
    if (package == NULL || runtime == NULL || profile_object == NULL) {
        mock_python_capture_exception("cannot encode controlled package paths for Python", error, error_size);
        goto fail;
    }
    args = state->api.tuple_pack(3, package, runtime, profile_object);
    if (args == NULL) {
        mock_python_capture_exception("cannot construct mock backend configuration", error, error_size);
        goto fail;
    }
    configured = state->api.call_object(configure, args);
    if (configured == NULL) {
        mock_python_capture_exception("mock backend rejected staged runtime", error, error_size);
        goto fail;
    }
    configured_profile = state->api.unicode_as_utf8(configured);
    if (configured_profile == NULL) {
        mock_python_capture_exception("mock backend returned invalid profile identity", error, error_size);
        goto fail;
    }
    if (strcmp(configured_profile, profile) != 0) {
        mock_python_set_error(error, error_size, "mock backend profile differs from package marker",
                              configured_profile);
        goto fail;
    }

    state->scalar_fn = mock_python_callable_locked(module, "scalar", error, error_size);
    state->numpy_fn = mock_python_callable_locked(module, "numpy_sum_squares", error, error_size);
    state->fenics_fn = mock_python_callable_locked(module, "fenics_poisson", error, error_size);
    state->runtime_fn = mock_python_callable_locked(module, "runtime_info", error, error_size);
    if (state->scalar_fn == NULL || state->numpy_fn == NULL ||
        state->fenics_fn == NULL || state->runtime_fn == NULL) {
        goto fail;
    }
    state->module = module;
    module = NULL;
    state->backend_ready = 1;

    state->api.dec_ref(configured);
    state->api.dec_ref(args);
    state->api.dec_ref(profile_object);
    state->api.dec_ref(runtime);
    state->api.dec_ref(package);
    state->api.dec_ref(configure);
    return 0;

fail:
    if (configured != NULL) state->api.dec_ref(configured);
    if (args != NULL) state->api.dec_ref(args);
    if (profile_object != NULL) state->api.dec_ref(profile_object);
    if (runtime != NULL) state->api.dec_ref(runtime);
    if (package != NULL) state->api.dec_ref(package);
    if (configure != NULL) state->api.dec_ref(configure);
    if (module != NULL) state->api.dec_ref(module);
    mock_python_clear_backend_locked();
    return -1;
}

static int mock_python_start_interpreter(const char *package_root,
                                         const char *runtime_root,
                                         const char *profile,
                                         char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    char provider[MOCK_PATH_MAX];
    char home[MOCK_PATH_MAX];
    char executable[MOCK_PATH_MAX];
    char paths[5][MOCK_PATH_MAX];
    char *path_items[5];
    size_t path_count = 0;
    size_t index;
    PyInitConfig *config = NULL;
    int prepare_result;
    const char *version;

    if (mock_python_build_paths(package_root, runtime_root, provider, home, executable,
                                paths, &path_count, error, error_size) != 0) {
        return -1;
    }
    if (mock_python_load_api(provider, error, error_size) != 0) {
        return -1;
    }
    if (state->api.is_initialized()) {
        snprintf(state->fatal_message, sizeof(state->fatal_message),
                 "CPython was already initialized before the reference bridge could apply its isolated configuration");
        state->fatal_message[sizeof(state->fatal_message) - 1] = '\0';
        state->fatal = 1;
        mock_python_set_error(error, error_size,
                              "pre-existing CPython interpreter would violate reference bridge isolation",
                              state->fatal_message);
        return -1;
    }
    config = state->api.init_config_create();
    if (config == NULL) {
        mock_python_set_error(error, error_size, "cannot allocate CPython initialization config", NULL);
        return -1;
    }
    if (mock_init_set_int(config, "isolated", 1, error, error_size) != 0 ||
        mock_init_set_int(config, "use_environment", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "site_import", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "user_site_directory", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "safe_path", 1, error, error_size) != 0 ||
        mock_init_set_int(config, "write_bytecode", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "install_signal_handlers", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "parse_argv", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "configure_c_stdio", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "configure_locale", 0, error, error_size) != 0 ||
        mock_init_set_int(config, "utf8_mode", 1, error, error_size) != 0 ||
        mock_init_set_str(config, "home", home, error, error_size) != 0 ||
        mock_init_set_str(config, "program_name", executable, error, error_size) != 0 ||
        mock_init_set_str(config, "executable", executable, error, error_size) != 0 ||
        mock_init_set_str(config, "base_executable", executable, error, error_size) != 0 ||
        mock_init_set_str(config, "prefix", home, error, error_size) != 0 ||
        mock_init_set_str(config, "base_prefix", home, error, error_size) != 0 ||
        mock_init_set_str(config, "exec_prefix", home, error, error_size) != 0 ||
        mock_init_set_str(config, "base_exec_prefix", home, error, error_size) != 0) {
        state->api.init_config_free(config);
        return -1;
    }
    for (index = 0; index < path_count; ++index) {
        path_items[index] = paths[index];
    }
    if (state->api.init_config_set_str_list(config, "module_search_paths",
                                            path_count, path_items) < 0) {
        mock_init_config_error(config, "cannot set controlled CPython module paths", error, error_size);
        state->api.init_config_free(config);
        return -1;
    }
    {
        char *argv_items[] = {executable};
        if (state->api.init_config_set_str_list(config, "argv", 1, argv_items) < 0) {
            mock_init_config_error(config, "cannot set controlled CPython argv", error, error_size);
            state->api.init_config_free(config);
            return -1;
        }
    }
    if (state->api.initialize_from_init_config(config) < 0) {
        mock_init_config_error(config, "isolated CPython initialization failed", error, error_size);
        snprintf(state->fatal_message, sizeof(state->fatal_message), "%s",
                 error != NULL && error[0] != '\0'
                     ? error
                     : "isolated CPython initialization failed");
        state->fatal_message[sizeof(state->fatal_message) - 1] = '\0';
        state->fatal = 1;
        state->api.init_config_free(config);
        return -1;
    }
    state->api.init_config_free(config);
    state->interpreter_ready = 1;

    version = state->api.get_version();
    if (version == NULL || strncmp(version, "3.14.10", 7) != 0 ||
        (version[7] != '\0' && version[7] != ' ')) {
        snprintf(state->fatal_message, sizeof(state->fatal_message),
                 "running interpreter is not CPython 3.14.10: %s",
                 version != NULL ? version : "unknown");
        state->fatal_message[sizeof(state->fatal_message) - 1] = '\0';
        state->fatal = 1;
        mock_python_set_error(error, error_size, "runtime identity mismatch", state->fatal_message);
        state->api.eval_save_thread();
        return -1;
    }

    prepare_result = mock_python_prepare_backend_locked(package_root, runtime_root, profile,
                                                        error, error_size);
    state->api.eval_save_thread();
    return prepare_result;
}

int mock_python_initialize(const char *package_root, const char *runtime_root,
                           const char *profile,
                           char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyGILState_STATE gil_state;
    int result;
    if (state->fatal) {
        mock_python_set_error(error, error_size, "CPython runtime is unusable", state->fatal_message);
        return -1;
    }
    if (!state->interpreter_ready) {
        if (mock_python_copy(package_root, state->package_root, sizeof(state->package_root),
                             error, error_size, "package root") != 0 ||
            mock_python_copy(runtime_root, state->runtime_root, sizeof(state->runtime_root),
                             error, error_size, "runtime root") != 0 ||
            mock_python_copy(profile, state->profile, sizeof(state->profile),
                             error, error_size, "profile") != 0) {
            return -1;
        }
        return mock_python_start_interpreter(package_root, runtime_root, profile,
                                             error, error_size);
    }
    if (strcmp(state->package_root, package_root) != 0 ||
        strcmp(state->runtime_root, runtime_root) != 0 ||
        strcmp(state->profile, profile) != 0) {
        mock_python_set_error(error, error_size,
                              "bridge runtime identity changed within one host process", package_root);
        return -1;
    }
    if (state->backend_ready) {
        return 0;
    }
    gil_state = state->api.gil_ensure();
    result = mock_python_prepare_backend_locked(package_root, runtime_root, profile,
                                                error, error_size);
    state->api.gil_release(gil_state);
    return result;
}

int mock_python_is_initialized(void)
{
    return mock_python_state.interpreter_ready;
}

static int mock_python_call_numeric(PyObject *callable, PyObject *argument,
                                    double *result,
                                    char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyObject *value;
    double number;
    if (!state->backend_ready || callable == NULL) {
        mock_python_set_error(error, error_size, "mock backend is not initialized", NULL);
        return -1;
    }
    if (argument == NULL) {
        mock_python_capture_exception("cannot create Python operation argument", error, error_size);
        return -1;
    }
    value = state->api.call_one_arg(callable, argument);
    state->api.dec_ref(argument);
    if (value == NULL) {
        mock_python_capture_exception("mock backend operation failed", error, error_size);
        return -1;
    }
    number = state->api.float_as_double(value);
    state->api.dec_ref(value);
    if (state->api.err_occurred() != NULL) {
        mock_python_capture_exception("mock backend returned a non-numeric result", error, error_size);
        return -1;
    }
    if (!isfinite(number)) {
        mock_python_set_error(error, error_size, "mock backend returned a non-finite result", NULL);
        return -1;
    }
    *result = number;
    return 0;
}

int mock_python_scalar(double value, double *result,
                       char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyGILState_STATE gil_state;
    PyObject *argument;
    int call_result;
    if (!state->backend_ready) {
        mock_python_set_error(error, error_size, "mock backend is not initialized", NULL);
        return -1;
    }
    gil_state = state->api.gil_ensure();
    argument = state->api.float_from_double(value);
    call_result = mock_python_call_numeric(state->scalar_fn, argument, result, error, error_size);
    state->api.gil_release(gil_state);
    return call_result;
}

int mock_python_numpy(long value, double *result,
                      char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyGILState_STATE gil_state;
    PyObject *argument;
    int call_result;
    if (!state->backend_ready) {
        mock_python_set_error(error, error_size, "mock backend is not initialized", NULL);
        return -1;
    }
    gil_state = state->api.gil_ensure();
    argument = state->api.long_from_long(value);
    call_result = mock_python_call_numeric(state->numpy_fn, argument, result, error, error_size);
    state->api.gil_release(gil_state);
    return call_result;
}

int mock_python_fenics(double *result,
                       char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyGILState_STATE gil_state;
    PyObject *value;
    double number;
    if (!state->backend_ready) {
        mock_python_set_error(error, error_size, "mock backend is not initialized", NULL);
        return -1;
    }
    gil_state = state->api.gil_ensure();
    value = state->api.call_object(state->fenics_fn, NULL);
    if (value == NULL) {
        mock_python_capture_exception("mock backend FEniCS operation failed", error, error_size);
        state->api.gil_release(gil_state);
        return -1;
    }
    number = state->api.float_as_double(value);
    state->api.dec_ref(value);
    if (state->api.err_occurred() != NULL) {
        mock_python_capture_exception("mock backend FEniCS result is not numeric", error, error_size);
        state->api.gil_release(gil_state);
        return -1;
    }
    if (!isfinite(number)) {
        mock_python_set_error(error, error_size, "mock backend FEniCS result is non-finite", NULL);
        state->api.gil_release(gil_state);
        return -1;
    }
    *result = number;
    state->api.gil_release(gil_state);
    return 0;
}

int mock_python_runtime(MockRuntimeInfo *info,
                        char *error, size_t error_size)
{
    MockPythonState *state = &mock_python_state;
    PyGILState_STATE gil_state;
    PyObject *value;
    Py_ssize_t index;
    char *destinations[4];
    size_t capacities[4];
    const char *labels[4] = {"python_version", "python_abi", "runtime_root", "profile"};
    int result = -1;
    if (!state->backend_ready || info == NULL) {
        mock_python_set_error(error, error_size, "mock backend is not initialized", NULL);
        return -1;
    }
    destinations[0] = info->python_version;
    destinations[1] = info->python_abi;
    destinations[2] = info->runtime_root;
    destinations[3] = info->profile;
    capacities[0] = sizeof(info->python_version);
    capacities[1] = sizeof(info->python_abi);
    capacities[2] = sizeof(info->runtime_root);
    capacities[3] = sizeof(info->profile);
    memset(info, 0, sizeof(*info));

    gil_state = state->api.gil_ensure();
    value = state->api.call_object(state->runtime_fn, NULL);
    if (value == NULL) {
        mock_python_capture_exception("mock backend runtime operation failed", error, error_size);
        goto done;
    }
    if (state->api.tuple_size(value) != 4) {
        if (state->api.err_occurred() != NULL) {
            mock_python_capture_exception("mock backend runtime result is not a tuple", error, error_size);
        } else {
            mock_python_set_error(error, error_size, "mock backend runtime result has wrong field count", NULL);
        }
        state->api.dec_ref(value);
        goto done;
    }
    for (index = 0; index < 4; ++index) {
        PyObject *item = state->api.tuple_get_item(value, index);
        const char *text;
        if (item == NULL) {
            mock_python_capture_exception("mock backend runtime tuple is malformed", error, error_size);
            state->api.dec_ref(value);
            goto done;
        }
        text = state->api.unicode_as_utf8(item);
        if (text == NULL) {
            mock_python_capture_exception("mock backend runtime field is not text", error, error_size);
            state->api.dec_ref(value);
            goto done;
        }
        if (mock_python_copy(text, destinations[index], capacities[index],
                             error, error_size, labels[index]) != 0) {
            state->api.dec_ref(value);
            goto done;
        }
    }
    state->api.dec_ref(value);
    result = 0;

done:
    state->api.gil_release(gil_state);
    return result;
}
