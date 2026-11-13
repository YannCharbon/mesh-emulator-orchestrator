/* SPDX-License-Identifier: Apache-2.0
 *
 * In-process nodes: what the upper stack holds, for sl_rf_inproc.c.
 */
#include "nsconfig.h"
#include "nanostack/mac/mac_common_defines.h"
#include "nanostack/source/nwk_interface/protocol.h"
#include "nanostack/source/6lowpan/mac/mpx_api.h"
#include "nanostack/source/6lowpan/ws/ws_llc.h"
#include "nanostack/source/service_libs/mac_neighbor_table/mac_neighbor_table.h"
#include "sl_rf_inproc.h"

bool rf_inproc_stack_holds_neighbor(const uint8_t eui64[8])
{
    protocol_interface_info_entry_t *cur = protocol_stack_interface_info_get(IF_6LoWPAN);

    if (!cur || !mac_neighbor_info(cur))
        return true;    // unknown: keep it
    return mac_neighbor_table_address_discover(mac_neighbor_info(cur), eui64, MAC_ADDR_MODE_64_BIT) ||
           ws_llc_get_multicast_temp_entry(cur, eui64) ||
           ws_llc_get_eapol_temp_entry(cur, eui64);
}
