#include "json.h"

#include <limits.h>
#include <string.h>

struct parser { struct gate_json_doc *doc; size_t pos; };

static int token(struct parser *p, enum gate_json_type type, size_t start)
{
    if (p->doc->count == GATE_JSON_MAX_TOKENS) return -1;
    size_t i = p->doc->count++;
    p->doc->tokens[i] = (struct gate_json_token){type, start, 0, 0};
    return (int)i;
}

static int string(struct parser *p)
{
    const char *s = p->doc->bytes;
    size_t n = p->doc->length;
    if (p->pos >= n || s[p->pos++] != '"') return -1;
    int i = token(p, GATE_JSON_STRING, p->pos);
    if (i < 0) return -1;
    while (p->pos < n && s[p->pos] != '"') {
        unsigned char c = (unsigned char)s[p->pos++];
        if (c < 0x20 || c > 0x7e || c == '\\') return -1;
    }
    if (p->pos == n) return -1;
    p->doc->tokens[i].end = p->pos++;
    p->doc->tokens[i].next = p->doc->count;
    return i;
}

static int value(struct parser *p, unsigned depth);

static int compare_keys(const struct gate_json_doc *d, int a, int b)
{
    const struct gate_json_token *x = &d->tokens[a], *y = &d->tokens[b];
    size_t xn = x->end - x->start, yn = y->end - y->start;
    size_t n = xn < yn ? xn : yn;
    int cmp = memcmp(d->bytes + x->start, d->bytes + y->start, n);
    if (cmp) return cmp;
    return (xn > yn) - (xn < yn);
}

static int container(struct parser *p, unsigned depth, int object)
{
    const char *s = p->doc->bytes;
    size_t n = p->doc->length;
    if (depth >= GATE_JSON_MAX_DEPTH) return -1;
    size_t start = p->pos++;
    int i = token(p, object ? GATE_JSON_OBJECT : GATE_JSON_ARRAY, start);
    if (i < 0 || p->pos >= n) return -1;
    char close = object ? '}' : ']';
    int previous = -1;
    if (s[p->pos] != close) for (;;) {
        if (object) {
            int key = string(p);
            if (key < 0 || (previous >= 0 && compare_keys(p->doc, previous, key) >= 0)) return -1;
            previous = key;
            if (p->pos >= n || s[p->pos++] != ':') return -1;
        }
        if (value(p, depth + 1) < 0 || p->pos >= n) return -1;
        if (s[p->pos] == close) break;
        if (s[p->pos++] != ',') return -1;
    }
    p->pos++;
    p->doc->tokens[i].end = p->pos;
    p->doc->tokens[i].next = p->doc->count;
    return i;
}

static int value(struct parser *p, unsigned depth)
{
    const char *s = p->doc->bytes;
    size_t n = p->doc->length;
    if (p->pos >= n) return -1;
    if (s[p->pos] == '{') return container(p, depth, 1);
    if (s[p->pos] == '[') return container(p, depth, 0);
    if (s[p->pos] == '"') return string(p);
    size_t start = p->pos;
    if (s[p->pos] == '-') p->pos++;
    if (p->pos >= n || s[p->pos] < '0' || s[p->pos] > '9') return -1;
    if (s[p->pos] == '0') {
        if (p->pos != start) return -1; /* -0 is not canonical */
        p->pos++;
        if (p->pos < n && s[p->pos] >= '0' && s[p->pos] <= '9') return -1;
    } else {
        while (p->pos < n && s[p->pos] >= '0' && s[p->pos] <= '9') p->pos++;
    }
    int i = token(p, GATE_JSON_INTEGER, start);
    if (i < 0) return -1;
    p->doc->tokens[i].end = p->pos;
    p->doc->tokens[i].next = p->doc->count;
    return i;
}

int gate_json_parse(const char *bytes, size_t length, struct gate_json_doc *doc)
{
    if (!bytes || !doc || !length || length > GATE_JSON_MAX_BYTES) return 0;
    memset(doc, 0, sizeof(*doc));
    doc->bytes = bytes; doc->length = length;
    struct parser p = {doc, 0};
    if (value(&p, 0) != 0 || p.pos != length) { memset(doc, 0, sizeof(*doc)); return 0; }
    return 1;
}

int gate_json_equal(const struct gate_json_doc *doc, size_t i, const char *literal)
{
    if (!doc || !literal || i >= doc->count || doc->tokens[i].type != GATE_JSON_STRING) return 0;
    const struct gate_json_token *t = &doc->tokens[i];
    size_t n = strlen(literal);
    return n == t->end - t->start && memcmp(doc->bytes + t->start, literal, n) == 0;
}

int gate_json_get(const struct gate_json_doc *doc, size_t object, const char *key)
{
    if (!doc || !key || object >= doc->count || doc->tokens[object].type != GATE_JSON_OBJECT) return -1;
    size_t limit = doc->tokens[object].next;
    for (size_t i = object + 1; i < limit;) {
        size_t v = i + 1;
        if (v >= limit || doc->tokens[i].type != GATE_JSON_STRING) return -1;
        if (gate_json_equal(doc, i, key)) return (int)v;
        i = doc->tokens[v].next;
    }
    return -1;
}

int gate_json_u64(const struct gate_json_doc *doc, size_t i, uint64_t *value)
{
    if (!doc || !value || i >= doc->count || doc->tokens[i].type != GATE_JSON_INTEGER) return 0;
    const struct gate_json_token *t = &doc->tokens[i];
    uint64_t result = 0;
    for (size_t p = t->start; p < t->end; p++) {
        unsigned char c = (unsigned char)doc->bytes[p];
        if (c < '0' || c > '9' || result > (UINT64_MAX - (c - '0')) / 10) return 0;
        result = result * 10 + (c - '0');
    }
    *value = result;
    return 1;
}
