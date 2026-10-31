#ifndef HUB_GATE_IDENTITY_H
#define HUB_GATE_IDENTITY_H
#include "json.h"
#include <stdint.h>

/* Validate the complete signed identity, with no implicit external authority. */
int gate_identity_validate(const struct gate_json_doc *doc, size_t object,
                           const char nonce[65], uint64_t device, uint64_t inode);
#endif
