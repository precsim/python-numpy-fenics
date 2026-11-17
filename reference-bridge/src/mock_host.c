#include "mock_host.h"
#include "mock_platform.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>

static void mock_host_set_error(char *error, size_t error_size, const char *fmt, ...)
{
    va_list args;
    if (error == NULL || error_size == 0) {
        return;
    }
    va_start(args, fmt);
    vsnprintf(error, error_size, fmt, args);
    va_end(args);
    error[error_size - 1] = '\0';
}

#define MOCK_RESOLVE_HOST(host, field, type)                                        \
    do {                                                                            \
        void *mock_symbol__ = mock_platform_find_host_symbol(#field);               \
        if (mock_symbol__ == NULL) {                                                 \
            mock_host_set_error(error, error_size,                                  \
                                "active host does not export required symbol %s",  \
                                #field);                                             \
            return -1;                                                              \
        }                                                                           \
        (host)->field = (type)mock_symbol__;                                         \
    } while (0)

int mock_host_load(MockHostApi *host, char *error, size_t error_size)
{
    if (host == NULL) {
        mock_host_set_error(error, error_size, "invalid host API storage");
        return -1;
    }
    memset(host, 0, sizeof(*host));
    MOCK_RESOLVE_HOST(host, mexErrMsgIdAndTxt,
                      void (*)(const char *, const char *, ...));
    MOCK_RESOLVE_HOST(host, mxIsChar, bool (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxIsDouble, bool (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxIsComplex, bool (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxIsSparse, bool (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxGetNumberOfElements, size_t (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxGetM, size_t (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxGetN, size_t (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxGetScalar, double (*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxArrayToString, char *(*)(const mxArray *));
    MOCK_RESOLVE_HOST(host, mxFree, void (*)(void *));
    MOCK_RESOLVE_HOST(host, mxCreateDoubleScalar, mxArray *(*)(double));
    MOCK_RESOLVE_HOST(host, mxCreateString, mxArray *(*)(const char *));
    MOCK_RESOLVE_HOST(host, mxCreateStructMatrix,
                      mxArray *(*)(size_t, size_t, int, const char **));
    MOCK_RESOLVE_HOST(host, mxSetField,
                      void (*)(mxArray *, size_t, const char *, mxArray *));
    MOCK_RESOLVE_HOST(host, mxDestroyArray, void (*)(mxArray *));
    MOCK_RESOLVE_HOST(host, mexLock, void (*)(void));
    return 0;
}

int mock_host_operation(const MockHostApi *host, const mxArray *array,
                        char *out, size_t out_size,
                        char *error, size_t error_size)
{
    char *text;
    size_t length;
    if (host == NULL || array == NULL || out == NULL || out_size == 0) {
        mock_host_set_error(error, error_size, "invalid operation argument");
        return -1;
    }
    if (!host->mxIsChar(array) || host->mxGetNumberOfElements(array) == 0 ||
        host->mxGetNumberOfElements(array) > 31 ||
        (host->mxGetM(array) != 1 && host->mxGetN(array) != 1)) {
        mock_host_set_error(error, error_size,
                            "operation must be a non-empty character vector of at most 31 characters");
        return -1;
    }
    text = host->mxArrayToString(array);
    if (text == NULL) {
        mock_host_set_error(error, error_size, "host could not convert operation to text");
        return -1;
    }
    length = strlen(text);
    if (length + 1 > out_size) {
        host->mxFree(text);
        mock_host_set_error(error, error_size, "operation name is too long");
        return -1;
    }
    memcpy(out, text, length + 1);
    host->mxFree(text);
    return 0;
}

int mock_host_scalar(const MockHostApi *host, const mxArray *array, double *out,
                     char *error, size_t error_size)
{
    if (host == NULL || array == NULL || out == NULL) {
        mock_host_set_error(error, error_size, "invalid scalar argument");
        return -1;
    }
    if (!host->mxIsDouble(array) || host->mxIsComplex(array) || host->mxIsSparse(array) ||
        host->mxGetM(array) != 1 || host->mxGetN(array) != 1 ||
        host->mxGetNumberOfElements(array) != 1) {
        mock_host_set_error(error, error_size,
                            "argument must be a real, non-complex, non-sparse 1x1 double");
        return -1;
    }
    *out = host->mxGetScalar(array);
    return 0;
}

mxArray *mock_host_runtime_struct(const MockHostApi *host,
                                  const char *python_version,
                                  const char *python_abi,
                                  const char *runtime_root,
                                  const char *profile,
                                  char *error, size_t error_size)
{
    static const char *fields[] = {
        "python_version", "python_abi", "runtime_root", "profile"
    };
    const char *values[] = {python_version, python_abi, runtime_root, profile};
    mxArray *result;
    int index;

    if (host == NULL || python_version == NULL || python_abi == NULL ||
        runtime_root == NULL || profile == NULL) {
        mock_host_set_error(error, error_size, "invalid runtime diagnostics");
        return NULL;
    }
    result = host->mxCreateStructMatrix(1, 1, 4, fields);
    if (result == NULL) {
        mock_host_set_error(error, error_size, "host could not allocate runtime diagnostics struct");
        return NULL;
    }
    for (index = 0; index < 4; ++index) {
        mxArray *value = host->mxCreateString(values[index]);
        if (value == NULL) {
            host->mxDestroyArray(result);
            mock_host_set_error(error, error_size,
                                "host could not allocate runtime diagnostics field %s", fields[index]);
            return NULL;
        }
        host->mxSetField(result, 0, fields[index], value);
    }
    return result;
}

void mock_host_error(const MockHostApi *host, const char *identifier,
                     const char *message)
{
    if (host != NULL && host->mexErrMsgIdAndTxt != NULL) {
        host->mexErrMsgIdAndTxt(identifier, "%s", message != NULL ? message : "reference bridge error");
    }
}
