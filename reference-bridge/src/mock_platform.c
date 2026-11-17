#define _GNU_SOURCE 1

#include "mock_platform.h"

#include <errno.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#else
#include <dlfcn.h>
#include <limits.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __linux__
#include <dirent.h>
#endif
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#endif

static void mock_set_error(char *error, size_t error_size, const char *fmt, ...)
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

static int mock_copy_string(const char *text, char *out, size_t out_size,
                            char *error, size_t error_size)
{
    size_t length;
    if (text == NULL || out == NULL || out_size == 0) {
        mock_set_error(error, error_size, "invalid path buffer");
        return -1;
    }
    length = strlen(text);
    if (length + 1 > out_size) {
        mock_set_error(error, error_size, "path exceeds %lu bytes",
                       (unsigned long)(out_size - 1));
        return -1;
    }
    memcpy(out, text, length + 1);
    return 0;
}

#ifdef _WIN32
static int mock_utf8_to_wide(const char *text, wchar_t *out, size_t out_count,
                             char *error, size_t error_size)
{
    int needed;
    if (text == NULL || out == NULL || out_count == 0) {
        mock_set_error(error, error_size, "invalid UTF-8 path buffer");
        return -1;
    }
    needed = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text, -1,
                                 out, (int)out_count);
    if (needed == 0) {
        mock_set_error(error, error_size, "cannot decode UTF-8 path (win32=%lu)",
                       (unsigned long)GetLastError());
        return -1;
    }
    return 0;
}

static int mock_wide_to_utf8(const wchar_t *text, char *out, size_t out_size,
                             char *error, size_t error_size)
{
    int needed;
    if (text == NULL || out == NULL || out_size == 0) {
        mock_set_error(error, error_size, "invalid wide path buffer");
        return -1;
    }
    needed = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, text, -1,
                                 out, (int)out_size, NULL, NULL);
    if (needed == 0) {
        mock_set_error(error, error_size, "cannot encode Windows path as UTF-8 (win32=%lu)",
                       (unsigned long)GetLastError());
        return -1;
    }
    return 0;
}

static int mock_windows_is_python_provider(const wchar_t *name)
{
    size_t length;
    if (name == NULL || _wcsnicmp(name, L"python3", 7) != 0) {
        return 0;
    }
    length = wcslen(name);
    return length > 4 && _wcsicmp(name + length - 4, L".dll") == 0;
}

static int mock_windows_module_path(HMODULE module, char *out, size_t out_size,
                                    char *error, size_t error_size)
{
    wchar_t wide_path[MOCK_PATH_MAX];
    DWORD length = GetModuleFileNameW(module, wide_path, (DWORD)(sizeof(wide_path) / sizeof(wide_path[0])));
    if (length == 0 || length >= sizeof(wide_path) / sizeof(wide_path[0])) {
        mock_set_error(error, error_size, "cannot resolve loaded module path (win32=%lu)",
                       (unsigned long)GetLastError());
        return -1;
    }
    return mock_wide_to_utf8(wide_path, out, out_size, error, error_size);
}
#endif

