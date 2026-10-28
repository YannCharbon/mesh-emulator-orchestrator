/*
 * Copyright (c) 2016-2019, Arm Limited and affiliates.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
#define _GNU_SOURCE
#include <time.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <systemd/sd-bus.h>
#include <error.h>
#ifdef HAVE_LIBPCAP
#  include <pcap/pcap.h>
#endif

#include <stdint.h>
#include "mbed-client-libservice/ns_trace.h"
#include "mbed-client-libservice/common_functions.h"
#include "nanostack/mac/platform/arm_hal_phy.h"
#include "nanostack/mac/mlme.h"
#include "nanostack/mac/mac_api.h"
#include "common/log.h"
#include "common/utils.h"
#include "ns_port/critical.h"
#include "ns_port/sched.h"
#include "ns_port/time.h"
#include "mac/rf_driver_storage.h"
#include "sl_rf_driver.h"
#include <sys/time.h>
#include "common/os_timer.h"
#include "common/fhss_timer.h"

#include <signal.h>
#include "fhss/fhss.h"
#include "ringbufindex.h"
#include <sched.h>
#include <pthread.h>

#include <sys/mman.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/un.h>

#include "sim_proto.h"
#ifdef NS_PORT_SIM
#include "ns_port_sim.h"
#endif
#include <math.h>

#define TRACE_GROUP "vrf"

#define FHSS_ON

struct rf_sim_host g_rf_host = { .rf_fd = -1 };

static phy_device_driver_s device_driver;
static uint8_t rf_mac_address[8];
static int8_t rf_radio_driver_id = (-1);
static bool data_request_pending_flag = false;


// The port's timebase: CLOCK_MONOTONIC in µs, modulo 2^32, on the POSIX port.
static uint32_t rf_get_timestamp(void)
{
    return ns_port_time_us();
}


static const phy_rf_channel_configuration_s phy_subghz = {.channel_0_center_frequency = 863100000, .channel_spacing = 2000000, .datarate = 150000, .number_of_channels = 35, .modulation = M_2FSK, MODULATION_INDEX_0_5};

static phy_device_channel_page_s phy_channel_pages[] = {
    {CHANNEL_PAGE_0, &phy_subghz},
    {CHANNEL_PAGE_0, NULL},
};

static int8_t phy_rf_state_control(phy_interface_state_e new_state, uint8_t channel);
static int8_t phy_rf_tx(uint8_t *data_ptr, uint16_t data_len, uint8_t tx_handle, data_protocol_e protocol);
static int8_t phy_rf_address_write(phy_address_type_e address_type, uint8_t *address_ptr);
static int8_t phy_rf_extension(phy_extension_type_e extension_type, uint8_t *data_ptr);

/**
 * @brief All the variables of new driver
 *
 */
enum driver_state {
    /// Driver is not initialized
    DRVSTATE_RADIO_UNINIT,
    /// Driver is beeing initialized but is not ready to be used
    DRVSTATE_RADIO_INITING,
    /// Driver is in a state where any operation/maintenance can be performed
    DRVSTATE_IDLE,
    /// Driver is listening channel for incoming packets
    DRVSTATE_IDLE_WAITING_RX,
    /// Driver is transmitting
    DRVSTATE_TX,
    /// Driver is receiving
    DRVSTATE_RX,
    /// Driver is inside the CSMA process
    DRVSTATE_CSMA_STARTED,
    /// Radio off (PHY_INTERFACE_DOWN): hears nothing (LFN study E1, E2).
    /// Last, so the values above, seen by other nodes in the CCA shared
    /// memory and on D-Bus, do not change.
    DRVSTATE_OFF,
};
static volatile enum driver_state driver_state = DRVSTATE_RADIO_UNINIT;
/**
 * @enum idle_state
 * @brief Represent the different idle states depths that can be reached *
 */
enum idle_state {
    /// Puts the radio in idle state after current TRX operation is finished
    RADIO_IDLE,
    /// Puts the radio in idle state immediately by cancelling the current TRX operation
    RADIO_IDLE_ABORT,
    /// Puts the radio in shutdown/sleep mode immediately by cancelling the current TRX operation and clearing any pending flags
    RADIO_SHUTDOWN,
};

/* Channel*/
static uint16_t channel = 0;
static uint16_t new_channel = 0;

/* Handles */
uint8_t mac_tx_handle;

/* Timers */
static int csma_timeout_timer;
static int tx_sleep_handler_timer;
static int rx_sleep_handler_timer;
static int backup_timeout_timer;


/* Timestamps */
volatile static uint32_t backoff_time = 0;
static int cca_enabled = 0;
static uint32_t packet_rxsync_time = 0;

/* Locks */
bool rx_thread_blokcked;

/* Radio off (E1): PHY_INTERFACE_DOWN during a TX turns the radio off at its
 * end; frames that arrive while off are lost (E2), counted here */
static bool rf_off_pending;
static unsigned long rf_frames_lost_off;
/* WSSIM_RADIO_OFF=0: PHY_INTERFACE_DOWN leaves the radio listening, as before
 * the off state existed (for comparisons with older runs). Default 1. */
static bool rf_off_state_enabled = true;

/* Buffers for RX and TX */
#define MAX_TXRX_BUFFERS 128
static struct ringbufindex input_ringbuf;
static struct ringbufindex output_ringbuf;
struct output_packet {
        uint8_t data[MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE]; /* Packet payload */
        uint16_t data_len;
        uint8_t tx_handle;
        data_protocol_e protocol;
    };

struct input_packet {
  uint8_t payload[MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE]; /* Packet payload */
  int len; /* Packet len */
  uint16_t channel; /* Channel we received the packet on */
  uint32_t rx_time;
  int8_t rssi;
};

struct input_packet input_array[MAX_TXRX_BUFFERS];
struct output_packet output_array[MAX_TXRX_BUFFERS];

#define PACKET_SENDING_EXTRA_TIME   5000
// Longest frame on air plus a margin: the backup timer of RX and TX
// (rf_phy_timing_update()).
uint32_t MAX_PACKET_SENDING_TIME = (uint32_t)(8000000/150000)*MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE + PACKET_SENDING_EXTRA_TIME;

/* PHY timing (docs/POWER_MODEL.md §12.4). A frame lasts its bytes on air at
 * the data rate: the SHR (preamble and SFD), the PHR, the PSDU the MAC gives
 * (without FCS) and the FCS. The data rate is the one the MAC configures
 * (PHY_EXTENSION_SET_RF_CONFIGURATION, from the PHY mode); phy_subghz's
 * 150 kbit/s only until then. User settings, in the nodes' environment:
 *   WSSIM_PHY_DATARATE=<bit/s>  this rate whatever the MAC configures
 *   WSSIM_PHY_PREAMBLE=<bytes>  default 8 (Wi-SUN FAN: at least 64 bits)
 *   WSSIM_PHY_SFD=<bytes>       default 2 (SUN FSK, 16-bit SFD)
 *   WSSIM_PHY_PHR=<bytes>       default 2 (SUN FSK PHR)
 *   WSSIM_PHY_FCS=<2|4>         FCS this node sends, default 4 (FAN: CRC-32);
 *                               frames carry it, as the PHR's FCS type bit
 *   WSSIM_PHY_LEGACY=1          the PHY before these settings: 150 kbit/s
 *                               always, airtime of the PSDU only (to compare
 *                               with older runs)
 * The receiver hears a frame for the sender's SHR and FCS (in the frame's
 * header, struct sim_frame_hdr) and its own PHR, at its own rate. */
