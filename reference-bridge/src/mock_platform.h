#ifndef REFERENCE_BRIDGE_MOCK_PLATFORM_H
#define REFERENCE_BRIDGE_MOCK_PLATFORM_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define MOCK_PATH_MAX 4096
#define MOCK_ERROR_MAX 1024

typedef void *MockLibrary;

int mock_platform_module_directory(char *out, size_t out_size,
                                   char *error, size_t error_size);
int mock_platform_join(const char *left, const char *right,
                       char *out, size_t out_size,
                       char *error, size_t error_size);
int mock_platform_canonical_path(const char *path, char *out, size_t out_size,
                                 char *error, size_t error_size);
int mock_platform_is_file(const char *path);
int mock_platform_is_directory(const char *path);
int mock_platform_read_small_text(const char *path, char *out, size_t out_size,
                                  char *error, size_t error_size);

void *mock_platform_find_host_symbol(const char *name);
MockLibrary mock_platform_load_python(const char *path,
                                      char *error, size_t error_size);
void *mock_platform_library_symbol(MockLibrary library, const char *name);

#ifdef __cplusplus
}
#endif

#endif