int mock_platform_canonical_path(const char *path, char *out, size_t out_size,
                                 char *error, size_t error_size)
{
#ifdef _WIN32
    wchar_t wide_path[MOCK_PATH_MAX];
    wchar_t final_path[MOCK_PATH_MAX];
    HANDLE handle;
    DWORD length;
    const wchar_t *start;

    if (mock_utf8_to_wide(path, wide_path, sizeof(wide_path) / sizeof(wide_path[0]),
                          error, error_size) != 0) {
        return -1;
    }
    handle = CreateFileW(wide_path, 0,
                         FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                         NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
    if (handle == INVALID_HANDLE_VALUE) {
        mock_set_error(error, error_size, "path does not exist: %s (win32=%lu)",
                       path, (unsigned long)GetLastError());
        return -1;
    }
    length = GetFinalPathNameByHandleW(handle, final_path,
                                       (DWORD)(sizeof(final_path) / sizeof(final_path[0])),
                                       FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
    CloseHandle(handle);
    if (length == 0 || length >= sizeof(final_path) / sizeof(final_path[0])) {
        mock_set_error(error, error_size, "cannot canonicalize path: %s (win32=%lu)",
                       path, (unsigned long)GetLastError());
        return -1;
    }
    start = final_path;
    if (wcsncmp(start, L"\\\\?\\", 4) == 0) {
        start += 4;
    }
    return mock_wide_to_utf8(start, out, out_size, error, error_size);
#else
    char resolved[MOCK_PATH_MAX];
    if (realpath(path, resolved) == NULL) {
        mock_set_error(error, error_size, "path does not exist: %s (%s)",
                       path, strerror(errno));
        return -1;
    }
    return mock_copy_string(resolved, out, out_size, error, error_size);
#endif
}

int mock_platform_module_directory(char *out, size_t out_size,
                                   char *error, size_t error_size)
{
    char module_path[MOCK_PATH_MAX];
    char canonical[MOCK_PATH_MAX];
    char *slash;
#ifdef _WIN32
    HMODULE module = NULL;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                            GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                            (LPCWSTR)(uintptr_t)&mock_platform_module_directory,
                            &module)) {
        mock_set_error(error, error_size, "cannot identify bridge module (win32=%lu)",
                       (unsigned long)GetLastError());
        return -1;
    }
    if (mock_windows_module_path(module, module_path, sizeof(module_path), error, error_size) != 0) {
        return -1;
    }
#else
    Dl_info info;
    if (dladdr((void *)(uintptr_t)&mock_platform_module_directory, &info) == 0 ||
        info.dli_fname == NULL) {
        mock_set_error(error, error_size, "cannot identify bridge module with dladdr");
        return -1;
    }
    if (mock_copy_string(info.dli_fname, module_path, sizeof(module_path), error, error_size) != 0) {
        return -1;
    }
#endif
    if (mock_platform_canonical_path(module_path, canonical, sizeof(canonical), error, error_size) != 0) {
        return -1;
    }
    slash = strrchr(canonical, '/');
#ifdef _WIN32
    {
        char *backslash = strrchr(canonical, '\\');
        if (backslash != NULL && (slash == NULL || backslash > slash)) {
            slash = backslash;
        }
    }
#endif
    if (slash == NULL || slash == canonical) {
        mock_set_error(error, error_size, "bridge module has no package directory: %s", canonical);
        return -1;
    }
    *slash = '\0';
    return mock_copy_string(canonical, out, out_size, error, error_size);
}

int mock_platform_join(const char *left, const char *right,
                       char *out, size_t out_size,
                       char *error, size_t error_size)
{
    size_t left_len;
    size_t right_len;
    char separator;
    int need_separator;
    if (left == NULL || right == NULL || out == NULL || out_size == 0 || right[0] == '\0') {
        mock_set_error(error, error_size, "invalid path join input");
        return -1;
    }
    left_len = strlen(left);
    right_len = strlen(right);
#ifdef _WIN32
    separator = '\\';
#else
    separator = '/';
#endif
    need_separator = left_len > 0 && left[left_len - 1] != '/' && left[left_len - 1] != '\\';
    if (left_len + (size_t)need_separator + right_len + 1 > out_size) {
        mock_set_error(error, error_size, "joined path exceeds %lu bytes",
                       (unsigned long)(out_size - 1));
        return -1;
    }
    memcpy(out, left, left_len);
    if (need_separator) {
        out[left_len++] = separator;
    }
    memcpy(out + left_len, right, right_len + 1);
    return 0;
}

int mock_platform_is_file(const char *path)
{
#ifdef _WIN32
    wchar_t wide_path[MOCK_PATH_MAX];
    DWORD attrs;
    if (mock_utf8_to_wide(path, wide_path, sizeof(wide_path) / sizeof(wide_path[0]), NULL, 0) != 0) {
        return 0;
    }
    attrs = GetFileAttributesW(wide_path);
    return attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY);
#else
    struct stat st;
    return stat(path, &st) == 0 && S_ISREG(st.st_mode);
#endif
}

int mock_platform_is_directory(const char *path)
{
#ifdef _WIN32
    wchar_t wide_path[MOCK_PATH_MAX];
    DWORD attrs;
    if (mock_utf8_to_wide(path, wide_path, sizeof(wide_path) / sizeof(wide_path[0]), NULL, 0) != 0) {
        return 0;
    }
    attrs = GetFileAttributesW(wide_path);
    return attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY) != 0;