static uint32_t rf_datarate = 150000;   // bit/s, used for every airtime (phy_subghz until init)
static uint32_t rf_datarate_fixed;      // WSSIM_PHY_DATARATE, 0 if unset
static bool rf_phy_legacy;
static uint8_t rf_preamble_len = 8;
static uint8_t rf_sfd_len = 2;
static uint8_t rf_phr_len = 2;
static uint8_t rf_fcs_len = 4;

/* Functions */
static void _rf_set_off(void);
static void _rf_stop_csma_timeout_timer();
static void _rf_rearm_csma_timeout_timer();
static int _rf_set_rx();
static int _rf_check_and_change_channel(uint16_t new_channel);
static void _rf_start_backup_timer(uint32_t duration);
static void _rf_stop_backup_timer();
static void ringbufindex_flush(struct ringbufindex *rbi);
static void _rf_rx_sleep_timeout_timer(uint32_t duration);
static void _rf_backup_timeout_handler();

/* Functions DBUS */
static int rf_dbus_get_rf_status(sd_bus *bus, const char *path, const char *interface,
                       const char *property, sd_bus_message *reply,
                        void *userdata, sd_bus_error *ret_error);
static uint8_t * rf_request_rf_state_dbus(uint8_t *dest_mac_addr);

/* Variables DBUS */
sd_bus * rf_dbus;
sd_bus * rf_request_dbus;
bool rf_dbus_registered = false;

/* Functions shm */
void set_shm_cca_state();
void get_shm_cca_state();

/* Variables shm. The names belong to the server socket (sim_shm_name()),
 * which rf_sim_connect() records before shm_init() runs. */
static char sim_socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
const int shm_size = SIM_SHM_CCA_SIZE;
int shm_fd = -1;
void* shm_ptr = NULL;

/* Shared memory for RSSI infos */
struct node_infos *nodes_infos_flat_map;
const long shm_infos_size = SIM_SHM_INFOS_SIZE;

/* Loss model */
// The bits the receiver must decode: the PHR, the PSDU and the FCS (the frame
// given to the driver has no FCS), as set above.
static uint64_t rf_rng_state;

/* Threads */
pthread_t rf_dbus_proces_thread;

/* Extra PHY defines, Helper functions */
// Airtime of a frame of psdu bytes (no FCS) with shr bytes of SHR and fcs of
// FCS, at the current rate. Legacy: the PSDU only.
static unsigned int rf_airtime_us(unsigned int psdu, unsigned int shr, unsigned int fcs)
{
    uint64_t bytes = psdu;

    if (!rf_phy_legacy)
        bytes += shr + rf_phr_len + fcs;
    return (unsigned int)(bytes * 8 * 1000000 / rf_datarate);
}

static void rf_phy_timing_update(void)
{
    if (rf_phy_legacy)  // as before the settings, whole µs per byte
        MAX_PACKET_SENDING_TIME = (8000000 / rf_datarate) * MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE +
                                  PACKET_SENDING_EXTRA_TIME;
    else                // the longest frame, from the longest SHR a frame header can carry
        MAX_PACKET_SENDING_TIME = rf_airtime_us(MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE, UINT8_MAX, 4) +
                                  PACKET_SENDING_EXTRA_TIME;
}

static unsigned int rf_phy_env(const char *name, unsigned int def, unsigned int min, unsigned int max)
{
    const char *env = getenv(name);
    unsigned long v;
    char *end;

    if (!env || !*env)
        return def;
    v = strtoul(env, &end, 0);
    FATAL_ON(*end || v < min || v > max, 2, "%s=%s: expected %u to %u", name, env, min, max);
    return v;
}

static void rf_phy_settings_init(void)
{
    rf_phy_legacy = rf_phy_env("WSSIM_PHY_LEGACY", 0, 0, 1);
    rf_datarate_fixed = rf_phy_env("WSSIM_PHY_DATARATE", 0, 1000, 10000000);
    rf_preamble_len = rf_phy_env("WSSIM_PHY_PREAMBLE", 8, 0, 255);
    rf_sfd_len = rf_phy_env("WSSIM_PHY_SFD", 2, 0, 8);
    rf_phr_len = rf_phy_env("WSSIM_PHY_PHR", 2, 0, 8);
    rf_fcs_len = rf_phy_env("WSSIM_PHY_FCS", 4, 2, 4);
    FATAL_ON(rf_fcs_len == 3, 2, "WSSIM_PHY_FCS=3: expected 2 or 4");
    FATAL_ON(rf_phy_legacy && rf_datarate_fixed, 2, "WSSIM_PHY_LEGACY and WSSIM_PHY_DATARATE together");
    FATAL_ON(rf_preamble_len + rf_sfd_len > 255, 2, "WSSIM_PHY_PREAMBLE + WSSIM_PHY_SFD above 255 bytes");
    rf_datarate = rf_datarate_fixed ? rf_datarate_fixed : phy_subghz.datarate;
    rf_phy_timing_update();
    if (rf_phy_legacy)
        INFO("PHY: legacy, %u bit/s, airtime of the PSDU only", rf_datarate);
    else
        INFO("PHY: %u bit/s%s, preamble %u B, SFD %u B, PHR %u B, FCS %u B", rf_datarate,
             rf_datarate_fixed ? " (WSSIM_PHY_DATARATE)" : " until the MAC configures it",
             rf_preamble_len, rf_sfd_len, rf_phr_len, rf_fcs_len);
}

// The MAC's PHY configuration (PHY mode): its data rate, unless fixed.
static void rf_phy_configure(const phy_rf_channel_configuration_s *config)
{
    if (rf_phy_legacy || rf_datarate_fixed || !config->datarate || config->datarate == rf_datarate)
        return;
    rf_datarate = config->datarate;
    rf_phy_timing_update();
    INFO("PHY: %u bit/s from the MAC's configuration", rf_datarate);
}

/*----------------------POWER (virtual time) -------------------------------------------*/
#ifdef NS_PORT_SIM
// Power recording (docs/POWER_MODEL.md §3): when the conductor asks for it,
// every change of the radio's hardware state goes to its power ledger as an
// "xr" (sim_proto.h). The driver's states map to the hardware's: a CSMA
// backoff listens, and the driver has no off state yet (GS8 stage 1).
static int8_t rf_tx_dbm;                // last TX power the MAC asked for
static int rf_power_last = -1;          // last state reported
static int8_t rf_power_last_dbm;

static void rf_power_send(uint8_t state, uint8_t events)
{
    struct sim_radio_report r = {
        .magic = "xr",
        .state = state,
        .tx_dbm = rf_tx_dbm,
        .events = events,
        .channel = channel,
    };
    int ret;

    ret = write(g_rf_host.rf_fd, &r, sizeof(r));
    FATAL_ON(ret != sizeof(r), 2, "RF socket: %m");
}

static uint8_t rf_power_state(void)
{
    switch (driver_state) {
    case DRVSTATE_RADIO_UNINIT:
    case DRVSTATE_RADIO_INITING:
    case DRVSTATE_OFF:
        return SIM_RADIO_OFF;
    case DRVSTATE_IDLE:
        return SIM_RADIO_IDLE;
    case DRVSTATE_IDLE_WAITING_RX:
    case DRVSTATE_CSMA_STARTED:
        return SIM_RADIO_LISTEN;
    case DRVSTATE_RX:
        return SIM_RADIO_RECEIVE;
    case DRVSTATE_TX:
        return SIM_RADIO_TX;
    }
    return SIM_RADIO_IDLE;
}

// Called wherever the driver's state may have changed (set_shm_cca_state())
static void rf_power_report(void)
{
    uint8_t state;

    if (!ns_port_sim_power() || g_rf_host.rf_fd < 0)
        return;
    state = rf_power_state();
    if (state == rf_power_last && (state != SIM_RADIO_TX || rf_tx_dbm == rf_power_last_dbm))
        return;
    rf_power_last = state;
    rf_power_last_dbm = rf_tx_dbm;
    rf_power_send(state, 0);
}

