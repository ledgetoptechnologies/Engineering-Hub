#define _GNU_SOURCE
#include "wire.h"

#include <errno.h>
#include <limits.h>
#include <openssl/evp.h>
#include <poll.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int seqpacket(int fd)
{
    int type = 0; socklen_t length = sizeof(type);
    struct sockaddr_storage address; socklen_t address_length = sizeof(address);
    return getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) == 0 &&
           length == sizeof(type) && type == SOCK_SEQPACKET &&
           getsockname(fd, (struct sockaddr *)&address, &address_length) == 0 &&
           address_length >= sizeof(sa_family_t) && address.ss_family == AF_UNIX;
}

static int before(const struct timespec *deadline)
{
    struct timespec now;
    return deadline && clock_gettime(CLOCK_MONOTONIC, &now) == 0 &&
        (now.tv_sec < deadline->tv_sec ||
         (now.tv_sec == deadline->tv_sec && now.tv_nsec < deadline->tv_nsec));
}

static int wait_ready(int fd, short events, const struct timespec *deadline)
{
    if (!deadline || deadline->tv_sec < 0 || deadline->tv_nsec < 0 || deadline->tv_nsec >= 1000000000L)
        return 0;
    for (;;) {
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0 ||
            now.tv_sec > deadline->tv_sec ||
            (now.tv_sec == deadline->tv_sec && now.tv_nsec >= deadline->tv_nsec)) return 0;
        time_t sec = deadline->tv_sec - now.tv_sec;
        long ns = deadline->tv_nsec - now.tv_nsec;
        if (ns < 0) { sec--; ns += 1000000000L; }
        int ms = sec >= INT_MAX / 1000 ? INT_MAX : (int)(sec * 1000 + (ns + 999999) / 1000000);
        struct pollfd pfd = {fd, events, 0};
        int ready = poll(&pfd, 1, ms);
        if (ready < 0 && errno == EINTR) continue;
        if (ready <= 0 || !(pfd.revents & events) || (pfd.revents & POLLNVAL)) return 0;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0 ||
            now.tv_sec > deadline->tv_sec ||
            (now.tv_sec == deadline->tv_sec && now.tv_nsec >= deadline->tv_nsec)) return 0;
        return 1;
    }
}

int gate_wire_send(int fd, const void *bytes, size_t length, const struct timespec *deadline)
{
    if (!bytes || !length || length > GATE_JSON_MAX_BYTES || !seqpacket(fd) ||
        !wait_ready(fd, POLLOUT, deadline)) return 0;
    /* One nonblocking send only: any failure or uncertainty is terminal. */
    ssize_t sent = send(fd, bytes, length, MSG_DONTWAIT | MSG_NOSIGNAL);
    return sent == (ssize_t)length && before(deadline);
}

int gate_wire_recv(int fd, char *buffer, size_t capacity, size_t *length,
                   const struct timespec *deadline)
{
    if (!buffer || !length || !capacity || capacity > GATE_JSON_MAX_BYTES || !seqpacket(fd) ||
        !wait_ready(fd, POLLIN, deadline)) return 0;
    union { struct cmsghdr align; unsigned char bytes[CMSG_SPACE(256 * sizeof(int))]; } control;
    struct iovec iov = {buffer, capacity};
    struct msghdr msg = {0};
    msg.msg_iov = &iov; msg.msg_iovlen = 1;
    msg.msg_control = control.bytes; msg.msg_controllen = sizeof(control.bytes);
    ssize_t got = recvmsg(fd, &msg, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
    if (got < 0) return 0;
    int ancillary = 0;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg, c)) {
        ancillary = 1;
        if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS && c->cmsg_len >= CMSG_LEN(0)) {
            size_t count = (c->cmsg_len - CMSG_LEN(0)) / sizeof(int);
            unsigned char *raw = CMSG_DATA(c);
            for (size_t i = 0; i < count; i++) {
                int received;
                memcpy(&received, raw + i * sizeof(int), sizeof(received));
                (void)close(received);
            }
        }
    }
    if (got == 0 || ancillary || (msg.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) || (size_t)got > capacity) return 0;
    if (!before(deadline)) return 0;
    *length = (size_t)got;
    return 1;
}

static int hex(unsigned char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

int gate_wire_verify(const char *bytes, size_t length, const unsigned char public_key[32],
                     const void *domain, size_t domain_length,
                     struct gate_json_doc *doc, size_t *payload)
{
    if (!bytes || !public_key || !domain || !domain_length || domain_length > 256 || !doc || !payload ||
        !gate_json_parse(bytes, length, doc) || doc->tokens[0].type != GATE_JSON_OBJECT) return 0;
    int p = gate_json_get(doc, 0, "payload"), s = gate_json_get(doc, 0, "signature");
    if (p != 2 || s < 0 || (size_t)s != doc->tokens[p].next + 1 || doc->tokens[p].type != GATE_JSON_OBJECT ||
        doc->tokens[s].type != GATE_JSON_STRING || doc->tokens[s].next != doc->tokens[0].next ||
        doc->tokens[s].end - doc->tokens[s].start != 128) return 0;
    unsigned char signature[64];
    for (size_t i = 0; i < sizeof(signature); i++) {
        int a = hex((unsigned char)bytes[doc->tokens[s].start + i * 2]);
        int b = hex((unsigned char)bytes[doc->tokens[s].start + i * 2 + 1]);
        if (a < 0 || b < 0) return 0;
        signature[i] = (unsigned char)((a << 4) | b);
    }
    size_t payload_length = doc->tokens[p].end - doc->tokens[p].start;
    if (payload_length > GATE_JSON_MAX_BYTES) return 0;
    unsigned char message[256 + GATE_JSON_MAX_BYTES];
    memcpy(message, domain, domain_length);
    memcpy(message + domain_length, bytes + doc->tokens[p].start, payload_length);
    EVP_PKEY *key = EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, NULL, public_key, 32);
    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    int ok = key && ctx && EVP_DigestVerifyInit(ctx, NULL, NULL, NULL, key) == 1 &&
             EVP_DigestVerify(ctx, signature, sizeof(signature), message, domain_length + payload_length) == 1;
    EVP_MD_CTX_free(ctx); EVP_PKEY_free(key);
    if (!ok) return 0;
    *payload = (size_t)p;
    return 1;
}