#else
    struct stat st;
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
#endif
}

int mock_platform_read_small_text(const char *path, char *out, size_t out_size,
                                  char *error, size_t error_size)
{
    FILE *stream;
    size_t used;
    int extra;
    if (out == NULL || out_size < 2) {
        mock_set_error(error, error_size, "invalid text buffer");
        return -1;
    }
#ifdef _WIN32
    {
        wchar_t wide_path[MOCK_PATH_MAX];
        if (mock_utf8_to_wide(path, wide_path, sizeof(wide_path) / sizeof(wide_path[0]),
                              error, error_size) != 0) {
            return -1;
        }
        stream = _wfopen(wide_path, L"rb");
    }
#else
    stream = fopen(path, "rb");
#endif
    if (stream == NULL) {
        mock_set_error(error, error_size, "cannot read %s", path);
        return -1;
    }
    used = fread(out, 1, out_size - 1, stream);
    extra = fgetc(stream);
    fclose(stream);
    if (extra != EOF) {
        mock_set_error(error, error_size, "text file is unexpectedly large: %s", path);
        return -1;
    }
    out[used] = '\0';
    while (used > 0 && (out[used - 1] == '\n' || out[used - 1] == '\r' ||
                        out[used - 1] == ' ' || out[used - 1] == '\t')) {
        out[--used] = '\0';
    }
    return 0;
}

void *mock_platform_find_host_symbol(const char *name)
{
#ifdef _WIN32
    HANDLE snapshot;
    MODULEENTRY32W entry;
    FARPROC proc;
    HMODULE executable = GetModuleHandleW(NULL);
    if (executable != NULL) {
        proc = GetProcAddress(executable, name);
        if (proc != NULL) {
            return (void *)(uintptr_t)proc;
        }
    }
    snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                       GetCurrentProcessId());
    if (snapshot == INVALID_HANDLE_VALUE) {
        return NULL;
    }
    memset(&entry, 0, sizeof(entry));
    entry.dwSize = sizeof(entry);
    if (Module32FirstW(snapshot, &entry)) {
        do {
            proc = GetProcAddress(entry.hModule, name);
            if (proc != NULL) {
                CloseHandle(snapshot);
                return (void *)(uintptr_t)proc;
            }
        } while (Module32NextW(snapshot, &entry));
    }
    CloseHandle(snapshot);
    return NULL;
#else
    return dlsym(RTLD_DEFAULT, name);
#endif
}

static int mock_python_collision(const char *expected,
                                 char *error, size_t error_size)
{
    char expected_canonical[MOCK_PATH_MAX];
    if (mock_platform_canonical_path(expected, expected_canonical, sizeof(expected_canonical),
                                     error, error_size) != 0) {
        return -1;
    }

#ifdef _WIN32
    {
        char expected_parent[MOCK_PATH_MAX];
        char *separator;
        HANDLE snapshot;
        MODULEENTRY32W entry;
        if (mock_copy_string(expected_canonical, expected_parent, sizeof(expected_parent),
                             error, error_size) != 0) {
            return -1;
        }
        separator = strrchr(expected_parent, '\\');
        if (separator == NULL) {
            separator = strrchr(expected_parent, '/');
        }
        if (separator == NULL) {
            mock_set_error(error, error_size, "CPython provider has no parent directory: %s",
                           expected_canonical);
            return -1;
        }
        *separator = '\0';
        snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32,
                                           GetCurrentProcessId());
        if (snapshot == INVALID_HANDLE_VALUE) {
            mock_set_error(error, error_size, "cannot inspect loaded CPython providers (win32=%lu)",
                           (unsigned long)GetLastError());
            return -1;
        }
        memset(&entry, 0, sizeof(entry));
        entry.dwSize = sizeof(entry);
        if (Module32FirstW(snapshot, &entry)) {
            do {
                if (mock_windows_is_python_provider(entry.szModule)) {
                    char loaded[MOCK_PATH_MAX];
                    char loaded_canonical[MOCK_PATH_MAX];
                    char loaded_parent[MOCK_PATH_MAX];
                    char *loaded_separator;
                    if (mock_windows_module_path(entry.hModule, loaded, sizeof(loaded), error, error_size) != 0 ||
                        mock_platform_canonical_path(loaded, loaded_canonical, sizeof(loaded_canonical),
                                                     error, error_size) != 0) {
                        CloseHandle(snapshot);
                        return -1;
                    }
                    if (mock_copy_string(loaded_canonical, loaded_parent, sizeof(loaded_parent),
                                         error, error_size) != 0) {
                        CloseHandle(snapshot);
                        return -1;
                    }
                    loaded_separator = strrchr(loaded_parent, '\\');
                    if (loaded_separator == NULL) {
                        loaded_separator = strrchr(loaded_parent, '/');
                    }
                    if (loaded_separator == NULL) {
                        CloseHandle(snapshot);
                        mock_set_error(error, error_size,
                                       "loaded CPython provider has no parent directory: %s",
                                       loaded_canonical);
                        return -1;
                    }
                    *loaded_separator = '\0';
                    if (_stricmp(loaded_parent, expected_parent) != 0) {
                        mock_set_error(error, error_size,
                                       "conflicting CPython provider already loaded: %s", loaded_canonical);
                        CloseHandle(snapshot);
                        return -1;
                    }
                }
            } while (Module32NextW(snapshot, &entry));
        }
        CloseHandle(snapshot);
    }