// A CCA, which the driver does in no time: the ledger charges it at the end
// of the listening that precedes it.
static void rf_power_cca(void)
{
    if (!ns_port_sim_power() || g_rf_host.rf_fd < 0)
        return;
    rf_power_send(rf_power_state(), SIM_RADIO_EV_CCA);
}
#endif

/*----------------------SHM-------------------------------------------------------------*/
void set_shm_cca_state()
{
    int pos = g_rf_host.node_id * 3;
    memcpy(shm_ptr+pos, (uint8_t *)&driver_state, 1);
    memcpy(shm_ptr+pos+1, &channel, 2);
#ifdef NS_PORT_SIM
    rf_power_report();
#endif
}

/*----------------------RADIO ----------------------------------------------------------*/
/**
 * \brief This function is used by the network stack library to set the interface state:
 *
 * \param new_state An interface state: PHY_INTERFACE_RESET, PHY_INTERFACE_DOWN,
 *                  PHY_INTERFACE_UP or PHY_INTERFACE_RX_ENERGY_STATE.
 *
 * \param channel An RF channel that the command applies to.
 *
 * \return 0 State update is OK.
 * \return -1 An unsupported state or a general failure.
 */
static int8_t phy_rf_state_control(phy_interface_state_e new_state, uint8_t rf_channel)
{
    tr_info("%s %d", __func__, new_state);
    if (new_state == PHY_INTERFACE_DOWN && rf_off_state_enabled) {
        // The channel updates of the driver before it had an off state, so
        // that FFN runs keep the same traces (the MAC calls DOWN with channel
        // 0, then UP, when it starts); then off.
        _rf_check_and_change_channel(rf_channel);
        if (driver_state != DRVSTATE_TX)
            _rf_check_and_change_channel(new_channel);
        _rf_set_off();
        return 0;
    }
    rf_off_pending = false;
    if (driver_state == DRVSTATE_OFF) {
        driver_state = DRVSTATE_IDLE;
        set_shm_cca_state();
    }
    _rf_check_and_change_channel(rf_channel);
    _rf_set_rx();
    return 0;
}

void write_pcap(uint8_t *buf, int len)
{
#ifdef HAVE_LIBPCAP
    struct pcap_pkthdr pcap_hdr;

    if (g_rf_host.pcap_dumper) {
        gettimeofday(&pcap_hdr.ts, NULL);
        pcap_hdr.caplen = len;
        pcap_hdr.len = len;
        pcap_dump((uint8_t *)g_rf_host.pcap_dumper, &pcap_hdr, buf);
        pcap_dump_flush(g_rf_host.pcap_dumper);
    }
#endif
}

// splitmix64: a small generator with a state of its own, so that the losses
// of each node can be replayed from a seed.
static uint64_t rf_rng_next(void)
{
    uint64_t z = (rf_rng_state += 0x9e3779b97f4a7c15ULL);

    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
    return z ^ (z >> 31);
}

static double rf_rng_uniform(void)
{
    return (rf_rng_next() >> 11) * 0x1.0p-53;
}

// The base seed comes from WSSIM_SEED, or else from the clock; in virtual
// time, from the conductor. Each node draws from base + node ID, and logs both
// so a run can be replayed.
static void rf_rng_seed(int node_id)
{
    uint64_t base;
#ifdef NS_PORT_SIM
    base = ns_port_sim_seed();
#else
    const char *env = getenv("WSSIM_SEED");

    if (env) {
        base = strtoull(env, NULL, 0);
    } else {
        struct timespec tp;

        clock_gettime(CLOCK_REALTIME, &tp);
        base = (uint64_t)tp.tv_sec * 1000000000 + tp.tv_nsec;
    }
#endif
    rf_rng_state = base + node_id;
    INFO("loss model: WSSIM_SEED=%llu, node %d", (unsigned long long)base, node_id);
}

int rf_sim_connect(const char *path, const uint8_t eui64[8])
{
    struct sockaddr_un addr = {
        .sun_family = AF_UNIX
    };
    struct sim_hello hello = { .magic = "xh" };
    int fd;
    int ret;

    g_rf_host.node_id = sim_node_id(eui64);
    FATAL_ON(g_rf_host.node_id >= SIM_MAX_NODES, 2, "node ID %d (from the EUI-64) is above %d",
             g_rf_host.node_id, SIM_MAX_NODES - 1);
    FATAL_ON(strlen(path) >= sizeof(addr.sun_path), 2, "socket path too long: %s", path);
    strcpy(addr.sun_path, path);
    strcpy(sim_socket_path, path);
    fd = socket(AF_UNIX, SOCK_SEQPACKET, 0); // use SOCK_SEQPACKET or SOCK_STREAM
    FATAL_ON(fd < 0, 2, "socket %s: %m", path);
    ret = connect(fd, (struct sockaddr *)&addr, sizeof(addr));
    FATAL_ON(ret < 0, 2, "connect %s: %m", path);
    // The server numbers nodes by this ID
    hello.node_id = g_rf_host.node_id;
#ifdef NS_PORT_SIM
    hello.flags = SIM_HELLO_VTIME;
#endif
    ret = write(fd, &hello, sizeof(hello));
    FATAL_ON(ret != sizeof(hello), 2, "write %s: %m", path);
#ifdef NS_PORT_SIM
    // Virtual time: this socket is also the conductor's (docs/VIRTUAL_TIME.md)
    ns_port_sim_attach(fd, g_rf_host.node_id);
#endif
    rf_rng_seed(g_rf_host.node_id);
    return fd;
}

bool fhss_on = false;

// Poll hook of the run loop: FHSS is on once the stack has synchronised its
// hopping, and stays on (fhss_on is never cleared).
static void rf_fhss_status_poll(void)
{
    if (fhss_on || !fhss_synch_global_state_get())
        return;
    fhss_on = true;
    WARN("FHSS : on");
}

