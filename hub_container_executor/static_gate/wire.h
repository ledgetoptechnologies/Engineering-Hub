#ifndef HUB_GATE_WIRE_H
#define HUB_GATE_WIRE_H

#include <stddef.h>
#include <time.h>
#include "json.h"

/* Deadline is an absolute CLOCK_MONOTONIC time, shared across the exchange.
 * A failed send can still have reached the peer and must never be retried. */
int gate_wire_send(int fd, const void *bytes, size_t length, const struct timespec *deadline);
int gate_wire_recv(int fd, char *buffer, size_t capacity, size_t *length,
                   const struct timespec *deadline);
/* Connect once to a pinned socket path. Signed messages authenticate issuer;
 * pathname checks alone do not establish exclusive host custody. */
int gate_wire_connect(const char *path, const struct timespec *deadline);
/* doc references bytes; payload receives the index of the signed object.
 * The caller must validate the entire protocol-specific payload schema. */
int gate_wire_verify(const char *bytes, size_t length, const unsigned char public_key[32],
                     const void *domain, size_t domain_length,
                     struct gate_json_doc *doc, size_t *payload);

#endif
