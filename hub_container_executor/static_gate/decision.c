#include "decision.h"
#include "wire.h"
#include <openssl/evp.h>
#include <string.h>

static const char *identity_fields[] = {"boot_id", "container_id", "expected_device", "expected_inode",
    "fence", "generation_id", "host_pid", "image_config_id", "implementation_commit", "launch_id",
    "manifest_digest", "mount_flags", "mount_target", "nonce", "operation_id", "platform_manifest_digest",
    "process_start_ticks", "run_id", "runnable_digest"};

static int fields(const struct gate_json_doc *d, size_t object, size_t count)
{
    if (object >= d->count || d->tokens[object].type != GATE_JSON_OBJECT) return 0;
    size_t i = object + 1, n = 0, end = d->tokens[object].next;
    while (i < end) {
        if (d->tokens[i].type != GATE_JSON_STRING || i + 1 >= end) return 0;
        i = d->tokens[i + 1].next; n++;
    }
    return i == end && n == count;
}

static int equal(const struct gate_json_doc *a, int x, const struct gate_json_doc *b, int y)
{
    if (x < 0 || y < 0 || a->tokens[x].type != b->tokens[y].type) return 0;
    const struct gate_json_token *p = &a->tokens[x], *q = &b->tokens[y];
    return p->end - p->start == q->end - q->start &&
        memcmp(a->bytes + p->start, b->bytes + q->start, p->end - p->start) == 0;
}

static int eqstr(const struct gate_json_doc *d, size_t obj, const char *key, const char *v)
{
    int i = gate_json_get(d, obj, key);
    return i >= 0 && gate_json_equal(d, (size_t)i, v);
}

static int number(const struct gate_json_doc *d, size_t obj, const char *key, uint64_t *v)
{
    int i = gate_json_get(d, obj, key);
    return i >= 0 && gate_json_u64(d, (size_t)i, v);
}

static int string_format(const struct gate_json_doc *d, int i, int kind)
{
    if (i < 0 || d->tokens[i].type != GATE_JSON_STRING) return 0;
    const struct gate_json_token *t = &d->tokens[i]; size_t n = t->end - t->start;
    const char *s = d->bytes + t->start;
    if ((kind == 1 && n != 64) || (kind == 2 && n != 71) ||
        (kind == 3 && n != 40) || (kind == 4 && n != 36) ||
        (kind == 5 && (!n || n > 128))) return 0;
    if (kind == 2 && memcmp(s, "sha256:", 7)) return 0;
    for (size_t j = kind == 2 ? 7 : 0; j < n; j++) {
        char c = s[j];
        if (kind == 4 && (j == 8 || j == 13 || j == 18 || j == 23)) { if (c != '-') return 0; continue; }
        if (kind == 5) {
            int alpha = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z');
            int digit = c >= '0' && c <= '9';
            if (!alpha && !digit && (j == 0 || (c != '_' && c != '.' && c != '-'))) return 0;
        } else if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
    }
    return 1;
}

static int identity_valid(const struct gate_json_doc *d)
{
    uint64_t n;
    const char *hex64[] = {"nonce", "container_id"};
    const char *digest[] = {"manifest_digest", "image_config_id", "runnable_digest", "platform_manifest_digest"};
    const char *uuid[] = {"launch_id", "operation_id", "run_id", "boot_id"};
    const char *positive[] = {"fence", "host_pid", "process_start_ticks"};
    if (!fields(d, 0, 19) || !eqstr(d, 0, "mount_target", "/var/lib/hub") ||
        !string_format(d, gate_json_get(d, 0, "implementation_commit"), 3) ||
        !string_format(d, gate_json_get(d, 0, "generation_id"), 5) ||
        !number(d, 0, "expected_device", &n) || !number(d, 0, "expected_inode", &n) || !n) return 0;
    for (size_t i = 0; i < sizeof(hex64)/sizeof(hex64[0]); i++)
        if (!string_format(d, gate_json_get(d, 0, hex64[i]), 1)) return 0;
    for (size_t i = 0; i < sizeof(digest)/sizeof(digest[0]); i++)
        if (!string_format(d, gate_json_get(d, 0, digest[i]), 2)) return 0;
    for (size_t i = 0; i < sizeof(uuid)/sizeof(uuid[0]); i++)
        if (!string_format(d, gate_json_get(d, 0, uuid[i]), 4)) return 0;
    for (size_t i = 0; i < sizeof(positive)/sizeof(positive[0]); i++)
        if (!number(d, 0, positive[i], &n) || !n || n >= (UINT64_C(1) << 63)) return 0;
    int a = gate_json_get(d, 0, "mount_flags");
    if (a < 0 || d->tokens[a].type != GATE_JSON_ARRAY) return 0;
    const char *flags[] = {"nodev", "noexec", "nosuid", "rw"}; size_t previous = 0; int seen_rw = 0, first = 1;
    for (size_t j = (size_t)a + 1; j < d->tokens[a].next; j = d->tokens[j].next) {
        size_t k = 0;
        while (k < 4 && !gate_json_equal(d, j, flags[k])) k++;
        if (k == 4 || (!first && k <= previous)) return 0;
        first = 0; previous = k; if (k == 3) seen_rw = 1;
    }
    return seen_rw;
}

