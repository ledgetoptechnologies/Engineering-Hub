#define _GNU_SOURCE
#include "decision.h"
#include "enrollment.h"
#include "wire.h"
#include <errno.h>
#include <fcntl.h>
#include <openssl/evp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <time.h>
#include <unistd.h>
#include <linux/close_range.h>

#ifndef GATE_PUBLIC_KEY_HEX
#error GATE_PUBLIC_KEY_HEX must be an independently enrolled 64-character lowercase public key
#endif
#ifndef GATE_CHANNEL_VERSION
#define GATE_CHANNEL_VERSION 2
#endif
#if GATE_CHANNEL_VERSION != 2 && GATE_CHANNEL_VERSION != 3
#error Unsupported gate channel version
#endif
#if defined(GATE_TEST_CHANNEL_PATH) && !defined(GATE_TEST_BUILD)
#error GATE_TEST_CHANNEL_PATH is restricted to test builds
#endif
#ifdef GATE_TEST_BUILD
#ifndef GATE_TEST_TARGET
#error GATE_TEST_TARGET is required for a test build
#endif
#ifndef GATE_TEST_EXEC
#error GATE_TEST_EXEC is required for a test build
#endif
#define TARGET GATE_TEST_TARGET
#define EXECUTABLE GATE_TEST_EXEC
#else
#define TARGET "/var/lib/hub"
#define EXECUTABLE "/opt/venv/bin/python"
#endif
#if !defined(GATE_TEST_BUILD) && GATE_CHANNEL_VERSION == 3
#define GATE_CONTROL_SOCKET "/run/hub-gate/controller.sock"
#endif

extern char **environ;
static const char hex[] = "0123456789abcdef";
static const char *token_names[] = {"CODEX_TOKEN", "HERMES_TOKEN", "CLAUDE_TOKEN"};
static char token_env[3][4110];

static int clocks(uint64_t *wall, uint64_t *mono)
{
    struct timespec a, b;
    if (clock_gettime(CLOCK_REALTIME, &a) || clock_gettime(CLOCK_MONOTONIC, &b) ||
        a.tv_sec <= 0 || b.tv_sec < 0 || (uint64_t)a.tv_sec > UINT64_MAX / 1000 ||
        (uint64_t)b.tv_sec > UINT64_MAX / 1000) return 0;
    *wall = (uint64_t)a.tv_sec * 1000 + (uint64_t)a.tv_nsec / 1000000;
    *mono = (uint64_t)b.tv_sec * 1000 + (uint64_t)b.tv_nsec / 1000000;
    return 1;
}

static int key_decode(unsigned char key[32])
{
    const char *s = GATE_PUBLIC_KEY_HEX;
    if (strlen(s) != 64) return 0;
    for (size_t i = 0; i < 32; i++) {
        const char *a = strchr(hex, s[2*i]), *b = strchr(hex, s[2*i+1]);
        if (!a || !b || !s[2*i] || !s[2*i+1]) return 0;
        key[i] = (unsigned char)(((a - hex) << 4) | (b - hex));
    }
    return 1;
}

static int tokens_snapshot(void)
{
    int seen[3] = {0};
    for (char **e = environ; e && *e; e++) for (size_t i = 0; i < 3; i++) {
        size_t n = strlen(token_names[i]);
        if (strncmp(*e, token_names[i], n) || (*e)[n] != '=') continue;
        const char *v = *e + n + 1; size_t length = strnlen(v, 4097);
        if (seen[i]++ || !length || length > 4096) return 0;
        for (size_t j = 0; j < length; j++) if ((unsigned char)v[j] < 33 || (unsigned char)v[j] > 126) return 0;
        memcpy(token_env[i], *e, n + 1 + length + 1);
    }
    return seen[0] == 1 && seen[1] == 1 && seen[2] == 1;
}

static int self_digest(char output[72])
{
    int fd = open("/proc/self/exe", O_RDONLY | O_CLOEXEC); struct stat st;
    EVP_MD_CTX *ctx = NULL; unsigned char digest[32], buffer[8192]; unsigned int n = 0; int ok = 0;
    if (fd < 0 || fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0 || st.st_size > 128 * 1024 * 1024) goto done;
    ctx = EVP_MD_CTX_new();
    if (!ctx || EVP_DigestInit_ex(ctx, EVP_sha256(), NULL) != 1) goto done;
    size_t total = 0;
    for (;;) {
        ssize_t count = read(fd, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) goto done;
        if (!count) break;
        total += (size_t)count;
        if (total > (size_t)st.st_size || EVP_DigestUpdate(ctx, buffer, (size_t)count) != 1) goto done;
    }
    if (total != (size_t)st.st_size || EVP_DigestFinal_ex(ctx, digest, &n) != 1 || n != 32) goto done;
    memcpy(output, "sha256:", 7);
    for (size_t i = 0; i < 32; i++) { output[7 + 2*i] = hex[digest[i] >> 4]; output[8 + 2*i] = hex[digest[i] & 15]; }
    output[71] = '\0'; ok = 1;
done:
    EVP_MD_CTX_free(ctx); if (fd >= 0) close(fd); return ok;
}

