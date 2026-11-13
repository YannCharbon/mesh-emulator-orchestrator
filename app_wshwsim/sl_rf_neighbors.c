/* SPDX-License-Identifier: Apache-2.0
 *
 * Neighbour FHSS timings: see sl_rf_neighbors.h.
 */
#include <string.h>

#include "common/log.h"
#include "sl_rf_neighbors.h"

static const uint8_t eui64_none[8] = { 0 };

static unsigned int rf_neighbors_hash(const uint8_t eui64[8])
{
    uint64_t v;

    memcpy(&v, eui64, 8);
    return (v * 0x9E3779B97F4A7C15ull) >> (64 - 10);
}
_Static_assert(RF_NEIGHBORS_HASH == 1 << 10, "rf_neighbors_hash() gives 10 bits");
_Static_assert(RF_NEIGHBORS_MAX < UINT16_MAX, "slot + 1 in a uint16_t");

void rf_neighbors_clear(struct rf_neighbors *t)
{
    bool (*still_known)(const uint8_t eui64[8]) = t->still_known;

    memset(t, 0, sizeof(*t));
    t->still_known = still_known;
}

static int rf_neighbors_find(struct rf_neighbors *t, const uint8_t eui64[8])
{
    unsigned int i;

    for (i = t->head[rf_neighbors_hash(eui64)]; i; i = t->next[i - 1])
        if (!memcmp(t->slot[i - 1].eui64, eui64, 8))
            return i - 1;
    return -1;
}

struct fhss_ws_neighbor_timing_info *rf_neighbors_get(struct rf_neighbors *t, const uint8_t eui64[8])
{
    int i = rf_neighbors_find(t, eui64);

    return i < 0 ? NULL : &t->slot[i].val;
}

static void rf_neighbors_remove(struct rf_neighbors *t, int i)
{
    uint16_t *p = &t->head[rf_neighbors_hash(t->slot[i].eui64)];

    while (*p != i + 1)
        p = &t->next[*p - 1];
    *p = t->next[i];
    t->next[i] = 0;
    memset(t->slot[i].eui64, 0, 8);
    t->count--;
}

// Full: first the entries the stack no longer holds, else the one updated
// longest ago.
static void rf_neighbors_make_room(struct rf_neighbors *t)
{
    const char *why = "the stack no longer holds them";
    int i, oldest = -1, n = 0;

    if (t->still_known)
        for (i = 0; i < RF_NEIGHBORS_MAX; i++)
            if (!t->still_known(t->slot[i].eui64)) {
                rf_neighbors_remove(t, i);
                n++;
            }
    if (!n) {
        for (i = 0; i < RF_NEIGHBORS_MAX; i++)
            if (oldest < 0 || t->stamp[i] - t->stamp[oldest] > INT32_MAX)
                oldest = i;
        rf_neighbors_remove(t, oldest);
        n = 1;
        why = "updated longest ago";
    }
    t->removed += n;
    // One line per clean-up; one-by-one removals at 1, 2, 4, 8... in all
    if (n > 1 || !(t->removed & (t->removed - 1)))
        WARN("neighbour timings full (%d entries): %d removed (%s), %d in all",
             RF_NEIGHBORS_MAX, n, why, t->removed);
}

struct fhss_ws_neighbor_timing_info *rf_neighbors_update(struct rf_neighbors *t, const uint8_t eui64[8])
{
    unsigned int h;
    int i = rf_neighbors_find(t, eui64);

    // The all-zero EUI-64 marks a free slot: not stored (as before, where
    // it matched the first free slot)
    if (!memcmp(eui64, eui64_none, 8))
        return &t->scratch;
    if (i < 0) {
        if (t->count == RF_NEIGHBORS_MAX)
            rf_neighbors_make_room(t);
        for (i = 0; i < RF_NEIGHBORS_MAX; i++)
            if (!memcmp(t->slot[i].eui64, eui64_none, 8))
                break;
        BUG_ON(i == RF_NEIGHBORS_MAX);
        memcpy(t->slot[i].eui64, eui64, 8);
        h = rf_neighbors_hash(eui64);
        t->next[i] = t->head[h];
        t->head[h] = i + 1;
        t->count++;
    }
    t->stamp[i] = ++t->clock;
    return &t->slot[i].val;
}

bool rf_neighbors_drop(struct rf_neighbors *t, const uint8_t eui64[8])
{
    int i = rf_neighbors_find(t, eui64);

    if (i < 0)
        return false;
    rf_neighbors_remove(t, i);
    return true;
}
