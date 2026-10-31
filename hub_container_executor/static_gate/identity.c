#include "identity.h"
#include <limits.h>
#include <string.h>

static int field_count(const struct gate_json_doc *d, size_t o)
{
    if (o >= d->count || d->tokens[o].type != GATE_JSON_OBJECT) return -1;
    int count = 0;
    for (size_t i = o + 1; i < d->tokens[o].next;) {
        if (i + 1 >= d->tokens[o].next) return -1;
        count++; i = d->tokens[i + 1].next;
    }
    return count;
}

static int chars(const struct gate_json_doc *d, int i, size_t len, int kind)
{
    if (i < 0 || d->tokens[i].type != GATE_JSON_STRING) return 0;
    const struct gate_json_token *t = &d->tokens[i];
    size_t n = t->end - t->start;
    if ((len && n != len) || (!len && (n == 0 || n > 128))) return 0;
    for (size_t j = 0; j < n; j++) {
        unsigned char c = (unsigned char)d->bytes[t->start + j];
        int hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
        if (kind == 1 && !hex) return 0;
        if (kind == 2 && ((j == 8 || j == 13 || j == 18 || j == 23) ? c != '-' : !hex)) return 0;
        if (kind == 3 && !(hex || (c >= 'A' && c <= 'Z') || (c >= 'g' && c <= 'z') || c == '_' || c == '.' || c == '-')) return 0;
        if (kind == 3 && j == 0 && !(hex || (c >= 'A' && c <= 'Z') || (c >= 'g' && c <= 'z'))) return 0;
    }
    return 1;
}

static int digest(const struct gate_json_doc *d, int i)
{
    if (i < 0 || d->tokens[i].type != GATE_JSON_STRING) return 0;
    const struct gate_json_token *t = &d->tokens[i];
    if (t->end - t->start != 71 || memcmp(d->bytes + t->start, "sha256:", 7)) return 0;
    for (size_t j = t->start + 7; j < t->end; j++) {
        char c = d->bytes[j];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return 0;
    }
    return 1;
}

int gate_identity_validate(const struct gate_json_doc *d, size_t o,
                           const char nonce[65], uint64_t device, uint64_t inode)
{
    static const char *uuids[] = {"boot_id", "launch_id", "operation_id", "run_id"};
    static const char *digests[] = {"image_config_id", "manifest_digest", "platform_manifest_digest", "runnable_digest"};
    if (!d || !nonce || field_count(d, o) != 19) return 0;
    int n = gate_json_get(d, o, "nonce");
    if (!chars(d, n, 64, 1) || !gate_json_equal(d, (size_t)n, nonce)) return 0;
    for (size_t i = 0; i < sizeof(uuids)/sizeof(*uuids); i++)
        if (!chars(d, gate_json_get(d, o, uuids[i]), 36, 2)) return 0;
    for (size_t i = 0; i < sizeof(digests)/sizeof(*digests); i++)
        if (!digest(d, gate_json_get(d, o, digests[i]))) return 0;
    if (!chars(d, gate_json_get(d, o, "container_id"), 64, 1) ||
        !chars(d, gate_json_get(d, o, "implementation_commit"), 40, 1) ||
        !chars(d, gate_json_get(d, o, "generation_id"), 0, 3)) return 0;
    int target = gate_json_get(d, o, "mount_target");
    if (target < 0 || !gate_json_equal(d, (size_t)target, "/var/lib/hub")) return 0;
    const char *nums[] = {"expected_device", "expected_inode", "fence", "host_pid", "process_start_ticks"};
    uint64_t values[5];
    for (size_t i = 0; i < 5; i++) {
        int x = gate_json_get(d, o, nums[i]);
        if (x < 0 || !gate_json_u64(d, (size_t)x, &values[i])) return 0;
    }
    if (values[0] != device || values[1] != inode || !inode ||
        !values[2] || !values[3] || !values[4] ||
        values[2] > INT64_MAX || values[3] > INT64_MAX || values[4] > INT64_MAX) return 0;
    int flags = gate_json_get(d, o, "mount_flags");
    if (flags < 0 || d->tokens[flags].type != GATE_JSON_ARRAY) return 0;
    const char *allowed[] = {"nodev", "noexec", "nosuid", "rw"};
    size_t j = 0; int rw = 0;
    for (size_t i = (size_t)flags + 1; i < d->tokens[flags].next; i = d->tokens[i].next) {
        while (j < 4 && !gate_json_equal(d, i, allowed[j])) j++;
        if (j == 4) return 0;
        if (j == 3) rw = 1;
        j++;
    }
    return rw;
}