//int dbg_i_rx = 0;
void phy_rf_rx_now(void)
{
    //WARN("RX_Counter %d", dbg_i_rx++);
    static char trace_buffer[128];
    static uint8_t msg[sizeof(struct sim_frame_hdr) + MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE];
    struct sim_frame_hdr *hdr = (struct sim_frame_hdr *)msg;
    uint8_t *buf = msg + sizeof(*hdr);
    uint16_t pkt_len;
    int pkt_chan;
    int len;
    int pkt_seq;
    len = read(g_rf_host.rf_fd, msg, sizeof(msg));
    FATAL_ON(!len, 2, "RF server has gone");
    FATAL_ON(len < 0, 2, "RF socket: %m");
    if (len < (int)sizeof(*hdr) || memcmp(hdr->magic, "xx", 2) || hdr->len != len - sizeof(*hdr)) {
        TRACE(TR_RF, " rf drop: chan=%2d/%2d %s", -1, channel,
            bytes_str(msg, len, NULL, trace_buffer, sizeof(trace_buffer), DELIM_SPACE | ELLIPSIS_STAR));
        goto CANCEL_RX;
    }
    if (hdr->fcs_len != 2 && hdr->fcs_len != 4) {
        WARN("rf drop: FCS of %u bytes from node %u", hdr->fcs_len, hdr->src);
        goto CANCEL_RX;
    }
    pkt_len = hdr->len;
    pkt_chan = hdr->chan;
    pkt_seq = hdr->seq;
    if (driver_state == DRVSTATE_OFF) {
        rf_frames_lost_off++;
        INFO("rf rx lost: radio off, chan=%d len=%d rxseq=%d (%lu lost)", pkt_chan, pkt_len, pkt_seq,
             rf_frames_lost_off);
        goto CANCEL_RX;
    }
    if ((channel != pkt_chan) && !fhss_on) {
        WARN("YET FHSS OFF & WRONG CHANNEL ... rf rx: chan=%d, curr chan=%d len=%d rxseq=%d", pkt_chan, channel, pkt_len, pkt_seq);
    }
    if(fhss_on) {
        if (channel != pkt_chan) {
            WARN("FHSS ON & WRONG CHANNEL ... rf rx: chan=%d, curr chan=%d len=%d rxseq=%d ts=%u", pkt_chan, channel, pkt_len, pkt_seq, rf_get_timestamp());
            goto CANCEL_RX;
        }
    }
    len = pkt_len;

    // Simulate RSSI: the link from the sender, whose ID the server stamped
    float rssi = sim_link(nodes_infos_flat_map, g_rf_host.node_id, hdr->src)->rssi;

    // T = 293 [K], B = 100 [kHz], R_b = 100 [kbit/s], NF_LNA = 4 [dB]
    // $BER(P_r) = \frac{1}{2} e^{\frac{1}{2} (10 \cdot \log_{10}(kTB) + 30 + NF_{LNA} + 10\cdot \log_{10}​(\frac{B}{R_b} ) - P_{r})}$
    double ber = 0.5 * exp(0.5 * (-153.930645 + 30 + 4 + 0 - rssi));
    if (ber > 0.5)
        ber = 0.5;
    // 1.0 -> -12dBm, 0.6 -> -99dBm
    // The frame is lost if any of its bits is: 1 - (1 - BER)^bits
    int bits = 8 * (rf_phr_len + pkt_len + hdr->fcs_len);
    double frame_error_rate = -expm1(bits * log1p(-ber));

    if (frame_error_rate > rf_rng_uniform()) {
        printf("Packet from node %d was lost due to link quality (RSSI %g dBm, FER = %g)\n", hdr->src, rssi, frame_error_rate);
        goto CANCEL_RX;
    }

    // Handle the received packet
    TRACE(TR_RF, "   rf rx: chan=%2d/%2d %s (%d bytes)", pkt_chan, channel,
        bytes_str(buf, len, NULL, trace_buffer, sizeof(trace_buffer), DELIM_SPACE | ELLIPSIS_STAR), pkt_len);
    // Flush data from the socket if RX is blocked
    if(!rx_thread_blokcked) {
/*         if(!eventOS_callback_timer_expired_usec(rx_sleep_handler_timer)) {
            WARN("Another RX in progress ... Abort");
            goto CANCEL_RX;
        } */
        if (driver_state == DRVSTATE_RX) {
            WARN("Another RX in progress (Hiden Terminal)... Abort");
            goto CANCEL_RX;
        }
        driver_state = DRVSTATE_RX;
        set_shm_cca_state();
        _rf_start_backup_timer(MAX_PACKET_SENDING_TIME);
        __PRINT(31, "Receiving ... pkt_chan=%d, curr_chan=%d, pkt_len=%u, rxseq=%d ts=%u", pkt_chan, channel, pkt_len, pkt_seq, rf_get_timestamp());

        // write in the rx_ring_buffer temporary
        int16_t input_index = ringbufindex_peek_put(&input_ringbuf);
        if (input_index == -1) {
            WARN("RX ring buffer full");
            driver_state = DRVSTATE_IDLE_WAITING_RX;
            set_shm_cca_state();
            goto CANCEL_RX;
        }
        static struct input_packet *current_input;
        current_input = &input_array[input_index];
        // For now only save packet sync word detect time
        current_input->rx_time = rf_get_timestamp();
        // Save the rest of packet in buffer
        current_input->len = pkt_len;
        current_input->channel = pkt_chan;
        current_input->rssi = rssi;
        memcpy(current_input->payload, buf, pkt_len);
        // Store it in the ringbuf
        ringbufindex_put(&input_ringbuf);
        // Now signal the management thread
        // Wait for the whole packet to be received
        unsigned int sleep_time = rf_airtime_us(pkt_len, hdr->shr_len, hdr->fcs_len);
        _rf_rx_sleep_timeout_timer(sleep_time);
    }
CANCEL_RX:
    return;
}

/**
 * \brief This function is used give driver data to transfer.
 *
 * \param data_ptr A pointer to TX data. The platform driver can use the same pointer, but the
 *                 network stack will free the memory when the device driver implementation
 *                 notifies the stack (using the unique tx_handle) that it is allowed to do so.
 *
 * \param data_len The length of data behind a pointer.
 *
 * \param tx_handle A unique TX handle defined by the network stack.
 *
 * \return 0 TX process start is OK. The library must wait for the TX Done callback
 *           before pushing a new packet.
 * \return 1 TX process is OK at the Ethernet side (fast TX phase).
 *
 * \return -1 PHY is busy.
 *
 */
uint16_t phy_seq = 0;
static int8_t phy_rf_tx_now()
{
    static char trace_buffer[128];
    static uint8_t msg[sizeof(struct sim_frame_hdr) + MAC_IEEE_802_15_4G_MAX_PHY_PACKET_SIZE];
    struct sim_frame_hdr *hdr = (struct sim_frame_hdr *)msg;
    int ret;

    // Get the next packet to transmit and remore it from ring buffer
    int16_t output_index = ringbufindex_get(&output_ringbuf);
    if (output_index == -1) {
        return -1;
    }
    struct output_packet *current_output = &output_array[output_index];
    uint8_t *data_ptr = current_output->data;
    uint16_t data_len = current_output->data_len;

    // One message: a header (the server fills in src), then the frame
    memcpy(hdr->magic, "xx", 2);
    hdr->len = data_len;
    hdr->chan = channel;
    hdr->seq = phy_seq;
    hdr->src = 0;
    hdr->shr_len = rf_preamble_len + rf_sfd_len;
    hdr->fcs_len = rf_fcs_len;
    memcpy(msg + sizeof(*hdr), data_ptr, data_len);
    TRACE(TR_RF, "   rf tx: chan=%2d/%2d %s (%d bytes)", channel, channel,
           bytes_str(data_ptr, data_len, NULL, trace_buffer, sizeof(trace_buffer), DELIM_SPACE | ELLIPSIS_STAR), data_len);
    __PRINT(35, "Transmitting ... chan=%d, len=%u, txseq=%d ts=%u", channel, data_len, phy_seq, rf_get_timestamp());
    phy_seq++;
    ret = write(g_rf_host.rf_fd, msg, sizeof(*hdr) + data_len);
    FATAL_ON(ret != sizeof(*hdr) + data_len, 2, "RF socket: %m");
    write_pcap(data_ptr, data_len);
    return 0;
}

static void _rf_handle_rx_end(){
    _rf_stop_backup_timer();

    int16_t buffed_index = ringbufindex_get(&input_ringbuf);
    if (buffed_index == -1) {
        return;
    }
    struct input_packet *buffed_packet = &input_array[buffed_index];
    uint8_t *buf = buffed_packet->payload;
    int len = buffed_packet->len;
    //int pkt_chan = buffed_packet->channel;
    //_rf_check_and_change_channel(pkt_chan);
    packet_rxsync_time = buffed_packet->rx_time;
    driver_state = DRVSTATE_IDLE_WAITING_RX;
    set_shm_cca_state();
    if (device_driver.phy_rx_cb)
        device_driver.phy_rx_cb(buf, len, 200, buffed_packet->rssi, rf_radio_driver_id);
}