#elif defined(__APPLE__)
    {
        uint32_t count = _dyld_image_count();
        uint32_t index;
        for (index = 0; index < count; ++index) {
            const char *image = _dyld_get_image_name(index);
            char loaded[MOCK_PATH_MAX];
            if (image == NULL ||
                (strstr(image, "Python.framework/Versions/") == NULL &&
                 strstr(image, "libpython3.") == NULL)) {
                continue;
            }
            if (mock_platform_canonical_path(image, loaded, sizeof(loaded), NULL, 0) == 0 &&
                strcmp(loaded, expected_canonical) != 0) {
                mock_set_error(error, error_size,
                               "conflicting CPython provider already loaded: %s", loaded);
                return -1;
            }
        }
    }
#else
    {
        FILE *maps = fopen("/proc/self/maps", "rb");
        if (maps != NULL) {
            char line[MOCK_PATH_MAX + 256];
            while (fgets(line, sizeof(line), maps) != NULL) {
                char *path = strchr(line, '/');
                char *newline;
                char loaded[MOCK_PATH_MAX];
                if (path == NULL || strstr(path, "/libpython3.") == NULL) {
                    continue;
                }
                newline = strpbrk(path, "\r\n");
                if (newline != NULL) {
                    *newline = '\0';
                }
                if (mock_platform_canonical_path(path, loaded, sizeof(loaded), NULL, 0) == 0 &&
                    strcmp(loaded, expected_canonical) != 0) {
                    fclose(maps);
                    mock_set_error(error, error_size,
                                   "conflicting CPython provider already loaded: %s", loaded);
                    return -1;
                }
            }
            fclose(maps);
        }
    }
#endif
    return 0;
}

#ifdef __linux__
#define MOCK_LINUX_NUMPY_PROVIDER "libopenblasbridge_np252_.so.0"
#define MOCK_LINUX_NUMPY_IDENTITY_SYMBOL "openblas_get_corenamebridge_np252_"

static void *mock_linux_numpy_provider;
static char mock_linux_numpy_provider_path[MOCK_PATH_MAX];

static int mock_linux_private_provider_loaded(const char *expected,
                                              char *error, size_t error_size)
{
    FILE *maps = fopen("/proc/self/maps", "rb");
    char line[MOCK_PATH_MAX + 256];

    if (maps == NULL) {
        mock_set_error(error, error_size,
                       "cannot inspect loaded private NumPy providers: %s",
                       strerror(errno));
        return -1;
    }
    while (fgets(line, sizeof(line), maps) != NULL) {
        char *path = strchr(line, '/');
        char *newline;
        char *basename;
        char loaded[MOCK_PATH_MAX];
        if (path == NULL) {
            continue;
        }
        newline = strpbrk(path, "\r\n");
        if (newline != NULL) {
            *newline = '\0';
        }
        basename = strrchr(path, '/');
        basename = basename != NULL ? basename + 1 : path;
        if (strcmp(basename, MOCK_LINUX_NUMPY_PROVIDER) != 0) {
            continue;
        }
        if (mock_platform_canonical_path(path, loaded, sizeof(loaded), NULL, 0) != 0) {
            fclose(maps);
            mock_set_error(error, error_size,
                           "cannot identify loaded private NumPy provider: %s", path);
            return -1;
        }
        fclose(maps);
        mock_set_error(error, error_size,
                       "private NumPy provider was loaded before bridge isolation: %s%s",
                       loaded,
                       strcmp(loaded, expected) == 0 ? "" : " (foreign path)");
        return 1;
    }
    fclose(maps);
    return 0;
}