static int mount_current(int fd, uint64_t dev, uint64_t ino, const struct gate_json_doc *doc, size_t identity)
{
    struct stat a, b; struct statvfs v;
    if (fstat(fd, &a) || lstat(TARGET, &b) || fstatvfs(fd, &v) || !S_ISDIR(a.st_mode) ||
        !S_ISDIR(b.st_mode) || (uint64_t)a.st_dev != dev || (uint64_t)b.st_dev != dev ||
        (uint64_t)a.st_ino != ino || (uint64_t)b.st_ino != ino || (v.f_flag & ST_RDONLY)) return 0;
    int i = gate_json_get(doc, identity, "mount_flags");
    if (i < 0 || doc->tokens[i].type != GATE_JSON_ARRAY) return 0;
    int nodev = 0, noexec = 0, nosuid = 0, rw = 0;
    for (size_t j = (size_t)i + 1; j < doc->tokens[i].next; j = doc->tokens[j].next) {
        nodev |= gate_json_equal(doc, j, "nodev"); noexec |= gate_json_equal(doc, j, "noexec");
        nosuid |= gate_json_equal(doc, j, "nosuid"); rw |= gate_json_equal(doc, j, "rw");
    }
    return rw && nodev == !!(v.f_flag & ST_NODEV) && noexec == !!(v.f_flag & ST_NOEXEC) &&
           nosuid == !!(v.f_flag & ST_NOSUID);
}

static int part(const struct gate_json_doc *doc, size_t object, const char *name, const char **s, int *len)
{
    int i = gate_json_get(doc, object, name);
    if (i < 0 || doc->tokens[i].end < doc->tokens[i].start || doc->tokens[i].end - doc->tokens[i].start > 1024) return 0;
    *s = doc->bytes + doc->tokens[i].start; *len = (int)(doc->tokens[i].end - doc->tokens[i].start); return 1;
}

