#include "mock_host.h"
#include "mock_platform.h"
#include "mock_python.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#ifdef _WIN32
#define MOCK_EXPORT __declspec(dllexport)
#else
#define MOCK_EXPORT __attribute__((visibility("default")))
#endif

typedef struct MockPackageState {
    int ready;
    char package_root[MOCK_PATH_MAX];
    char runtime_root[MOCK_PATH_MAX];
    char profile[32];
} MockPackageState;

static MockPackageState mock_package_state;
static int mock_mex_locked;

static int mock_parent_path(const char *path, char *out, size_t out_size)
{
    const char *slash = strrchr(path, '/');
    const char *backslash = strrchr(path, '\\');
    const char *last = slash;
    size_t length;
    if (backslash != NULL && (last == NULL || backslash > last)) {
        last = backslash;
    }
    if (last == NULL || last == path) {
        return -1;
    }
    length = (size_t)(last - path);
    if (length + 1 > out_size) {
        return -1;
    }
    memcpy(out, path, length);
    out[length] = '\0';
    return 0;
}

static int mock_path_equal(const char *left, const char *right)
{
#ifdef _WIN32
    return _stricmp(left, right) == 0;
#else
    return strcmp(left, right) == 0;
#endif
}

static int mock_package_load(char *error, size_t error_size)
{
    MockPackageState *state = &mock_package_state;
    char runtime_candidate[MOCK_PATH_MAX];
    char runtime_parent[MOCK_PATH_MAX];
    char profile_path[MOCK_PATH_MAX];
    char backend_path[MOCK_PATH_MAX];

    if (state->ready) {
        return 0;
    }
    if (mock_platform_module_directory(state->package_root, sizeof(state->package_root),
                                       error, error_size) != 0 ||
        mock_platform_join(state->package_root, "runtime", runtime_candidate,
                           sizeof(runtime_candidate), error, error_size) != 0 ||
        mock_platform_canonical_path(runtime_candidate, state->runtime_root,
                                     sizeof(state->runtime_root), error, error_size) != 0) {
        return -1;
    }
    if (!mock_platform_is_directory(state->runtime_root) ||
        mock_parent_path(state->runtime_root, runtime_parent, sizeof(runtime_parent)) != 0 ||
        !mock_path_equal(runtime_parent, state->package_root)) {
        snprintf(error, error_size,
                 "runtime directory is not the canonical package-relative runtime path: %s",
                 state->runtime_root);
        error[error_size - 1] = '\0';
        return -1;
    }
    if (mock_platform_join(state->package_root, "profile.txt", profile_path,
                           sizeof(profile_path), error, error_size) != 0 ||
        mock_platform_read_small_text(profile_path, state->profile,
                                      sizeof(state->profile), error, error_size) != 0) {
        return -1;
    }
    if (strcmp(state->profile, "core") != 0 &&
        strcmp(state->profile, "numpy") != 0 &&
        strcmp(state->profile, "fenics") != 0) {
        snprintf(error, error_size, "unsupported package profile marker: %s", state->profile);
        error[error_size - 1] = '\0';
        return -1;
    }
    if (mock_platform_join(state->package_root, "python/mock_backend.py", backend_path,
                           sizeof(backend_path), error, error_size) != 0 ||
        !mock_platform_is_file(backend_path)) {
        snprintf(error, error_size, "packaged mock backend is missing: %s", backend_path);
        error[error_size - 1] = '\0';
        return -1;
    }
    state->ready = 1;
    return 0;
}

static int mock_profile_supports(const char *operation)
{
    const char *profile = mock_package_state.profile;
    if (strcmp(operation, "numpy") == 0) {
        return strcmp(profile, "numpy") == 0 || strcmp(profile, "fenics") == 0;
    }
    if (strcmp(operation, "fenics") == 0) {
        return strcmp(profile, "fenics") == 0;
    }
    return 1;
}

static void mock_raise(const MockHostApi *host, const char *identifier,
                       const char *message)
{
    mock_host_error(host, identifier, message);
}