static int fresh(uint64_t issued, uint64_t expires, uint64_t wall0, uint64_t mono0,
                 uint64_t wall, uint64_t mono)
{
    return wall0 > 0 && issued > 0 && expires > issued && expires - issued <= 5000 &&
        expires < UINT64_C(9007199254740992) && (issued >= wall0 || wall0 - issued <= 5000) &&
        wall >= wall0 && mono >= mono0 && issued <= wall && wall < expires && expires > wall0 &&
        mono - mono0 < expires - wall0;
}

int gate_decision_verify(const char *wire, size_t wire_len, const unsigned char key[32],
    const char *identity, size_t identity_len, uint64_t wall0, uint64_t mono0,
    uint64_t wall, uint64_t mono, struct gate_decision *result)
{
    static const char domain[] = "engineering-hub.mount-gate-decision.v1";
    struct gate_json_doc d, id; size_t p; uint64_t ed, ei, od, oi, issued, expires;
    if (!result || !gate_json_parse(identity, identity_len, &id) || !identity_valid(&id) ||
        !gate_wire_verify(wire, wire_len, key, domain, sizeof(domain), &d, &p) || !fields(&d, p, 26)) return 0;
    for (size_t i = 0; i < sizeof(identity_fields) / sizeof(identity_fields[0]); i++) {
        const char *name = identity_fields[i];
        if (!equal(&id, gate_json_get(&id, 0, name), &d, gate_json_get(&d, p, name))) return 0;
    }
    if (!eqstr(&d, p, "protocol", "engineering-hub.mount-gate-decision.v1") ||
        !eqstr(&d, p, "audience", "engineering-hub.application-bootstrap") ||
        !eqstr(&d, p, "decision", "allow") ||
        !number(&d, p, "expected_device", &ed) || !number(&d, p, "expected_inode", &ei) ||
        !number(&d, p, "observed_device", &od) || !number(&d, p, "observed_inode", &oi) ||
        !ei || ed != od || ei != oi || !number(&d, p, "issued_at_ms", &issued) ||
        !number(&d, p, "expires_at_ms", &expires) || !fresh(issued, expires, wall0, mono0, wall, mono)) return 0;
    result->issued_at_ms = issued; result->expires_at_ms = expires;
    return 1;
}

int gate_ack_verify(const char *wire, size_t wire_len, const unsigned char key[32],
    const char *identity, size_t identity_len, const char *decision_wire, size_t decision_len,
    uint64_t wall0, uint64_t mono0, uint64_t wall, uint64_t mono)
{
    static const char domain[] = "engineering-hub-application-gate-consume-ack-v1";
    struct gate_decision decision; struct gate_json_doc d, id; size_t p; uint64_t expiry;
    if (!gate_decision_verify(decision_wire, decision_len, key, identity, identity_len,
            wall0, mono0, wall, mono, &decision) || !gate_json_parse(identity, identity_len, &id) ||
        !gate_wire_verify(wire, wire_len, key, domain, sizeof(domain), &d, &p) || !fields(&d, p, 8) ||
        !eqstr(&d, p, "protocol", "engineering-hub.application-gate-consume-ack.v1") ||
        !eqstr(&d, p, "audience", "engineering-hub.static-application-bootstrap") ||
        !number(&d, p, "expires_at_ms", &expiry) || expiry != decision.expires_at_ms) return 0;
    const char *names[] = {"nonce", "launch_id", "operation_id", "fence"};
    for (size_t i = 0; i < sizeof(names) / sizeof(names[0]); i++)
        if (!equal(&id, gate_json_get(&id, 0, names[i]), &d, gate_json_get(&d, p, names[i]))) return 0;
    unsigned char digest[32]; unsigned int digest_len = 0;
    if (!EVP_Digest(decision_wire, decision_len, digest, &digest_len, EVP_sha256(), NULL) || digest_len != 32) return 0;
    char expected[65]; static const char hex[] = "0123456789abcdef";
    for (size_t i = 0; i < 32; i++) { expected[i * 2] = hex[digest[i] >> 4]; expected[i * 2 + 1] = hex[digest[i] & 15]; }
    expected[64] = '\0';
    return eqstr(&d, p, "decision_sha256", expected);
}