static int mock_linux_preload_numpy_provider(const char *python_provider,
                                             char *error, size_t error_size)
{
    char runtime_lib[MOCK_PATH_MAX];
    char private_root[MOCK_PATH_MAX];
    char private_root_canonical[MOCK_PATH_MAX];
    char provider[MOCK_PATH_MAX];
    char provider_canonical[MOCK_PATH_MAX];
    char expected_canonical[MOCK_PATH_MAX];
    char *separator;
    DIR *directory;
    struct dirent *entry;
    struct stat st;
    int matching_entries = 0;
    int loaded;
    void *identity;
    Dl_info info;
    char identity_path[MOCK_PATH_MAX];

    if (mock_copy_string(python_provider, runtime_lib, sizeof(runtime_lib),
                         error, error_size) != 0) {
        return -1;
    }
    separator = strrchr(runtime_lib, '/');
    if (separator == NULL || separator == runtime_lib) {
        mock_set_error(error, error_size,
                       "CPython provider has no runtime lib directory: %s",
                       python_provider);
        return -1;
    }
    *separator = '\0';
    if (mock_platform_join(runtime_lib, "numpy-private", private_root,
                           sizeof(private_root), error, error_size) != 0) {
        return -1;
    }
    if (lstat(private_root, &st) != 0) {
        if (errno == ENOENT) {
            return 0;
        }
        mock_set_error(error, error_size,
                       "cannot inspect private NumPy provider directory %s: %s",
                       private_root, strerror(errno));
        return -1;
    }
    if (!S_ISDIR(st.st_mode)) {
        mock_set_error(error, error_size,
                       "private NumPy provider root must be a real directory: %s",
                       private_root);
        return -1;
    }
    if (mock_platform_canonical_path(private_root, private_root_canonical,
                                     sizeof(private_root_canonical),
                                     error, error_size) != 0 ||
        mock_platform_join(private_root_canonical, MOCK_LINUX_NUMPY_PROVIDER,
                           expected_canonical, sizeof(expected_canonical),
                           error, error_size) != 0) {
        return -1;
    }

    directory = opendir(private_root_canonical);
    if (directory == NULL) {
        mock_set_error(error, error_size,
                       "cannot inspect private NumPy provider directory %s: %s",
                       private_root_canonical, strerror(errno));
        return -1;
    }
    while ((entry = readdir(directory)) != NULL) {
        if (strncmp(entry->d_name, "libopenblas", 11) != 0) {
            continue;
        }
        matching_entries += 1;
        if (strcmp(entry->d_name, MOCK_LINUX_NUMPY_PROVIDER) != 0) {
            closedir(directory);
            mock_set_error(error, error_size,
                           "unexpected private NumPy OpenBLAS provider: %s/%s",
                           private_root_canonical, entry->d_name);
            return -1;
        }
    }
    closedir(directory);
    if (matching_entries != 1) {
        mock_set_error(error, error_size,
                       "expected exactly one pinned private NumPy OpenBLAS provider in %s",
                       private_root_canonical);
        return -1;
    }
    if (mock_platform_join(private_root_canonical, MOCK_LINUX_NUMPY_PROVIDER,
                           provider, sizeof(provider), error, error_size) != 0) {
        return -1;
    }
    if (lstat(provider, &st) != 0 || !S_ISREG(st.st_mode)) {
        mock_set_error(error, error_size,
                       "private NumPy provider must be a regular non-symlink file: %s",
                       provider);
        return -1;
    }
    if (mock_platform_canonical_path(provider, provider_canonical,
                                     sizeof(provider_canonical),
                                     error, error_size) != 0 ||
        strcmp(provider_canonical, expected_canonical) != 0) {
        mock_set_error(error, error_size,
                       "private NumPy provider escaped its pinned runtime path: %s",
                       provider);
        return -1;
    }

    if (mock_linux_numpy_provider != NULL) {
        if (strcmp(mock_linux_numpy_provider_path, provider_canonical) != 0) {
            mock_set_error(error, error_size,
                           "bridge private NumPy provider identity changed: %s != %s",
                           mock_linux_numpy_provider_path, provider_canonical);
            return -1;
        }
        return 0;
    }
    loaded = mock_linux_private_provider_loaded(provider_canonical, error, error_size);
    if (loaded != 0) {
        return -1;
    }
    if (dlsym(RTLD_DEFAULT, MOCK_LINUX_NUMPY_IDENTITY_SYMBOL) != NULL) {
        mock_set_error(error, error_size,
                       "private NumPy OpenBLAS identity symbol is already globally visible: %s",
                       MOCK_LINUX_NUMPY_IDENTITY_SYMBOL);
        return -1;
    }

    mock_linux_numpy_provider =
        dlopen(provider_canonical, RTLD_NOW | RTLD_LOCAL | RTLD_DEEPBIND);
    if (mock_linux_numpy_provider == NULL) {
        const char *message = dlerror();
        mock_set_error(error, error_size,
                       "cannot isolate private NumPy provider %s: %s",
                       provider_canonical,
                       message != NULL ? message : "unknown loader error");
        return -1;
    }
    identity = dlsym(mock_linux_numpy_provider, MOCK_LINUX_NUMPY_IDENTITY_SYMBOL);
    if (identity == NULL ||
        dladdr(identity, &info) == 0 ||
        info.dli_fname == NULL ||
        mock_platform_canonical_path(info.dli_fname, identity_path,
                                     sizeof(identity_path), error, error_size) != 0 ||
        strcmp(identity_path, provider_canonical) != 0) {
        dlclose(mock_linux_numpy_provider);
        mock_linux_numpy_provider = NULL;
        mock_set_error(error, error_size,
                       "private NumPy provider identity validation failed: %s",
                       provider_canonical);
        return -1;
    }
    if (mock_copy_string(provider_canonical, mock_linux_numpy_provider_path,
                         sizeof(mock_linux_numpy_provider_path),
                         error, error_size) != 0) {
        dlclose(mock_linux_numpy_provider);
        mock_linux_numpy_provider = NULL;
        return -1;
    }
    return 0;
}
#endif