MOCK_EXPORT void mexFunction(int nlhs, mxArray *plhs[], int nrhs, const mxArray *prhs[])
{
    MockHostApi host;
    char error[MOCK_ERROR_MAX];
    char operation[32];
    double input = 0.0;
    double result = 0.0;

    error[0] = '\0';
    if (mock_host_load(&host, error, sizeof(error)) != 0) {
        mock_raise(&host, "reference_bridge:host", error);
        return;
    }
    if (nlhs != 1) {
        mock_raise(&host, "reference_bridge:arity", "reference bridge operations require exactly one output");
        return;
    }
    if (nrhs < 1) {
        mock_raise(&host, "reference_bridge:arity", "reference bridge requires an operation name");
        return;
    }
    if (mock_host_operation(&host, prhs[0], operation, sizeof(operation), error, sizeof(error)) != 0) {
        mock_raise(&host, "reference_bridge:type", error);
        return;
    }
    if (strcmp(operation, "scalar") != 0 && strcmp(operation, "numpy") != 0 &&
        strcmp(operation, "fenics") != 0 && strcmp(operation, "runtime") != 0) {
        mock_raise(&host, "reference_bridge:operation", "unknown reference bridge operation");
        return;
    }

    if (strcmp(operation, "scalar") == 0 || strcmp(operation, "numpy") == 0) {
        if (nrhs != 2) {
            mock_raise(&host, "reference_bridge:arity",
                       strcmp(operation, "scalar") == 0
                           ? "scalar requires exactly one value argument"
                           : "numpy requires exactly one value argument");
            return;
        }
        if (mock_host_scalar(&host, prhs[1], &input, error, sizeof(error)) != 0) {
            mock_raise(&host, "reference_bridge:type", error);
            return;
        }
        if (!isfinite(input)) {
            mock_raise(&host, "reference_bridge:range", "numeric argument must be finite");
            return;
        }
        if (strcmp(operation, "numpy") == 0 &&
            (input < 1.0 || input > 1000.0 || (double)(long)input != input)) {
            mock_raise(&host, "reference_bridge:range",
                       "numpy argument must be a positive integer double in [1, 1000]");
            return;
        }
    } else if (nrhs != 1) {
        mock_raise(&host, "reference_bridge:arity",
                   strcmp(operation, "fenics") == 0
                       ? "fenics takes no value arguments"
                       : "runtime takes no value arguments");
        return;
    }

    if (mock_package_load(error, sizeof(error)) != 0) {
        mock_raise(&host, "reference_bridge:runtime", error);
        return;
    }
    if (!mock_profile_supports(operation)) {
        snprintf(error, sizeof(error), "operation '%s' is unavailable in profile '%s'",
                 operation, mock_package_state.profile);
        error[sizeof(error) - 1] = '\0';
        mock_raise(&host, "reference_bridge:profile", error);
        return;
    }

    {
        int python_result = mock_python_initialize(mock_package_state.package_root,
                                                   mock_package_state.runtime_root,
                                                   mock_package_state.profile,
                                                   error, sizeof(error));
        if (mock_python_is_initialized() && !mock_mex_locked) {
            host.mexLock();
            mock_mex_locked = 1;
        }
        if (python_result != 0) {
            mock_raise(&host, "reference_bridge:python", error);
            return;
        }
    }

    if (strcmp(operation, "scalar") == 0) {
        if (mock_python_scalar(input, &result, error, sizeof(error)) != 0) {
            mock_raise(&host, "reference_bridge:python", error);
            return;
        }
        plhs[0] = host.mxCreateDoubleScalar(result);
        if (plhs[0] == NULL) {
            mock_raise(&host, "reference_bridge:host", "host could not allocate scalar result");
        }
        return;
    }

    if (strcmp(operation, "numpy") == 0) {
        if (mock_python_numpy((long)input, &result, error, sizeof(error)) != 0) {
            mock_raise(&host, "reference_bridge:python", error);
            return;
        }
        plhs[0] = host.mxCreateDoubleScalar(result);
        if (plhs[0] == NULL) {
            mock_raise(&host, "reference_bridge:host", "host could not allocate NumPy result");
        }
        return;
    }

    if (strcmp(operation, "fenics") == 0) {
        if (mock_python_fenics(&result, error, sizeof(error)) != 0) {
            mock_raise(&host, "reference_bridge:python", error);
            return;
        }
        if (!isfinite(result) || result > 5.0e-2) {
            mock_raise(&host, "reference_bridge:python",
                       "FEniCS backend returned an invalid Poisson L2 error");
            return;
        }
        plhs[0] = host.mxCreateDoubleScalar(result);
        if (plhs[0] == NULL) {
            mock_raise(&host, "reference_bridge:host", "host could not allocate FEniCS result");
        }
        return;
    }

    {
        MockRuntimeInfo info;
        if (mock_python_runtime(&info, error, sizeof(error)) != 0) {
            mock_raise(&host, "reference_bridge:python", error);
            return;
        }
        if (strcmp(info.python_version, "3.14.10") != 0 ||
            strcmp(info.python_abi, "cp314") != 0 ||
            !mock_path_equal(info.runtime_root, mock_package_state.runtime_root) ||
            strcmp(info.profile, mock_package_state.profile) != 0) {
            mock_raise(&host, "reference_bridge:runtime",
                       "backend runtime diagnostics differ from verified package identity");
            return;
        }
        plhs[0] = mock_host_runtime_struct(&host, info.python_version, info.python_abi,
                                           info.runtime_root, info.profile,
                                           error, sizeof(error));
        if (plhs[0] == NULL) {
            mock_raise(&host, "reference_bridge:host", error);
        }
    }
}
