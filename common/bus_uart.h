/* SPDX-License-Identifier: Apache-2.0
 * Copyright (c) 2021, Silicon Labs
 * Main authors:
 *    - Jérôme Pouiller <jerome.pouiller@silabs.com>
 */
#ifndef BUS_UART_H
#define BUS_UART_H

#include <stdbool.h>
#include <stdint.h>

// State of the Spinel/UART link to the RCP. It used to be part of the port's
// struct os_ctxt (common/os_types.h).
struct uart_ctxt {
    int     trig_fd;
    int     data_fd;
    int     spi_recv_window;
    bool    uart_next_frame_ready;
    int     uart_rx_buf_len;
    uint8_t uart_rx_buf[2048];
};

int uart_open(const char *device, int bitrate, bool hardflow);
int uart_tx(struct uart_ctxt *ctxt, const void *buf, unsigned int len);
int uart_rx(struct uart_ctxt *ctxt, void *buf, unsigned int len);

#endif

