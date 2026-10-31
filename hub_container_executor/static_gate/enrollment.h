#ifndef HUB_GATE_ENROLLMENT_H
#define HUB_GATE_ENROLLMENT_H
#include "json.h"
#include <stdint.h>

/* doc retains references into raw, which the caller must keep unchanged. */
int gate_start_verify(const char *raw, size_t length, const unsigned char key[32],
                      const char nonce[65], const char executable_digest[72],
                      uint64_t device, uint64_t inode, uint64_t exchange_wall_ms,
                      uint64_t elapsed_ms, uint64_t now_ms,
                      struct gate_json_doc *doc, size_t *identity);
#endif