/**
 * @brief Changes the active channel of the radio with state conflict avoidance
 *
 * @param[in] new_channel New channel to switch to
 * @return 0 on success, -1 on failure (channel not changed because radio is
 * emitting or receiving a packet)
 */
static int _rf_check_and_change_channel(uint16_t new_channel){
    if (driver_state == DRVSTATE_TX || driver_state == DRVSTATE_RX){
        return -1;
    }
    if (new_channel != channel && new_channel != -1) {
        channel = new_channel;
    }
    __PRINT(32, "Channel changed to %d", channel);
    return 0;
}

/**
 * @brief Sets the to receive mode (with state verification).
 *
 * @return 0 on success, > 0 on failure.
 *
 * @note The return value can be casted to @ref RAIL_Status_t to get the exact error code.
 */
static int _rf_set_rx(){
    if (driver_state == DRVSTATE_TX) {
        return -1;
    }
    if (driver_state == DRVSTATE_OFF) {
        // Only the channel: a channel change does not turn the radio on
        _rf_check_and_change_channel(new_channel);
        return 0;
    }

    /*Set channel*/
    _rf_check_and_change_channel(new_channel);

    if (driver_state != DRVSTATE_CSMA_STARTED) {
        driver_state = DRVSTATE_IDLE_WAITING_RX;
        set_shm_cca_state();
    }
    return 1;
}

/**
 * @brief Sets the radio to idle state.
 *
 * @param[in] mode Specifies if the radio must abort ongoing operation (TRX) or must shutdown (see @ref idle_state).
 */
static void _rf_set_idle(enum idle_state mode){
    return;
}

/**
 * @brief Turns the radio off (PHY_INTERFACE_DOWN, LFN study E1).
 *
 * A frame being received is lost; a CSMA backoff is cancelled (the MAC that
 * asks for this has given up its transmission). A frame being sent is on the
 * air already: the radio turns off at its end.
 */
static void _rf_set_off(void){
    switch (driver_state) {
    case DRVSTATE_OFF:
        return;
    case DRVSTATE_TX:
        rf_off_pending = true;
        return;
    case DRVSTATE_RX:
        eventOS_callback_timer_stop(rx_sleep_handler_timer);
        _rf_stop_backup_timer();
        ringbufindex_flush(&input_ringbuf);
        break;
    case DRVSTATE_CSMA_STARTED:
        _rf_stop_csma_timeout_timer();
        break;
    default:
        break;
    }
    driver_state = DRVSTATE_OFF;
    set_shm_cca_state();
}


static void _rf_tx_sleep_timeout_timer_cb(int timer_id, uint16_t slots)
{
    _rf_stop_backup_timer();
    if (device_driver.phy_tx_done_cb)
                device_driver.phy_tx_done_cb(rf_radio_driver_id,
                                                        mac_tx_handle, PHY_LINK_TX_SUCCESS, 0, 0);
    rx_thread_blokcked = false;
    driver_state = rf_off_pending ? DRVSTATE_OFF : DRVSTATE_IDLE_WAITING_RX;
    rf_off_pending = false;
    set_shm_cca_state();
    //_rf_set_rx();
}

static void _rf_rx_sleep_timeout_timer_cb(int timer_id, uint16_t slots)
{
    _rf_handle_rx_end();
    return;
}

static void _rf_rx_sleep_timeout_timer(uint32_t duration){
    INFO("SIG RX in %u us", duration);
    eventOS_callback_timer_start_usec(rx_sleep_handler_timer, duration);
}

/**
 * @brief Checks if the current channel is clear to send.
 *
 * @return true if the channel is clear to send, false otherwise.
 */
static bool _rf_check_cca(){
    if(!cca_enabled){
        return true;
    }
    if(!_rf_set_rx()){
        WARN("Could not start RX mode");
    }
#ifdef NS_PORT_SIM
    rf_power_cca();
#endif
    struct neighbor_timings *neighbors = g_rf_host.neighbors;
    int i;

    for (i = 0; i < g_rf_host.neighbors_count; i++) {
        if(neighbors[i].eui64[1] != 0) {
            WARN("neigh %d: mac[last]: %d",i, neighbors[i].eui64[7]);
            /* uint8_t * cca_res = rf_request_rf_state_dbus(&neighbors[i].eui64[0]);
            if (cca_res != NULL) {
                if(cca_res[0] == DRVSTATE_TX && (uint16_t)cca_res[1] == channel) {
                    __PRINT(36,"CCA: CANNEL BUSY");
                    driver_state = DRVSTATE_IDLE_WAITING_RX;
                    return false;
                }
            } */
            // using shm
            int neigh_id = sim_node_id(neighbors[i].eui64);
            if (neigh_id >= SIM_MAX_NODES)
                continue;
            int des_pos_shm = neigh_id * 3;
            uint8_t tmp_st = *(uint8_t*)(shm_ptr+des_pos_shm);
            uint16_t tmp_ch = *(uint16_t*)(shm_ptr+des_pos_shm+1);
            WARN("tmp_st %d, tmp_ch: %d", tmp_st, tmp_ch);
            if(tmp_st == DRVSTATE_TX && tmp_ch == channel)
            {
                    __PRINT(36,"CCA: CANNEL BUSY");
                    driver_state = DRVSTATE_IDLE_WAITING_RX;
                    set_shm_cca_state();
                    return false;
            }
        }
    }
    return true;
}


/**
 * @brief Sends the packet stored inside the TX fifo.
 *
 * @return 0 on success, > 0 on failure.
 *
 * This function is non-blocking. It only sets the radio to TX mode. The end of transmission is handled
 * by the radio IRQ handler @ref _rf_rail_events_irq_handler with the RAIL_EVENTS_TX_COMPLETION flag.
 *
 * @note The return value can be casted to @ref RAIL_Status_t to get the exact error code.
 */
static int _rf_execute_tx(){
    // TX FIFO must be pre-filled with TX data

    driver_state = DRVSTATE_TX;
    set_shm_cca_state();

    _rf_set_idle(RADIO_IDLE_ABORT);

    // block the RX thread
    rx_thread_blokcked = true;
    // Get the next packet to transmit but do not remove it yet from ring buffer
    int16_t output_index = ringbufindex_peek_get(&output_ringbuf);
    if (output_index == -1) {
        return -1;
    }
    struct output_packet *current_output = &output_array[output_index];
    uint16_t data_len = current_output->data_len;

    unsigned int sleep_time = rf_airtime_us(data_len, rf_preamble_len + rf_sfd_len, rf_fcs_len); // tx duration
    // Do tx now and signal to do callback later after sleep
    phy_rf_tx_now();
    INFO("SIG TX in %u us for len %d", sleep_time, data_len);
    // Set the TX sleep timeout timer
    eventOS_callback_timer_start_usec(tx_sleep_handler_timer, sleep_time);

    _rf_start_backup_timer(MAX_PACKET_SENDING_TIME);

    return 0;
}

/**
 * @brief CSMA-CA timeout handler called by the IRQ thread task
 *
 * This function tells to the MAC layer that the CSMA-CA timeout has occurred.
 * The MAC layer is responsible to tell what to do next. Depending on the status
 * that the MAC layer returns, this function will either cancel the transmission,
 * start the transmission or start the CSMA-CA procedure again.
 */
