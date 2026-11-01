#ifndef HUB_GATE_DECISION_H
#define HUB_GATE_DECISION_H

#include <stddef.h>
#include <stdint.h>

struct gate_decision { uint64_t issued_at_ms, expires_at_ms; };
/* identity is an independently verified canonical enrollment object.
 * Times are caller-measured CLOCK_REALTIME and CLOCK_MONOTONIC milliseconds.
 * These functions do not prove peer identity or continuing mount custody. */
int gate_decision_verify(const char *wire, size_t wire_len, const unsigned char key[32],
    const char *identity, size_t identity_len, uint64_t exchange_wall_ms,
    uint64_t exchange_mono_ms, uint64_t now_wall_ms, uint64_t now_mono_ms,
    struct gate_decision *result);
int gate_ack_verify(const char *wire, size_t wire_len, const unsigned char key[32],
    const char *identity, size_t identity_len, const char *decision_wire, size_t decision_len,
    uint64_t exchange_wall_ms, uint64_t exchange_mono_ms, uint64_t now_wall_ms,
    uint64_t now_mono_ms);

#endif
