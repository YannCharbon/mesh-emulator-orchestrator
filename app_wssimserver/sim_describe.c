/* SPDX-License-Identifier: Apache-2.0
 *
 * wssimserver --describe=json (docs/MEDIUM.md §11.12): every command-line
 * option with its type, default, the values the medium presets give it and
 * a one-line help, for programs that build command lines (the web
 * application's emulator settings).
 *
 * The option table of the parser (getopt_long) is the reference: the
 * server checks, before printing, that every option it parses has an entry
 * here and every entry here is an option it parses, and refuses to print
 * otherwise. The preset values come from the preset table itself.
 */
#include <stdbool.h>
#include <string.h>

#include "common/log.h"
#include "sim_describe.h"
#include "sim_floorplan.h"

struct desc {
    const char *name;           // long option, without "--"
    const char *group;          // server, vtime, power, medium, sniff, propagation, variation, interference
    const char *arg;            // the argument's syntax, NULL for a flag
    const char *type;           // flag, int, number, enum, string, file, spec, list
    const char *values;         // enum: the values, separated by '|'
    const char *def;            // the built-in default, NULL for none
    bool repeatable;
    const char *needs;          // another option it needs, NULL for none
    const char *section;        // documentation
    const char *help;
};

#define R true
#define N false
static const struct desc descs[] = {
    // The server
    { "group", "server", "NODES[:RSSI]", "spec", NULL, NULL, R, NULL, "MEDIUM.md §2",
      "Link these nodes (0-2, or 2,5:-67.0); RSSI in dBm at the receiver; A,B:RSSI is A->B and B->A unless B,A is given" },
    { "group-file", "server", "FILE", "file", NULL, NULL, R, NULL, "MEDIUM.md §2",
      "-g options from a file, one per line (blank lines and # comments skipped)" },
    { "verbose", "server", NULL, "flag", NULL, NULL, N, NULL, NULL, "Also log each connection's fd and the fd limit" },
    { "dump", "server", NULL, "flag", NULL, NULL, N, NULL, NULL, "Print the topology and the RSSI map" },
    { "fpath", "server", "[FILE]", "file", NULL, "/tmp/graph.txt", N, "dump", NULL,
      "With --dump, also write the edges to FILE" },
    { "help", "server", NULL, "flag", NULL, NULL, N, NULL, NULL, "Print the options and exit" },
    { "describe", "server", "json", "enum", "json", NULL, N, NULL, "MEDIUM.md §11.12",
      "Print this description of every option as JSON and exit" },
    // Virtual time
    { "vtime", "vtime", "N", "int", NULL, NULL, N, NULL, "VIRTUAL_TIME.md", "Virtual time: conduct N nodes (1 to 4096)" },
    { "vtime-end", "vtime", "SECONDS", "number", NULL, NULL, N, "vtime", "VIRTUAL_TIME.md", "End the run at this virtual time" },
    { "vtime-control", "vtime", "SOCKET", "string", NULL, NULL, N, "vtime", "SCENARIOS.md",
      "A scenario driver steps the run through this UNIX stream socket (set by the run script with SCENARIO)" },
    { "drift", "vtime", "[IDS:]PPM", "spec", NULL, "0", R, "vtime", "VIRTUAL_TIME.md",
      "Clock error of these nodes (all if no IDS), in ppm; the last one naming a node wins" },
    { "drift-random", "vtime", "PPM", "number", NULL, "0", N, "vtime", "VIRTUAL_TIME.md",
      "Each node draws its clock error in [-PPM, +PPM] from WSSIM_SEED" },
    { "vtime-kill", "vtime", "ID:SECONDS", "spec", NULL, NULL, R, "vtime", "VIRTUAL_TIME.md",
      "Stop node ID outright at that time" },
    { "vtime-start", "vtime", "ID:SECONDS", "spec", NULL, NULL, R, "vtime", "VIRTUAL_TIME.md",
      "One more instance of node ID is welcomed at that time (after the previous one stops)" },
    { "vtime-power-up", "vtime", "ID:SECONDS", "spec", NULL, NULL, R, "vtime", "VIRTUAL_TIME.md",
      "Node ID is welcomed at that time instead of 0" },
    { "vtime-cut", "vtime", "SRC:DST:SECONDS", "spec", NULL, NULL, R, "vtime", "VIRTUAL_TIME.md",
      "From that time, frames from SRC no longer reach DST (one direction)" },
    { "vtime-link", "vtime", "A:B:SECONDS:RSSI", "spec", NULL, NULL, R, "vtime", "MEDIUM.md §11.5",
      "From that time, the link A-B has this RSSI (dBm), both directions" },
    { "vtime-drop", "vtime", "SRC:DST:SEQ:FROM:TO", "spec", NULL, NULL, R, "vtime", "VIRTUAL_TIME.md",
      "Frames from SRC with this MAC sequence number are not delivered to DST between FROM and TO s" },
    { "vtime-frames", "vtime", "FILE", "file", NULL, NULL, N, "vtime", "VIRTUAL_TIME.md",
      "Write every frame sent (time us, sender, channel, PSDU in hex) to FILE" },
    // Power
    { "power", "power", "PROFILE", "file", NULL, NULL, N, "vtime", "POWER_MODEL.md",
      "Record power, every node with this profile" },
    { "power-node", "power", "IDS:PROFILE", "spec", NULL, NULL, R, "power", "POWER_MODEL.md",
      "Another profile for these nodes" },
    { "power-mode", "power", "[IDS:]MODE", "spec", NULL, "auto", R, "power", "POWER_MODEL.md",
      "Profile values of this PHY mode (1a ... 5), common, or auto (the mode the node's MAC configured)" },
    { "power-out", "power", "FILE", "file", NULL, "power.json", N, "power", "POWER_MODEL.md", "The power report" },
    { "power-log", "power", "FILE", "file", NULL, NULL, N, "power", "POWER_MODEL.md",
      "Also log every state interval of every node" },
    { "power-cell", "power", "MAH[:V[:EFF]]", "spec", NULL, NULL, N, "power", "POWER_MODEL.md",
      "Battery life for this cell (capacity, voltage, converter efficiency)" },
    // Medium
    { "medium", "medium", "MODE", "enum", "central|legacy", "central", N, "vtime", "MEDIUM.md §1",
      "Who decides reception: the server (central) or each radio (legacy, the permanent reference runs)" },
    { "medium-preset", "medium", "NAME", "enum", "calibrated|realistic|basic|em4", "calibrated", N, "vtime",
      "MEDIUM.md §14", "A set of medium options placed before the command line's own" },
    { "medium-tx-ref", "medium", "DBM", "number", NULL, NULL, N, "vtime", "MEDIUM.md §2",
      "Link values are the received power when the sender transmits at DBM (default: whatever its TX power)" },
    { "medium-tx-max", "medium", "DBM|profile|off", "spec", NULL, "off", N, "vtime", "MEDIUM.md §2",
      "The most a sender radiates, for links that depend on its TX power" },
    { "medium-lock-snr", "medium", "DB", "number", NULL, "0", N, "vtime", "MEDIUM.md §3",
      "Lowest SINR at which a receiver locks onto a frame" },
    { "medium-model", "medium", "MODEL", "enum", "fg25|em4", "fg25", N, "vtime", "MEDIUM.md §2.1",
      "Reception model: the FG25 kit measurements or the MVP's" },
    { "medium-int-curve", "medium", "[MODE:]X0,B|noise", "spec", NULL, "per model (fg25 mode 3: -2.85,0.9)", R,
      "vtime", "MEDIUM.md §2.1", "Interference bit error rate 0.5 exp(-(SIR-X0)/B), or the noise curve" },
    { "medium-capture-db", "medium", "[MODE:]DB", "spec", NULL, "per model (fg25: 3; em4: 6)", R, "vtime",
      "MEDIUM.md §2.1", "A frame this much stronger captures the receiver in the capture windows" },
    { "medium-capture-early-bits", "medium", "[MODE:]N|shr", "spec", NULL, "per model (fg25: 1a 20, 3 37; em4: shr)",
      R, "vtime", "MEDIUM.md §2.1", "Early capture window, bits from the SHR's start, or the whole SHR" },
    { "medium-capture-sfd", "medium", "[MODE:]on|off", "spec", NULL, "per model (fg25: 3 on, 1a off)", R, "vtime",
      "MEDIUM.md §2.1", "Capture also during the SFD" },
    { "medium-break-db", "medium", "[MODE:]DB|off", "spec", NULL, "per model (fg25: 2.5; em4: off)", R, "vtime",
      "MEDIUM.md §2.1", "Between the windows, a frame this much stronger makes both lost" },
    { "medium-adjacent-db", "medium", "[MODE:]DB|off", "spec", NULL, "per model (fg25: 10; em4: off)", R, "vtime",
      "MEDIUM.md §2.1", "A frame on the next channel interferes this much weaker" },
    { "medium-loss", "medium", "P", "number", NULL, "0", N, "vtime", "MEDIUM.md §12.2",
      "Every frame a receiver would decode is destroyed with this probability, independently per receiver and frame (as ns-3's random loss)" },
    { "medium-log", "medium", "FILE", "file", NULL, NULL, N, "vtime", "MEDIUM.md §7",
      "One line per frame and receiver (t_end_us src dst frame_id outcome rx_dbm sinr_min_db ...)" },
    // Sniffing
    { "sniff-detect", "sniff", "WHAT", "enum", "preamble|frame", "preamble", N, "vtime", "MEDIUM.md §10.5",
      "What part of a frame a sniff can detect" },
    { "sniff-dwell-bits", "sniff", "N", "int", NULL, "7", N, "vtime", "MEDIUM.md §10.5",
      "Bits of a frame a sniff must observe for one detection try" },
    { "sniff-threshold", "sniff", "DBM|off", "spec", NULL, "off", N, "vtime", "MEDIUM.md §10.5",
      "Lowest received power a sniff detects" },
    { "sniff-curve", "sniff", "ber:N|SINR:P,...", "spec", NULL, "-2.8:0,0.6:0.002,3.9:0.65,5.4:1", N, "vtime",
      "MEDIUM.md §10.5", "Detection probability of one try against its lowest SINR" },
    { "sniff-sfd-bits", "sniff", "N", "int", NULL, "16", N, "vtime", "MEDIUM.md §10.5", "Bits of SFD at the end of each SHR" },
    { "sniff-settle-us", "sniff", "N", "int", NULL, "225", N, "vtime", "MEDIUM.md §10.5",
      "Receiver settling time: a sniff detects from N us after its receiver is on" },
    { "sniff-margin-bits", "sniff", "N", "int", NULL, "22", N, "vtime", "MEDIUM.md §10.5",
      "Preamble bits a detection must leave before the SFD for the frame to be received" },
    { "sniff-log", "sniff", "FILE", "file", NULL, NULL, N, "vtime", "MEDIUM.md §10.4", "One line per sniff" },
    // Propagation
    { "positions", "propagation", "FILE", "file", NULL, NULL, N, NULL, "MEDIUM.md §11.1",
      "Node positions, one per line: ID X Y Z [FLOOR [GAIN_DBI]] (m); enables the propagation model" },
    { "prop-model", "propagation", "MODEL", "enum", "indoor|logdist|freespace", "indoor (with --prop-floorplan: freespace)",
      N, "positions", "MEDIUM.md §11.3", "Path-loss model" },
    { "prop-freq", "propagation", "MHZ", "number", NULL, "915", N, "positions", "MEDIUM.md §11.3", "Carrier frequency" },
    { "prop-pl0", "propagation", "DB", "number", NULL, "per model (indoor: 20log10(f)-28; others: free space at 1 m)", N,
      "positions", "MEDIUM.md §11.3", "Path loss at 1 m" },
    { "prop-exp", "propagation", "N", "number", NULL, "per model (indoor 3.3, logdist 3.0, freespace and floor plan 2)", N,
      "positions", "MEDIUM.md §11.3", "Distance exponent" },
    { "prop-min-dist", "propagation", "M", "number", NULL, "1", N, "positions", "MEDIUM.md §11.3",
      "Shortest distance in the formula" },
    { "prop-wall-db-per-m", "propagation", "DB", "number", NULL, "0", N, "positions", "MEDIUM.md §11.3",
      "Wall loss per horizontal metre" },
    { "prop-walls", "propagation", "SX[,SY]:DB", "spec", NULL, NULL, N, "positions", "MEDIUM.md §11.3",
      "A regular grid of walls every SX (SY) metres, DB each" },
    { "prop-floor-db", "propagation", "L1[,L2...]", "list", NULL, "indoor: 9,19,24; others: none", N, "positions",
      "MEDIUM.md §11.3", "Loss for 1, 2, ... floors apart, the last step repeated beyond; one value: per floor" },
    { "prop-floor-height", "propagation", "M", "number", NULL, "3.5", N, "positions", "MEDIUM.md §11.1",
      "Floor of a node without a FLOOR column: Z / M rounded (a floor plan's floorheight wins)" },
    { "prop-floorplan", "propagation", "FILE", "file", NULL, NULL, N, "positions", "MEDIUM.md §11.11",
      "Walls and slabs with materials: the walls crossed and the slabs add their losses" },
    { "prop-floor-model", "propagation", "MODEL", "enum", "cost231|slabs|table", "cost231", N, "prop-floorplan",
      "MEDIUM.md §11.11", "Floor term with a floor plan: COST 231's nonlinear term on the mean slab loss, the sum of the slabs crossed, or --prop-floor-db" },
    { "prop-floor-b", "propagation", "B", "number", NULL, "0.46", N, "prop-floorplan", "MEDIUM.md §11.11",
      "b of the COST 231 floor term Lf k^((k+2)/(k+1)-b)" },
    { "prop-shadow", "propagation", "DB", "number", NULL, "4", N, "positions", "MEDIUM.md §11.4",
      "Static shadowing sigma, the same both ways" },
    { "prop-shadow-dir", "propagation", "DB", "number", NULL, "0", N, "positions", "MEDIUM.md §11.4",
      "Static shadowing sigma of each direction on its own" },
    { "prop-shadow-node", "propagation", "DB", "number", NULL, "0", N, "positions", "MEDIUM.md §11.4",
      "Static shadowing sigma of a term per node, in all its links" },
    { "prop-gain", "propagation", "DBI", "number", NULL, "0", N, "positions", "MEDIUM.md §11.1",
      "Antenna gain of a node without a GAIN column" },
    { "prop-tx-dbm", "propagation", "DBM", "number", NULL, "20", N, "positions", "MEDIUM.md §11.2",
      "Nominal TX power: the RSSI map and the hearing graph" },
    { "prop-cutoff", "propagation", "DBM", "number", NULL, "-125", N, "positions", "MEDIUM.md §11.5",
      "A pair hears each other when its mean level plus the margin reaches this" },
    { "prop-margin", "propagation", "DB", "number", NULL, "10", N, "positions", "MEDIUM.md §11.5",
      "Margin of the hearing graph for the time-varying layer" },
    { "prop-dump", "propagation", "FILE", "file", NULL, NULL, N, "positions", "MEDIUM.md §11.7",
      "The computed links at start-up, one line per direction (with a floor plan: walls crossed and their losses)" },
    { "prop-dump-node", "propagation", "ID", "int", NULL, NULL, N, "prop-dump", "MEDIUM.md §11.7",
      "With --prop-dump, only the links of this node" },
    { "rssi-offset", "propagation", "DB", "number", NULL, "0", N, "vtime", "MEDIUM.md §11.6",
      "Reported RSSI: sigma of a fixed offset per receiver" },
    { "rssi-noise", "propagation", "DB", "number", NULL, "0", N, "vtime", "MEDIUM.md §11.6",
      "Reported RSSI: sigma of a draw per frame" },
    // Link variation
    { "medium-rx-model", "variation", "MODEL", "enum", "empirical|layered", "empirical", N, "vtime", "MEDIUM.md §12.3",
      "Noise curve: the empirical one or a steep receiver curve meant for fading" },
    { "medium-rx-curve", "variation", "X0,B", "spec", NULL, "9.0,0.9", N, "vtime", "MEDIUM.md §12.3",
      "Layered curve: BER 0.5 exp(-(SNR-X0)/B)" },
    { "medium-combine", "variation", "max|sum|P", "spec", NULL, "max (layered: sum)", N, "vtime", "MEDIUM.md §12.4",
      "Noise and interference: the larger error rate or the P-norm of their equivalent powers" },
    { "medium-fading", "variation", "K,TC_S|off", "spec", NULL, "off (layered: 10,0.3)", N, "vtime", "MEDIUM.md §12.1",
      "Rician fading: K factor (linear), coherence time in s" },
    { "medium-fading-bc", "variation", "MHZ|inf", "spec", NULL, "5.5", N, "vtime", "MEDIUM.md §12.1",
      "Coherence bandwidth across channels; inf: the same fading on every channel" },
    { "medium-chan-spacing", "variation", "KHZ", "number", NULL, "from the data rate (50 kbit/s: 100; others: 200)", N,
      "vtime", "MEDIUM.md §12.1", "Channel spacing for the fading" },
    { "medium-shadowing", "variation", "SIGMA_DB,TAU_S|off", "spec", NULL, "off", N, "vtime", "MEDIUM.md §12.1",
      "Slow shadowing (Gauss-Markov)" },
    { "medium-node-share", "variation", "F", "number", NULL, "0", N, "vtime", "MEDIUM.md §12.1",
      "Share of the shadowing variance common to a node's links" },
    { "medium-reciprocal", "variation", "on|off", "enum", "on|off", "on", N, "vtime", "MEDIUM.md §12.1",
      "The same variation in both directions of a link" },
    { "medium-burst", "variation", "GOOD_S,BAD_S,DB[,link|rx|node]|off", "spec", NULL, "off", N, "vtime",
      "MEDIUM.md §12.2", "Loss bursts: mean good and bad durations, attenuation in the bad state" },
    { "medium-loss-burst", "variation", "GOOD_S,BAD_S,LOSS[,link|rx|node]|off", "spec", NULL, "off", N, "vtime",
      "MEDIUM.md §12.2", "Frame-loss bursts: mean good and bad durations; in the bad state each frame a receiver would decode is destroyed with probability LOSS, whatever the link's level" },
    { "medium-var-dump", "variation", "RX,TX,FROM_S,STEP_S,COUNT[,...]", "spec", NULL, NULL, N, "vtime",
      "MEDIUM.md §12.1", "Print a link's variation and exit" },
    // Interference and spectrum
    { "interferer", "interference", "SPEC", "spec", NULL, NULL, R, "vtime", "MEDIUM.md §13.1",
      "An external interferer, KEY=VALUE,...: chan, level, at, pos, eirp, pattern, hop, kind, rate, pre, adj, name" },
    { "interferer-file", "interference", "FILE", "file", NULL, NULL, R, "vtime", "MEDIUM.md §13.1",
      "One interferer SPEC per line (# comments)" },
    { "interferer-log", "interference", "FILE", "file", NULL, NULL, N, "vtime", "MEDIUM.md §13.1",
      "One line per on-interval (start_us end_us name chan)" },
    { "medium-adjacent-table", "interference", "[MODE:]DB/DB/...", "spec", NULL, "per model (fg25: 10, nothing further)",
      R, "vtime", "MEDIUM.md §13.2", "Attenuation of a frame 1, 2, ... channels away (off for none)" },
    { "medium-capture-model", "interference", "[MODE:]step|smooth", "spec", NULL, "step", R, "vtime", "MEDIUM.md §13.3",
      "Capture and break as steps at their margins, or probabilities fitted to the kits" },
    { "medium-capture-smooth", "interference", "[MODE:]KEY=V,...", "spec", NULL, "the fit of MEDIUM.md §13.3", R, "vtime",
      "MEDIUM.md §13.3", "Smooth model parameters: m50, s, brk_s, early, slope, w, rise, fall" },
    { "medium-int-curve-presync", "interference", "[MODE:]X0,B|same", "spec", NULL,
      "same (with smooth: -2.72,0.54)", R, "vtime", "MEDIUM.md §13.3",
      "Interference curve of frames already on air when the received frame's SHR ends" },
    { "sniff-adjacent", "interference", "MODE", "enum", "off|on|detect", "off", N, "vtime", "MEDIUM.md §13.4",
      "Frames on the next channels take part in sniffing as interference and energy (on), also as detectable (detect)" },
    { "sniff-energy", "interference", "DBM|off", "spec", NULL, "off", N, "vtime", "MEDIUM.md §13.4",
      "Also detect when the summed energy stays at or above DBM" },
    { "sniff-energy-us", "interference", "N", "int", NULL, "107", N, "vtime", "MEDIUM.md §13.4",
      "Averaging time of the energy detection" },
    { "sniff-false", "interference", "P|RATE/s", "spec", NULL, "0", N, "vtime", "MEDIUM.md §13.4",
      "False detections on noise: probability per sniff window, or a rate per second of listening" },
};
#undef R
#undef N

