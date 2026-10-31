#ifndef HUB_GATE_JSON_H
#define HUB_GATE_JSON_H

#include <stddef.h>
#include <stdint.h>

#define GATE_JSON_MAX_BYTES 8192
#define GATE_JSON_MAX_TOKENS 256
#define GATE_JSON_MAX_DEPTH 8

enum gate_json_type { GATE_JSON_OBJECT, GATE_JSON_ARRAY, GATE_JSON_STRING, GATE_JSON_INTEGER };
struct gate_json_token {
    enum gate_json_type type;
    size_t start, end, next;
};
struct gate_json_doc {
    const char *bytes;
    size_t length, count;
    struct gate_json_token tokens[GATE_JSON_MAX_TOKENS];
};

/* Restrictive canonical ASCII subset: no escapes, booleans, null or floats. */
int gate_json_parse(const char *bytes, size_t length, struct gate_json_doc *doc);
/* Return token index or -1. Objects contain alternating string keys and values. */
int gate_json_get(const struct gate_json_doc *doc, size_t object, const char *key);
int gate_json_equal(const struct gate_json_doc *doc, size_t token, const char *literal);
int gate_json_u64(const struct gate_json_doc *doc, size_t token, uint64_t *value);

#endif