MockLibrary mock_platform_load_python(const char *path,
                                      char *error, size_t error_size)
{
    if (!mock_platform_is_file(path)) {
        mock_set_error(error, error_size, "CPython provider is missing: %s", path);
        return NULL;
    }
    if (mock_python_collision(path, error, error_size) != 0) {
        return NULL;
    }
#ifdef __linux__
    if (mock_linux_preload_numpy_provider(path, error, error_size) != 0) {
        return NULL;
    }
#endif
#ifdef _WIN32
    {
        wchar_t wide_path[MOCK_PATH_MAX];
        HMODULE module;
        if (mock_utf8_to_wide(path, wide_path, sizeof(wide_path) / sizeof(wide_path[0]),
                              error, error_size) != 0) {
            return NULL;
        }
        module = LoadLibraryExW(wide_path, NULL,
                                LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR |
                                LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (module == NULL) {
            mock_set_error(error, error_size, "cannot load CPython provider %s (win32=%lu)",
                           path, (unsigned long)GetLastError());
            return NULL;
        }
        return (MockLibrary)module;
    }
#else
    {
        void *library = dlopen(path, RTLD_NOW | RTLD_GLOBAL);
        if (library == NULL) {
            const char *message = dlerror();
            mock_set_error(error, error_size, "cannot load CPython provider %s: %s",
                           path, message != NULL ? message : "unknown loader error");
            return NULL;
        }
        return library;
    }
#endif
}

void *mock_platform_library_symbol(MockLibrary library, const char *name)
{
    if (library == NULL || name == NULL) {
        return NULL;
    }
#ifdef _WIN32
    return (void *)(uintptr_t)GetProcAddress((HMODULE)library, name);
#else
    return dlsym(library, name);
#endif
}
