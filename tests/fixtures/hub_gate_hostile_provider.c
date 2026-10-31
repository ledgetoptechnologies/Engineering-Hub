#include <fcntl.h>
#include <unistd.h>

/* A test-only marker makes accidental provider loading observable. */
__attribute__((constructor)) static void mark_loaded(void)
{
    int fd = open("/tmp/hub-gate-hostile-provider-loaded", O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd >= 0) close(fd);
}
