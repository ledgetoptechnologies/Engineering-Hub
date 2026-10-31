#include "enrollment.h"
#include "identity.h"
#include "wire.h"

static int fields(const struct gate_json_doc *d, size_t o)
{
    if (o >= d->count || d->tokens[o].type != GATE_JSON_OBJECT) return -1;
    int count = 0;
    for (size_t i = o + 1; i < d->tokens[o].next;) {
        if (i + 1 >= d->tokens[o].next) return -1;
        count++; i = d->tokens[i + 1].next;
    }
    return count;
}

static int equal_field(const struct gate_json_doc *d, size_t o, const char *name, const char *value)
{
    int i = gate_json_get(d, o, name);
    return i >= 0 && gate_json_equal(d, (size_t)i, value);
}

static int number(const struct gate_json_doc *d, size_t o, const char *name, uint64_t *out)
{
    int i = gate_json_get(d, o, name);
    return i >= 0 && gate_json_u64(d, (size_t)i, out);
}

int gate_start_verify(const char *raw, size_t length, const unsigned char key[32],
                      const char nonce[65], const char executable_digest[72],
                      uint64_t device, uint64_t inode, uint64_t exchange_wall_ms,
                      uint64_t elapsed_ms, uint64_t now_ms,
                      struct gate_json_doc *doc, size_t *identity)
{
    static const char domain[] = "engineering-hub-application-gate-start-enrollment-v1";
    size_t p; uint64_t issued, expires;
    if (!raw || !key || !nonce || !executable_digest || !doc || !identity ||
        !gate_wire_verify(raw, length, key, domain, sizeof(domain), doc, &p) ||
        fields(doc, p) != 6 ||
        !equal_field(doc, p, "protocol", "engineering-hub.application-gate-start-enrollment.v1") ||
        !equal_field(doc, p, "audience", "engineering-hub.static-application-bootstrap") ||
        !equal_field(doc, p, "bootstrap_digest", executable_digest) ||
        !number(doc, p, "issued_at_ms", &issued) || !number(doc, p, "expires_at_ms", &expires) ||
        !exchange_wall_ms || expires <= issued || expires - issued > 5000 ||
        now_ms < exchange_wall_ms || now_ms < issued || now_ms >= expires ||
        (issued < exchange_wall_ms && exchange_wall_ms - issued > 5000) ||
        expires <= exchange_wall_ms || elapsed_ms >= expires - exchange_wall_ms) return 0;
    int i = gate_json_get(doc, p, "identity");
    if (i < 0 || !gate_identity_validate(doc, (size_t)i, nonce, device, inode)) return 0;
    *identity = (size_t)i;
    return 1;
}
