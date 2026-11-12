/* SPDX-License-Identifier: Apache-2.0
 *
 * wssimserver --describe=json: every option as JSON (docs/MEDIUM.md §11.12).
 */
#ifndef SIM_DESCRIBE_H
#define SIM_DESCRIBE_H

#include <getopt.h>
#include <stdio.h>

struct describe_preset {
    const char *name;
    const char *const *opts;    // NULL-terminated
};

// opts: the parser's getopt_long table (NULL name at the end). Checks that
// every option has a description and the reverse (FATAL otherwise), then
// prints the JSON to f.
void describe_json(FILE *f, const struct option *opts, const struct describe_preset *presets, int n_presets,
                   int default_preset);

#endif