static void _rf_csma_timeout_handler(){
    int8_t status = -1;

    // Request CSMA status from Nanostack
    if (device_driver.phy_tx_done_cb)
        status = device_driver.phy_tx_done_cb(rf_radio_driver_id, mac_tx_handle, PHY_LINK_CCA_PREPARE, 0, 0);

    switch (status){
    case PHY_TX_NOT_ALLOWED:
        __PRINT(92,"CSMA-CA PHY_TX_NOT_ALLOWED");
        if (driver_state == DRVSTATE_CSMA_STARTED) {
            _rf_set_idle(RADIO_IDLE_ABORT);
            driver_state = DRVSTATE_IDLE;
            set_shm_cca_state();
            _rf_set_rx();
        }
        break;
    case PHY_RESTART_CSMA:
        __PRINT(32,"CSMA-CA PHY_RESTART_CSMA");
        if (driver_state == DRVSTATE_RX){
            if (device_driver.phy_tx_done_cb)
                device_driver.phy_tx_done_cb(rf_radio_driver_id, mac_tx_handle, PHY_LINK_CCA_FAIL_RX, 0, 0);
        } else if (!_rf_check_cca()) {
            // Channel is busy
            WARN("Channel is busy");
            if (device_driver.phy_tx_done_cb)
                device_driver.phy_tx_done_cb(rf_radio_driver_id, mac_tx_handle, PHY_LINK_CCA_FAIL, 0, 0);
        } else {
            // Channel is clear
            if (device_driver.phy_tx_done_cb)
                device_driver.phy_tx_done_cb(rf_radio_driver_id, mac_tx_handle, PHY_LINK_CCA_OK, 0, 0);

            if (backoff_time > 0) {
                _rf_rearm_csma_timeout_timer();
            }
        }
        break;
    case PHY_TX_ALLOWED:
        __PRINT(32,"CSMA-CA PHY_TX_ALLOWED");
        if (driver_state == DRVSTATE_RX){
            if (device_driver.phy_tx_done_cb)
                device_driver.phy_tx_done_cb(rf_radio_driver_id, mac_tx_handle, PHY_LINK_CCA_FAIL_RX, 0, 0);
        } else if(!_rf_check_cca()){
            // Channel is busy
            WARN("Channel is busy");
            if (device_driver.phy_tx_done_cb)
                device_driver.phy_tx_done_cb(rf_radio_driver_id,
                                                                mac_tx_handle, PHY_LINK_CCA_FAIL, 0, 0);
        } else {
            // Channel is clear -> start transmission immediately
            if (_rf_execute_tx() != 0){
                WARN("TX fail");
                if (device_driver.phy_tx_done_cb)
                    device_driver.phy_tx_done_cb(rf_radio_driver_id,
                                                                mac_tx_handle, PHY_LINK_TX_FAIL, 0, 0);
            }
            // TX success is called in _rf_rail_events_irq_handler()
        }
        break;
    default:
        break;
    }
}

/**
 * @brief Starts the CSMA-CA timer
 *
 * @param[in] duration before triggering the CSMA-CA timeout callback
 */
static void _rf_start_csma_timeout_timer(uint32_t duration){
    WARN("CSMA-CA timeout in %u us", duration);
    eventOS_callback_timer_start_usec(csma_timeout_timer, duration);
}

/**
 * @brief Stops the CSMA-CA timer
 */
static void _rf_stop_csma_timeout_timer(){
    //WARN("CSMA-CA timeout stopped");
    eventOS_callback_timer_stop(csma_timeout_timer);
}

/**
 * @brief CSMA-CA timeout callback
 */
static void _rf_csma_timeout_timer_cb(int timer_id, uint16_t slots){
    _rf_csma_timeout_handler();
}

/**
 * @brief Starts/restarts the CSMA-CA timer based on the requested backoff time *
 */
static void _rf_rearm_csma_timeout_timer(){
    uint32_t csma_ca_period = backoff_time - rf_get_timestamp();

    if(csma_ca_period > 65000){
        // Time has already passed, trigger the IRQ immediately for immediate TX
        csma_ca_period = 1;
    }

    _rf_start_csma_timeout_timer(csma_ca_period);
}


/**
 * @brief Starts the backup timer
 *
 * @param[in] duration before triggering the backup timeout callback
 *
 * The backup timer is responsible to put the radio in a known state in case of unexpected radio failure.
 */
static void _rf_start_backup_timer(uint32_t duration){
    eventOS_callback_timer_start_usec(backup_timeout_timer,duration);
}

/**
 * @brief Stops the backup timer
 */
static void _rf_stop_backup_timer(){
    eventOS_callback_timer_stop(backup_timeout_timer);
}

/**
 * @brief Backup timeout callback
 */
static void _rf_backup_timer_cb(int timer_id, uint16_t slots){
    _rf_backup_timeout_handler();
}

/**
 * @brief Backup timeout handler called by the IRQ thread task
 */
static void _rf_backup_timeout_handler(){
    if (driver_state == DRVSTATE_TX) {
        // Radio is stuck in TX state, reset it
        if (device_driver.phy_tx_done_cb) {
            device_driver.phy_tx_done_cb(rf_radio_driver_id, mac_tx_handle, PHY_LINK_TX_FAIL, 0, 0);
        }
        driver_state = rf_off_pending ? DRVSTATE_OFF : DRVSTATE_IDLE;
        rf_off_pending = false;
        set_shm_cca_state();
        _rf_set_idle(RADIO_IDLE_ABORT);
        _rf_set_rx();
    } else if (driver_state == DRVSTATE_RX){
        driver_state = DRVSTATE_IDLE_WAITING_RX;
        set_shm_cca_state();
    }
}


/**
 * @brief Checks if the radio is busy (i.e. transmitting or receiving a packet).
 *
 * @return true if the radio is busy, false otherwise.
 */
static bool _rf_is_busy(){
    if (driver_state == DRVSTATE_IDLE || driver_state == DRVSTATE_IDLE_WAITING_RX){
        return false;
    } else {
        return true;
    }
}


//int dbg_i_tx = 0;
static int8_t phy_rf_tx(uint8_t *data_ptr, uint16_t data_len, uint8_t tx_handle, data_protocol_e protocol) {
    //WARN("phy_rf_tx %d", dbg_i_tx++);
    /*Check if transmitter is busy*/
    if(_rf_is_busy()) {
        WARN("RF is busy");
        return -1;
    }
    WARN("RF is not busy, bft %u", backoff_time);

    //platform_enter_critical();
    mac_tx_handle = tx_handle;
    driver_state = DRVSTATE_CSMA_STARTED;
    set_shm_cca_state();
    // flush, then transfer data to outgoing fifo ring buffer
    ringbufindex_flush(&output_ringbuf);
    int16_t tx_index = ringbufindex_peek_put(&output_ringbuf);
    if (tx_index == -1) {
        WARN("RX ring buffer full");
        return -1;
    }
    static struct output_packet *current_tx;
    current_tx = &output_array[tx_index];
    current_tx->data_len = data_len;
    current_tx->protocol = protocol;
    memcpy(current_tx->data , data_ptr, data_len);
    ringbufindex_put(&output_ringbuf);

    if (backoff_time > 0) {
        _rf_rearm_csma_timeout_timer();
    } else {
        // If there is no backoff time, we can start transmitting immediately
        _rf_start_csma_timeout_timer(1);
        WARN("Start TX immediately");
    }
    //platform_exit_critical();
    return 0;
}


static void phy_rf_mlme_orserver_tx(const mlme_set_t *set_req)
{
    return;
}

/*----------------------DBUS ----------------------------------------------------------*/

static int rf_dbus_get_rf_status (sd_bus *bus, const char *path, const char *interface,
                       const char *property, sd_bus_message *reply,
                        void *userdata, sd_bus_error *ret_error)
{
    uint8_t rf_and_ch[3] = {0};
    rf_and_ch[0] = driver_state; // Holds TX state
    memcpy(rf_and_ch + 1, &channel, 2); // Holds channel
    int ret = sd_bus_message_append_array(reply, 'y', &rf_and_ch, 3);
    if (ret < 0) {
        WARN("Failed to append rf_state: %s", strerror(-ret));
        return ret;
    }
    return 0;
}