static const struct desc *desc_find(const char *name)
{
    for (size_t i = 0; i < sizeof(descs) / sizeof(descs[0]); i++)
        if (!strcmp(descs[i].name, name))
            return &descs[i];
    return NULL;
}

static void json_str(FILE *f, const char *s)
{
    if (!s) {
        fputs("null", f);
        return;
    }
    fputc('"', f);
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;

        if (c == '"' || c == '\\')
            fprintf(f, "\\%c", c);
        else if (c < 0x20)
            fprintf(f, "\\u%04x", c);
        else
            fputc(c, f);
    }
    fputc('"', f);
}

// Whether option o of a preset is option name; *val its value (NULL for a
// flag without one)
static bool preset_match(const char *o, const char *name, const char **val)
{
    size_t n = strlen(name);

    if (strncmp(o, "--", 2) || strncmp(o + 2, name, n) || (o[2 + n] != '=' && o[2 + n]))
        return false;
    *val = o[2 + n] ? o + 3 + n : NULL;
    return true;
}

// The value(s) a preset gives option name, as JSON: a string, true for a
// flag, an array when the preset gives it more than once. False if none.
static bool preset_json(FILE *f, const struct describe_preset *p, const char *name)
{
    const char *v;
    int n = 0, k2 = 0;

    for (int k = 0; p->opts[k]; k++)
        n += preset_match(p->opts[k], name, &v);
    if (!n || !f)
        return n;
    if (n > 1)
        fputc('[', f);
    for (int k = 0; p->opts[k]; k++)
        if (preset_match(p->opts[k], name, &v)) {
            fprintf(f, "%s", k2++ ? ", " : "");
            if (v)
                json_str(f, v);
            else
                fputs("true", f);
        }
    if (n > 1)
        fputc(']', f);
    return true;
}

