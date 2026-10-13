/* Application-local compatibility for the Orange Pi Jammy Mali X11 driver.
 * Its advertised EXT platform path returns EGL_NO_SURFACE for Qt's window.
 * Keep the working eglGetDisplay/eglCreateWindowSurface X11 path instead.
 * No system libraries or global environment files are modified.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>

const char *eglQueryString(void *display, int name)
{
    typedef const char *(*query_function)(void *, int);
    query_function query = (query_function)dlsym(RTLD_NEXT, "eglQueryString");
    if (!query)
        return NULL;
    const char *original = query(display, name);
    /* EGL_EXTENSIONS on EGL_NO_DISPLAY: leave per-display data untouched. */
    if (display || name != 0x3055 || !original)
        return original;
    static _Thread_local char extensions[16384];
    if (strlen(original) >= sizeof extensions)
        return original;
    strcpy(extensions, original);
    const char token[] = "EGL_EXT_platform_x11";
    char *match = strstr(extensions, token);
    if (match && (match == extensions || match[-1] == ' ')
        && (match[sizeof token - 1] == ' ' || match[sizeof token - 1] == '\0'))
        memset(match, ' ', sizeof token - 1);
    return extensions;
}