int main(void)
{
    unsigned char key[32], random[32]; char nonce[65], executable[72], request[8193], start[8193], decision_wire[8193], ack[8193];
    size_t start_len, decision_len, ack_len, identity; struct gate_json_doc doc; struct gate_decision decision;
    uint64_t wall0, mono0, wall, mono, previous_wall; struct timespec deadline; struct stat st; int mount_fd = -1, channel = -1, result = 1;
    if (!tokens_snapshot() || clearenv() != 0 ||
        OPENSSL_init_crypto(OPENSSL_INIT_NO_LOAD_CONFIG, NULL) != 1 ||
        !key_decode(key) || !self_digest(executable) || !clocks(&wall0, &mono0) ||
        clock_gettime(CLOCK_MONOTONIC, &deadline) || deadline.tv_sec > (time_t)(INT64_MAX - 5) ||
        syscall(SYS_close_range, 4U, ~0U, CLOSE_RANGE_CLOEXEC) != 0) goto done;
    deadline.tv_sec += 5;
#ifdef GATE_TEST_CHANNEL_PATH
    (void)close(3);
    channel = gate_wire_connect(GATE_TEST_CHANNEL_PATH, &deadline);
#elif !defined(GATE_TEST_BUILD) && GATE_CHANNEL_VERSION == 3
    (void)close(3); /* Never trust or inherit a runtime-provided descriptor. */
    channel = gate_wire_connect(GATE_CONTROL_SOCKET, &deadline);
#else
    channel = 3; /* Legacy v2 contract; production activation remains disabled. */
#endif
    if (channel < 0 || fcntl(channel, F_SETFD, FD_CLOEXEC) < 0) goto done;
    ssize_t got = getrandom(random, sizeof(random), GRND_NONBLOCK);
    if (got != (ssize_t)sizeof(random)) goto done;
    for (size_t i = 0; i < 32; i++) { nonce[2*i] = hex[random[i] >> 4]; nonce[2*i+1] = hex[random[i] & 15]; }
    nonce[64] = '\0';
    mount_fd = open(TARGET, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (mount_fd < 0 || fstat(mount_fd, &st) || !S_ISDIR(st.st_mode) || !st.st_ino) goto done;
    uint64_t dev = (uint64_t)st.st_dev, ino = (uint64_t)st.st_ino;
    int n = snprintf(request, sizeof(request), "{\"nonce\":\"%s\",\"protocol\":\"engineering-hub.application-gate-start-request.v1\"}", nonce);
    if (n <= 0 || (size_t)n >= sizeof(request) || !gate_wire_send(channel, request, (size_t)n, &deadline) ||
        !gate_wire_recv(channel, start, 8192, &start_len, &deadline) || !clocks(&wall, &mono) || mono < mono0 ||
        mono - mono0 >= 5000 || !gate_start_verify(start, start_len, key, nonce, executable, dev, ino, wall0, mono - mono0, wall, &doc, &identity) ||
        !mount_current(mount_fd, dev, ino, &doc, identity)) goto done;
    previous_wall = wall;
    const struct gate_json_token *id = &doc.tokens[identity];
    const char *identity_wire = start + id->start; size_t identity_len = id->end - id->start;
    n = snprintf(request, sizeof(request), "{\"identity\":%.*s,\"observed_device\":%llu,\"observed_inode\":%llu,\"protocol\":\"engineering-hub.application-mount-observation.v1\"}",
        (int)identity_len, identity_wire, (unsigned long long)dev, (unsigned long long)ino);
    if (n <= 0 || (size_t)n >= sizeof(request) || !gate_wire_send(channel, request, (size_t)n, &deadline) ||
        !gate_wire_recv(channel, decision_wire, 8192, &decision_len, &deadline) || !clocks(&wall, &mono) ||
        wall < previous_wall || mono < mono0 || mono - mono0 >= 5000 ||
        !mount_current(mount_fd, dev, ino, &doc, identity) ||
        !gate_decision_verify(decision_wire, decision_len, key, identity_wire, identity_len, wall0, mono0, wall, mono, &decision)) goto done;
    previous_wall = wall;
    unsigned char digest[32]; unsigned int digest_len; char digest_hex[65];
    if (!EVP_Digest(decision_wire, decision_len, digest, &digest_len, EVP_sha256(), NULL) || digest_len != 32) goto done;
    for (size_t i = 0; i < 32; i++) { digest_hex[2*i] = hex[digest[i] >> 4]; digest_hex[2*i+1] = hex[digest[i] & 15]; }
    digest_hex[64] = '\0';
    const char *launch, *operation, *fence; int ll, ol, fl;
    if (!part(&doc, identity, "launch_id", &launch, &ll) || !part(&doc, identity, "operation_id", &operation, &ol) ||
        !part(&doc, identity, "fence", &fence, &fl)) goto done;
    n = snprintf(request, sizeof(request), "{\"decision_sha256\":\"%s\",\"fence\":%.*s,\"launch_id\":\"%.*s\",\"nonce\":\"%s\",\"operation_id\":\"%.*s\",\"protocol\":\"engineering-hub.application-gate-consume-request.v1\"}",
        digest_hex, fl, fence, ll, launch, nonce, ol, operation);
    if (n <= 0 || (size_t)n >= sizeof(request) || !mount_current(mount_fd, dev, ino, &doc, identity) ||
        !gate_wire_send(channel, request, (size_t)n, &deadline) || !gate_wire_recv(channel, ack, 8192, &ack_len, &deadline) ||
        !clocks(&wall, &mono) || wall < previous_wall || mono < mono0 || mono - mono0 >= 5000 ||
        !gate_ack_verify(ack, ack_len, key, identity_wire, identity_len,
            decision_wire, decision_len, wall0, mono0, wall, mono) || !mount_current(mount_fd, dev, ino, &doc, identity)) goto done;
    previous_wall = wall;
    if (!clocks(&wall, &mono) || wall < previous_wall || mono < mono0 || mono - mono0 >= 5000 ||
        !gate_decision_verify(decision_wire, decision_len, key, identity_wire, identity_len,
            wall0, mono0, wall, mono, &decision)) goto done;
#ifdef GATE_TEST_BUILD
    char *const args[] = {EXECUTABLE, NULL};
#else
    char *const args[] = {EXECUTABLE, "-I", "-B", "-m", "engineering_hub", "--host", "0.0.0.0", "--port", "8770", NULL};
#endif
    char *const env[] = {"PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1", "OPENBLAS_NUM_THREADS=1", "HUB_UI_DIRECTORY=/app/dashboard",
        "HUB_DB=/var/lib/hub/hub.sqlite3", "HUB_BASE_URL=http://127.0.0.1:8770",
        token_env[0], token_env[1], token_env[2], NULL};
    execve(EXECUTABLE, args, env);
done:
    if (mount_fd >= 0) close(mount_fd);
    if (channel >= 0) close(channel);
    return result;
}