void describe_json(FILE *f, const struct option *opts, const struct describe_preset *presets, int n_presets,
                   int default_preset)
{
    bool ok = true;
    size_t n_desc = sizeof(descs) / sizeof(descs[0]);

    // Both ways: the parser's options and the descriptions
    for (int i = 0; opts[i].name; i++)
        if (!desc_find(opts[i].name)) {
            WARN("--describe: option --%s has no description (sim_describe.c)", opts[i].name);
            ok = false;
        }
    for (size_t k = 0; k < n_desc; k++) {
        bool found = false;

        for (int i = 0; opts[i].name && !found; i++)
            found = !strcmp(opts[i].name, descs[k].name);
        if (!found) {
            WARN("--describe: description of --%s, which the parser does not know (sim_describe.c)", descs[k].name);
            ok = false;
        }
    }
    for (int p = 0; p < n_presets; p++)
        for (int k = 0; presets[p].opts[k]; k++) {
            const char *o = presets[p].opts[k], *eq = strchr(o, '=');
            char name[64];

            snprintf(name, sizeof(name), "%.*s", eq ? (int)(eq - o - 2) : (int)strlen(o + 2), o + 2);
            if (!desc_find(name)) {
                WARN("--describe: preset %s sets --%s, which has no description", presets[p].name, name);
                ok = false;
            }
        }
    FATAL_ON(!ok, 1, "--describe: the description does not match the parser");

    fprintf(f, "{\n  \"program\": \"wssimserver\",\n  \"schema\": 1,\n  \"presets\": [\n");
    for (int p = 0; p < n_presets; p++) {
        fprintf(f, "    {\"name\": ");
        json_str(f, presets[p].name);
        fprintf(f, ", \"default\": %s, \"options\": [", p == default_preset ? "true" : "false");
        for (int k = 0; presets[p].opts[k]; k++) {
            fprintf(f, "%s", k ? ", " : "");
            json_str(f, presets[p].opts[k]);
        }
        fprintf(f, "]}%s\n", p + 1 < n_presets ? "," : "");
    }
    fprintf(f, "  ],\n  \"materials\": [\n");
    for (int i = 0; fp_builtin_materials[i].name; i++) {
        fprintf(f, "    {\"name\": ");
        json_str(f, fp_builtin_materials[i].name);
        fprintf(f, ", \"loss_db\": %g, \"color\": ", fp_builtin_materials[i].db);
        json_str(f, fp_builtin_materials[i].color);
        fprintf(f, ", \"basis\": ");
        json_str(f, fp_builtin_materials[i].basis);
        fprintf(f, "}%s\n", fp_builtin_materials[i + 1].name ? "," : "");
    }
    fprintf(f, "  ],\n  \"options\": [\n");
    for (size_t k = 0; k < n_desc; k++) {
        const struct desc *d = &descs[k];
        bool first = true;

        fprintf(f, "    {\"name\": ");
        json_str(f, d->name);
        fprintf(f, ", \"group\": ");
        json_str(f, d->group);
        fprintf(f, ", \"arg\": ");
        json_str(f, d->arg);
        fprintf(f, ", \"type\": ");
        json_str(f, d->type);
        fprintf(f, ", \"values\": ");
        if (d->values) {
            const char *s = d->values;

            fputc('[', f);
            while (*s) {
                const char *bar = strchr(s, '|');
                size_t len = bar ? (size_t)(bar - s) : strlen(s);

                fprintf(f, "%s\"%.*s\"", s == d->values ? "" : ", ", (int)len, s);
                s += len + (bar != NULL);
            }
            fputc(']', f);
        } else {
            fputs("null", f);
        }
        fprintf(f, ", \"default\": ");
        json_str(f, d->def);
        // What a run gets without the option: the default preset's value
        // when it sets the option, else the built-in default
        fprintf(f, ", \"effective_default\": ");
        if (!preset_json(f, &presets[default_preset], d->name))
            json_str(f, d->def);
        fprintf(f, ", \"presets\": {");
        for (int p = 0; p < n_presets; p++) {
            if (!preset_json(NULL, &presets[p], d->name))
                continue;
            fprintf(f, "%s", first ? "" : ", ");
            json_str(f, presets[p].name);
            fprintf(f, ": ");
            preset_json(f, &presets[p], d->name);
            first = false;
        }
        fprintf(f, "}, \"repeatable\": %s, \"needs\": ", d->repeatable ? "true" : "false");
        json_str(f, d->needs);
        fprintf(f, ", \"section\": ");
        json_str(f, d->section);
        fprintf(f, ", \"help\": ");
        json_str(f, d->help);
        fprintf(f, "}%s\n", k + 1 < n_desc ? "," : "");
    }
    fprintf(f, "  ]\n}\n");
}