static const sd_bus_vtable rf_dbus_vtable[] = {
        SD_BUS_VTABLE_START(0),
        SD_BUS_PROPERTY("RfState", "ay", rf_dbus_get_rf_status, 0, 0),
        SD_BUS_VTABLE_END
};

int rf_dbus_register () {
    int ret = sd_bus_open_user(&rf_dbus);
    if (ret < 0) {
        WARN_ON(1, "Failed to connect to system bus: %s", strerror(-ret));
        return ret;
    }

    char dbus_name[38];
    snprintf(dbus_name,38, "com.wshwsim.mac%02x%02x%02x%02x%02x%02x%02x%02x",
        rf_mac_address[0], rf_mac_address[1], rf_mac_address[2], rf_mac_address[3],
        rf_mac_address[4], rf_mac_address[5], rf_mac_address[6], rf_mac_address[7]);
    WARN("Registering to DBus as %s", dbus_name);
    char dbus_path[39];
    snprintf(dbus_path,39, "/com/wshwsim/Mac%02x%02x%02x%02x%02x%02x%02x%02x",
        rf_mac_address[0], rf_mac_address[1], rf_mac_address[2], rf_mac_address[3],
        rf_mac_address[4], rf_mac_address[5], rf_mac_address[6], rf_mac_address[7]);

    ret = sd_bus_add_object_vtable(rf_dbus, NULL, dbus_path, dbus_name, rf_dbus_vtable, NULL);
    if (ret < 0) {
        WARN_ON(1,"Failed to add object: %s", strerror(-ret));
        return ret;
    }

    ret = sd_bus_request_name(rf_dbus, dbus_name, SD_BUS_NAME_ALLOW_REPLACEMENT | SD_BUS_NAME_REPLACE_EXISTING);
    if (ret < 0) {
        WARN_ON(1,"Failed to acquire service name: %s", strerror(-ret));
        return ret;
    }
    INFO("Successfully registered to User DBus");

    return 0;
}

void * rf_dbus_process () {
    INFO("Starting DBus processing thread");
    int fd = sd_bus_get_fd(rf_dbus);
    if (fd < 0) {
        WARN_ON(1, "Failed to get bus fd: %s", strerror(-fd));
    }
    fd_set fds;
    FD_ZERO(&fds);
    while (1)
    {
        FD_SET(fd, &fds);
        int ret = pselect(fd+1, &fds, NULL, NULL, NULL, NULL);
        if (ret < 0) {
            WARN_ON(1,"Failed to pselect: %s", strerror(-ret));
        }
        if(FD_ISSET(fd, &fds)) {
            //INFO("Processing DBus");
            ret = sd_bus_process(rf_dbus, NULL);
            if (ret < 0) {
                WARN_ON(1, "Failed to process bus: %s", strerror(-ret));
            }
        }
        /* sd_bus_wait(rf_dbus, (uint64_t) -1);
        int ret = sd_bus_process(rf_dbus, NULL);
        if (ret < 0) {
            WARN_ON(1, "Failed to process bus: %s", strerror(-ret));
        } */
    }
    return NULL;
}

static  uint8_t * rf_request_rf_state_dbus(uint8_t *dest_mac_addr) {
    // Construct the service name, interface and path from the mac address of the given neighbor node
    static char dbus_name[38];
    snprintf(dbus_name,38, "com.wshwsim.mac%02x%02x%02x%02x%02x%02x%02x%02x",
        *dest_mac_addr, *(dest_mac_addr+1), *(dest_mac_addr+2), *(dest_mac_addr+3),
        *(dest_mac_addr+4), *(dest_mac_addr+5), *(dest_mac_addr+6), *(dest_mac_addr+7));
    static char dbus_path[39];
    snprintf(dbus_path,39, "/com/wshwsim/Mac%02x%02x%02x%02x%02x%02x%02x%02x",
        *dest_mac_addr, *(dest_mac_addr+1), *(dest_mac_addr+2), *(dest_mac_addr+3),
        *(dest_mac_addr+4), *(dest_mac_addr+5), *(dest_mac_addr+6), *(dest_mac_addr+7));
    INFO("Sending message to s %s p %s", dbus_name, dbus_path);

    static sd_bus_message *reply_buf = NULL;
    int ret = -1;

    ret = sd_bus_is_open(rf_request_dbus);
    if (ret <= 0) {
        INFO("Opening DBus connection");
        ret = sd_bus_open_user(&rf_request_dbus);
        BUG_ON( ret < 0,"Failed to open a user dbus connection: %s", strerror(-ret));
    }
    ret = sd_bus_get_property(rf_request_dbus, dbus_name, dbus_path, dbus_name, "RfState", NULL, &reply_buf, "ay");
    if (ret < 0) {
        WARN("Failed to get requested property: %s", strerror(-ret));
        return NULL;
    }
    sd_bus_flush(rf_request_dbus);
    //sd_bus_close(rf_request_dbus);
    uint8_t *rep;
    size_t rep_len;
    ret = sd_bus_message_read_array(reply_buf, 'y', (const void **)&rep, &rep_len);
    if (ret < 0) {
        WARN("Failed to read/parse requested property: %s", strerror(-ret));
        return NULL;
    }

    INFO("reply is %u %u", *rep, (uint16_t)*(rep+1));
    sd_bus_message_unref(reply_buf);

    return rep;
}

/*----------------------DBUS & HELPERS ----------------------------------------------------------*/

/**
 * \brief This is the default PHY interface address write API for all interface types.
 *
 * \param address_type Defines the PHY address type: PHY_MAC_64BIT, PHY_MAC_48BIT,
 *                     PHY_MAC_PANID or PHY_MAC_16BIT.
 *
 * \param address_ptr A pointer to an address.
 *
 * \return 0 Write is OK.
 * \return -1 PHY is busy.
 */
static int8_t phy_rf_address_write(phy_address_type_e address_type, uint8_t *address_ptr)
{
    if (address_ptr) {
        switch (address_type) {
            case PHY_MAC_64BIT: {
                memcpy(rf_mac_address, address_ptr, 8);
                break;
            }
            default:
                break;
        }
    }
    // Now we have the MAC address, we can register to DBus. Not in virtual
    // time: its thread would answer at wall-clock times (step 3 brings it back).
#ifndef NS_PORT_SIM
    if (rf_dbus_registered == 0) {
        rf_dbus_registered = 1;
        if(rf_dbus_register() < 0) {
            BUG_ON(1, "Failed to register to DBus");
        }
        pthread_create(&rf_dbus_proces_thread, NULL, rf_dbus_process, NULL);
    }
#endif
    return 0;
}

static void ringbufindex_flush(struct ringbufindex *rbi)
{
    while (ringbufindex_get(rbi) >= 0);
}

/**
 * \brief This is the default PHY interface address write API for all interface types.
 *
 * \param extension_type Supported extension types: PHY_EXTENSION_CTRL_PENDING_BIT,
 *                       PHY_EXTENSION_SET_CHANNEL, PHY_EXTENSION_READ_CHANNEL_ENERGY
 *                       or PHY_EXTENSION_READ_LINK_STATUS.
 *
 * \param data_ptr A pointer to an 8-bit data storage for read or write purpose,
 *                 based on the extension command types.
 *
 * \return 0 State update is OK.
 * \return -1 An unsupported state or a general failure.
 */
