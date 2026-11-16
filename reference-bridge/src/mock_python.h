#ifndef REFERENCE_BRIDGE_MOCK_PYTHON_H
#define REFERENCE_BRIDGE_MOCK_PYTHON_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct MockRuntimeInfo {
    char python_version[64];
    char python_abi[32];
    char runtime_root[4096];
    char profile[32];
} MockRuntimeInfo;

int mock_python_initialize(const char *package_root, const char *runtime_root,
                           const char *profile,
                           char *error, size_t error_size);
int mock_python_is_initialized(void);
int mock_python_scalar(double value, double *result,
                       char *error, size_t error_size);
int mock_python_numpy(long value, double *result,
                      char *error, size_t error_size);
int mock_python_fenics(double *result,
                       char *error, size_t error_size);
int mock_python_runtime(MockRuntimeInfo *info,
                        char *error, size_t error_size);

#ifdef __cplusplus
}
#endif

#endif
