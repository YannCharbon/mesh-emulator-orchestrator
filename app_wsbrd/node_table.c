/* SPDX-License-Identifier: Apache-2.0 */
#include <stdlib.h>
#include <string.h>

#include "node_table.h"

static int route_info_compare(const void *obj_a, const void *obj_b)
{
    const bbr_route_info_t *a = obj_a, *b = obj_b;
    int ret;

    ret = memcmp(a->parent, b->parent, sizeof(a->parent));
    if (ret)
        return ret;
    ret = memcmp(a->target, b->target, sizeof(a->target));
    return ret;
}

int wsbr_node_table_get(int rcp_if_id, bbr_route_info_t *table, int table_len)
{
    int len;

    len = ws_bbr_routing_table_get(rcp_if_id, table, table_len);
    if (len < 0)
        return len;
    qsort(table, len, sizeof(table[0]), route_info_compare);
    return len;
}
