/* SPDX-License-Identifier: Apache-2.0
 *
 * Link variation and the layered loss model of the central medium
 * (docs/MEDIUM.md §12). Off by default: without any of its options the
 * medium behaves exactly as before.
 *
 * - Variation of each link's received power with virtual time, added to the
 *   link's mean value: slow shadowing (Gauss-Markov), fast fading (Rician
 *   with a coherence time) and loss bursts (a two-state Gilbert-Elliott
 *   process). The value of a link at time t depends only on the run's seed,
 *   the link and t: not on the order or the number of the queries.
 * - The receiver's decision: the noise curve (today's empirical curve, or a
 *   steep receiver curve meant to be used with fading) and how noise and
 *   interference combine.
 */
#ifndef SIM_CHANNEL_H
#define SIM_CHANNEL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#include <stdint.h>

// One link's state at one time: what the medium log shows
struct chan_sample {
    float mean_dbm;             // the link's mean value (before variation)
    float shadow_db;            // slow shadowing
    float fading_db;            // fast fading (power gain of the Rician envelope)
    float burst_db;             // the burst attenuation (0 in the good state)
    uint8_t bad;                // 1: the burst process is in its bad state
};

// Options (before chan_start()): name without the leading "--", e.g.
// "medium-fading". Returns false if the name is not one of this module's.
bool chan_set_option(const char *name, const char *arg);
bool chan_options_given(void);
// The long option names of this module, NULL-terminated
extern const char *const chan_option_names[];
// After the command line: refuses options without effect; --medium-var-dump
// prints its samples and exits
void chan_options_done(void);
// --medium-combine was given (the medium warns for profiles where
// interference counts as noise, which ignore it)
bool chan_combine_given(void);
void chan_help(FILE *stream);

void chan_start(uint64_t seed);
void chan_finish(void);

// Any variation process is on (the medium log then has the extra columns)
bool chan_varies(void);
// The variation of link (rx hears tx) at t (µs), for a frame on chan at
// rate_bps, is added to *dbm, the link's mean received power; *s gets the
// parts. A perfect link (an edge without a value) does not vary.
void chan_apply(int rx, int tx, uint64_t t, uint16_t chan, uint32_t rate_bps, bool perfect, float *dbm,
                struct chan_sample *s);
// The extra columns of a medium log line (empty when nothing varies)
int chan_log_columns(char *buf, size_t size, const struct chan_sample *s);

// The receiver's decision differs from the empirical curve with the larger
// of the noise and interference error rates
bool chan_ber_custom(void);
// Bit error rate of a signal p_mw with noise n_mw and interference i_mw.
// int_noise: interference counts as noise (the em4 model); otherwise x0i,
// bi is the interference curve 0.5 exp(-(SIR - x0i) / bi).
double chan_ber(double p_mw, double n_mw, double i_mw, bool int_noise, double x0i, double bi);
// The noise curve of the model (a function of the SNR or SINR in dB)
double chan_noise_ber(double snr_db);
// Its inverse: the SNR (dB) of a bit error rate in (0, 0.5]
double chan_noise_snr(double ber);
// Noise and an equivalent interference power combined as chan_ber() does (mW)
double chan_combine(double n_mw, double i_eq_mw);
// The steep receiver curve is in use (--medium-rx-model=layered)
bool chan_layered(void);

#endif
