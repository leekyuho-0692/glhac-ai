/* Keep this signed process alive as the launchd job's TCC identity. */
#include <errno.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#include <glhac_offsite_script.h>

extern char **environ;

static int fail(const char *operation, int error)
{
    fprintf(stderr, "glhac-offsite-backup: %s: %s\n", operation, strerror(error));
    return 2;
}

static int allowed_environment(const char *entry)
{
    const char *equals = strchr(entry, '=');
    if (equals == NULL)
        return 0;
    size_t length = (size_t)(equals - entry);
    const char prefix[] = "GLHAC_BACKUP_";
    if (length >= sizeof(prefix) - 1 &&
        memcmp(entry, prefix, sizeof(prefix) - 1) == 0)
        return 1;
    const char *names[] = {"HOME", "TZ", "LANG", "LC_ALL", "USER", "LOGNAME", "TMPDIR"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        if (length == strlen(names[i]) && memcmp(entry, names[i], length) == 0)
            return 1;
    }
    return 0;
}

static char **child_environment(void)
{
    size_t count = 0;
    for (char **entry = environ; *entry != NULL; ++entry)
        if (allowed_environment(*entry))
            ++count;
    char **environment = calloc(count + 2, sizeof(*environment));
    if (environment == NULL)
        return NULL;
    size_t index = 0;
    environment[index++] = "PATH=/usr/bin:/bin:/usr/sbin:/sbin";
    for (char **entry = environ; *entry != NULL; ++entry)
        if (allowed_environment(*entry))
            environment[index++] = *entry; /* Preserve values, including newlines. */
    return environment;
}

static int fits_arguments(size_t *remaining, char *const entries[])
{
    for (size_t i = 0; entries[i] != NULL; ++i) {
        size_t size = strlen(entries[i]) + 1 + sizeof(char *);
        if (size > *remaining)
            return 0;
        *remaining -= size;
    }
    return 1;
}

static void child_changed(int signum)
{
    (void)signum;
}

static int run_script(char **environment)
{
    char *arguments[] = {"/bin/bash", "--noprofile", "--norc", "-c",
                         (char *)glhac_offsite_script, "glhac-offsite-backup", NULL};
    long arg_max = sysconf(_SC_ARG_MAX);
    if (arg_max <= 4096)
        return fail("ARG_MAX", E2BIG);
    /* Include argv/envp strings and pointers, with room for OS bookkeeping. */
    size_t remaining = (size_t)arg_max - 4096;
    if (!fits_arguments(&remaining, arguments) ||
        !fits_arguments(&remaining, environment))
        return fail("embedded script and environment exceed ARG_MAX", E2BIG);

    sigset_t signals, child_mask;
    sigemptyset(&signals);
    sigaddset(&signals, SIGTERM);
    sigaddset(&signals, SIGINT);
    sigaddset(&signals, SIGHUP);
    sigaddset(&signals, SIGCHLD);
    /* Block before spawning so an early stop cannot miss the child PID. */
    if (sigprocmask(SIG_BLOCK, &signals, &child_mask) == -1)
        return fail("sigprocmask", errno);

    struct sigaction action = {0};
    action.sa_handler = SIG_DFL;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) == -1 ||
        sigaction(SIGINT, &action, NULL) == -1 ||
        sigaction(SIGHUP, &action, NULL) == -1)
        return fail("sigaction", errno);
    /* Darwin discards default-ignored SIGCHLD even while blocked. Register a
     * handler so sigwait can consume it; it stays blocked in this process. */
    action.sa_handler = child_changed;
    if (sigaction(SIGCHLD, &action, NULL) == -1)
        return fail("sigaction", errno);

    /* The child must receive stop signals, even if our caller blocked them. */
    sigdelset(&child_mask, SIGTERM);
    sigdelset(&child_mask, SIGINT);
    sigdelset(&child_mask, SIGHUP);
    sigdelset(&child_mask, SIGCHLD);
    posix_spawnattr_t attributes;
    int error = posix_spawnattr_init(&attributes);
    if (error != 0)
        return fail("posix_spawnattr_init", error);
    error = posix_spawnattr_setsigmask(&attributes, &child_mask);
    if (error == 0)
        error = posix_spawnattr_setsigdefault(&attributes, &signals);
    if (error == 0)
        error = posix_spawnattr_setflags(&attributes,
                                       POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    pid_t child = -1;
    if (error == 0)
        error = posix_spawn(&child, "/bin/bash", NULL, &attributes, arguments, environment);
    posix_spawnattr_destroy(&attributes);
    if (error != 0)
        return fail("posix_spawn /bin/bash", error);

    int status;
    const char *operation;
    for (;;) {
        pid_t result = waitpid(child, &status, WNOHANG);
        if (result == child)
            break;
        if (result == -1) {
            if (errno == EINTR)
                continue;
            operation = "waitpid";
            error = errno;
            goto child_failure;
        }
        /* SIGCHLD wakes this wait even if the child exited just above.
         * Synchronous handling also avoids forwarding to a reaped/reused PID. */
        int received;
        error = sigwait(&signals, &received);
        if (error != 0) {
            operation = "sigwait";
            goto child_failure;
        }
        if (received != SIGCHLD && kill(child, received) == -1 && errno != ESRCH) {
            operation = "forward signal";
            error = errno;
            goto child_failure;
        }
    }
    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    if (WIFSIGNALED(status))
        return 128 + WTERMSIG(status);
    return fail("unexpected child status", ECHILD);

child_failure:
    kill(child, SIGKILL);
    while (waitpid(child, &status, 0) == -1 && errno == EINTR)
        ;
    return fail(operation, error);
}

int main(int argc, char **argv)
{
    if (argc != 1) {
        fprintf(stderr, "Usage: %s (no arguments)\n", argv[0]);
        return 2;
    }
    char **environment = child_environment();
    if (environment == NULL)
        return fail("calloc environment", errno);
    int result = run_script(environment);
    free(environment);
    return result;
}
