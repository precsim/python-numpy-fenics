#ifndef REFERENCE_BRIDGE_MOCK_HOST_H
#define REFERENCE_BRIDGE_MOCK_HOST_H

#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct mxArray_tag mxArray;

typedef struct MockHostApi MockHostApi;

struct MockHostApi {
    bool (*mxIsChar)(const mxArray *array);
    bool (*mxIsDouble)(const mxArray *array);
    bool (*mxIsComplex)(const mxArray *array);
    bool (*mxIsSparse)(const mxArray *array);
    size_t (*mxGetNumberOfElements)(const mxArray *array);
    size_t (*mxGetM)(const mxArray *array);
    size_t (*mxGetN)(const mxArray *array);
    double (*mxGetScalar)(const mxArray *array);
    char *(*mxArrayToString)(const mxArray *array);
    void (*mxFree)(void *memory);
    mxArray *(*mxCreateDoubleScalar)(double value);
    mxArray *(*mxCreateString)(const char *value);
    mxArray *(*mxCreateStructMatrix)(size_t m, size_t n, int nfields,
                                     const char **field_names);
    void (*mxSetField)(mxArray *array, size_t index, const char *field_name,
                       mxArray *value);
    void (*mxDestroyArray)(mxArray *array);
    void (*mexLock)(void);
    void (*mexErrMsgIdAndTxt)(const char *identifier, const char *format, ...);
};

int mock_host_load(MockHostApi *host, char *error, size_t error_size);
int mock_host_operation(const MockHostApi *host, const mxArray *array,
                        char *out, size_t out_size,
                        char *error, size_t error_size);
int mock_host_scalar(const MockHostApi *host, const mxArray *array, double *out,
                     char *error, size_t error_size);
mxArray *mock_host_runtime_struct(const MockHostApi *host,
                                  const char *python_version,
                                  const char *python_abi,
                                  const char *runtime_root,
                                  const char *profile,
                                  char *error, size_t error_size);
void mock_host_error(const MockHostApi *host, const char *identifier,
                     const char *message);

#ifdef __cplusplus
}
#endif

#endif