static int8_t phy_rf_extension(phy_extension_type_e extension_type, uint8_t *data_ptr)
{
    uint32_t *data_ptr_32bits;
    phy_csma_params_t *csma_params;

    if (data_ptr) {
        switch (extension_type) {
            /*Control MAC pending bit for Indirect data transmission*/
            case PHY_EXTENSION_CTRL_PENDING_BIT: {
                data_request_pending_flag = *data_ptr;
                break;
            }
            /*Return frame pending status*/
            case PHY_EXTENSION_READ_LAST_ACK_PENDING_STATUS: {
                *data_ptr = data_request_pending_flag;
                break;
            }
            case PHY_EXTENSION_DYNAMIC_RF_SUPPORTED: {
                *data_ptr = true;
                break;
            }
            case PHY_EXTENSION_GET_SYMBOLS_PER_SECOND: {
                data_ptr_32bits = (uint32_t *)data_ptr;
                *data_ptr_32bits = rf_datarate;
                break;
            }
            case PHY_EXTENSION_SET_CCA_THRESHOLD: {
                break;
            }
            case PHY_EXTENSION_SET_CHANNEL_CCA_THRESHOLD: {
                break;
            }
            case PHY_EXTENSION_SET_TX_POWER: {
                INFO("change tx power to %u", *data_ptr);
#ifdef NS_PORT_SIM
                rf_tx_dbm = (int8_t)*data_ptr;
#endif
                break;
            }
            case PHY_EXTENSION_SET_RF_CONFIGURATION: {
                rf_phy_configure((const phy_rf_channel_configuration_s *)data_ptr);
                break;
            }
            case PHY_EXTENSION_SET_CSMA_PARAMETERS: {
                csma_params = (phy_csma_params_t *)data_ptr;
                //INFO("set csma parameters, %u", csma_params->backoff_time);
                if (csma_params->backoff_time == 0){
                    // Cancel any on-going transmission and set to RX state on current channel
                    _rf_stop_csma_timeout_timer();
                    _rf_set_idle(RADIO_IDLE_ABORT);
                    if (driver_state != DRVSTATE_OFF)
                        driver_state = DRVSTATE_IDLE;
                    set_shm_cca_state();
                    //_rf_set_rx();
                    backoff_time = 0;
                } else {
                    // Store backoff_time and cca_enabled
                    cca_enabled = csma_params->cca_enabled;
                    backoff_time = csma_params->backoff_time;
                }
                break;
            }
            case PHY_EXTENSION_SET_CHANNEL: {
                if (driver_state == DRVSTATE_IDLE ||
                    driver_state == DRVSTATE_IDLE_WAITING_RX ||
                    driver_state == DRVSTATE_CSMA_STARTED ||
                    driver_state == DRVSTATE_OFF){
                    new_channel = *data_ptr;
                    _rf_set_rx();
                } else {
                    // Store for later : channel could not be set yet
                    new_channel = *data_ptr;
                    return -1;
                }
                //__PRINT(92, "channel reqest %u", new_channel);
                break;
            }
            case PHY_EXTENSION_READ_RX_TIME: {
                common_write_32_bit(packet_rxsync_time, data_ptr);
                break;
            }
            case PHY_EXTENSION_GET_TIMESTAMP: {
                *(uint32_t *)data_ptr = rf_get_timestamp();
                break;
            }
            case PHY_EXTENSION_READ_RX_ACTIVE: {
                // A frame is on its way in (its airtime runs): an LFN's
                // receive window closes only after it (GS8 stage 2)
                *data_ptr = driver_state == DRVSTATE_RX;
                break;
            }
            default:
                WARN("RF extention not implemented: %02x", extension_type);
                break;
        }
    }

    return 0;
}

void shm_init()
{
    char shm_name[192], shm_infos_name[192];

    FATAL_ON(!sim_socket_path[0], 2, "shm_init() before rf_sim_connect()");
    sim_shm_name(shm_name, sizeof(shm_name), "wssimcca", sim_socket_path);
    sim_shm_name(shm_infos_name, sizeof(shm_infos_name), "wssimnodesinfos", sim_socket_path);

    // wssimserver creates both tables before it listens. A missing one means
    // this node and the server spelled the socket path differently (the names
    // come from the path string): creating it here would give this node
    // tables of its own, with perfect links and no shared CCA.
    int shm_fd = shm_open(shm_name, O_RDWR, 0);
    FATAL_ON(shm_fd < 0, 2, "shm_open %s: %m (is wssimserver's socket path spelled the same: %s?)",
             shm_name, sim_socket_path);

    // Map shared memory
    shm_ptr = mmap(0, shm_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd, 0);
    FATAL_ON(shm_ptr == MAP_FAILED, 2, "shm mmap failed!");


    // Shared memory for nodes and edges information
    int shm_infos_fd = shm_open(shm_infos_name, O_RDWR, 0);
    FATAL_ON(shm_infos_fd < 0, 2, "shm_open %s: %m (is wssimserver's socket path spelled the same: %s?)",
             shm_infos_name, sim_socket_path);

    // Map shared memory
    void* infos_ptr = mmap(0, shm_infos_size, PROT_READ | PROT_WRITE, MAP_SHARED, shm_infos_fd, 0);
    FATAL_ON(infos_ptr == MAP_FAILED, 2, "shm_infos mmap failed!");

    nodes_infos_flat_map = (struct node_infos *)infos_ptr;
}

void rf_driver_local_init(void)
{
    ringbufindex_init(&input_ringbuf, MAX_TXRX_BUFFERS);
    ringbufindex_init(&output_ringbuf, MAX_TXRX_BUFFERS);
    csma_timeout_timer = eventOS_callback_timer_register(_rf_csma_timeout_timer_cb);
    tx_sleep_handler_timer = eventOS_callback_timer_register(_rf_tx_sleep_timeout_timer_cb);
    rx_sleep_handler_timer = eventOS_callback_timer_register(_rf_rx_sleep_timeout_timer_cb);
    backup_timeout_timer = eventOS_callback_timer_register(_rf_backup_timer_cb);
    ns_port_add_poll_hook(rf_fhss_status_poll);
    if (getenv("WSSIM_RADIO_OFF"))
        rf_off_state_enabled = strtol(getenv("WSSIM_RADIO_OFF"), NULL, 0) != 0;
    rf_phy_settings_init();

    driver_state = DRVSTATE_IDLE;
    set_shm_cca_state();
}
// XXX: the phy_channel_pages needs to match the config at cmd_network.c, or the RF init fails
int8_t virtual_rf_device_register(phy_link_type_e link_type, uint16_t mtu_size)
{
    if (rf_radio_driver_id < 0) {
        shm_init();
        rf_driver_local_init();
        memset(&device_driver, 0, sizeof(phy_device_driver_s));
        /*Set pointer to MAC address*/
        device_driver.PHY_MAC = rf_mac_address;
        device_driver.driver_description = "VSND";

        device_driver.link_type = link_type;

        if (link_type == PHY_LINK_15_4_SUBGHZ_TYPE) {
            /*Type of RF PHY is SubGHz*/
            device_driver.phy_channel_pages = phy_channel_pages;
        } else {
            device_driver.phy_channel_pages = NULL;
        }

        device_driver.phy_MTU = mtu_size;
        /* Add 0 extra bytes header in PHY */
        device_driver.phy_header_length = 0;
        /* Register handler functions */
        device_driver.state_control = &phy_rf_state_control;
        device_driver.tx = &phy_rf_tx;
        device_driver.address_write = phy_rf_address_write;
        device_driver.extension = &phy_rf_extension;
        device_driver.phy_tx_done_cb = NULL;
        rf_radio_driver_id = arm_net_phy_register(&device_driver);

        arm_net_observer_cb_set(rf_radio_driver_id, phy_rf_mlme_orserver_tx);
    }
    return rf_radio_driver_id;
}
