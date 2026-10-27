/* SPDX-License-Identifier: Apache-2.0 */
#ifndef WSBR_NODE_TABLE_H
#define WSBR_NODE_TABLE_H

#include "nanostack/ws_bbr_api.h"

// The border router's view of the network: one entry per node, the node's
// IID and its RPL parent's IID, sorted by parent then node. This is the table
// that D-Bus "Nodes" returns (dbus.c) and that the virtual-time build logs
// (wsbr.c). Returns the number of entries, or < 0 if the DODAG is not up.
int wsbr_node_table_get(int rcp_if_id, bbr_route_info_t *table, int table_len);

#endif
