/* SPDX-License-Identifier: Apache-2.0
 *
 * Floor plan for the propagation model (docs/MEDIUM.md §11.11): walls and
 * slabs with materials, read from a text file (--prop-floorplan), and the
 * losses of a straight path through them.
 *
 * Geometry: a wall is a vertical segment on one storey, full storey height;
 * the slab of floor F lies between storeys F and F + 1. A point is given by
 * its horizontal coordinates and its storey coordinate s = F + u, with F its
 * storey and u its height within the storey (0..1). Along a path, s changes
 * linearly, so the storey at a wall crossing is floor(s(t)).
 */
#ifndef SIM_FLOORPLAN_H
#define SIM_FLOORPLAN_H

#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>

// Reads the file; FATAL on any error. default_h: the floor height when the
// file has no floorheight line.
void fp_load(const char *path, double default_h);
bool fp_loaded(void);
double fp_floor_height(void);
// Log lines describing what was read
void fp_log_summary(void);

// The loss of the slab between storeys f and f + 1 (dB)
double fp_slab_db(int f);

// Walls crossed by the straight path from a to b: a[0..1], b[0..1] the
// horizontal coordinates (m), sa, sb the storey coordinates. Returns the
// summed loss (dB); *n the weighted number of crossings (a contact at a
// wall's end, or at the path's end, counts half), *count the number of walls
// met. With list (size listsz), the crossed walls' IDs: the floor plan's line
// number, ".K" for edge K of an outline, ":W" when the weight W is not 1;
// "-" if none.
double fp_walls(const double a[2], double sa, const double b[2], double sb, double *n, int *count,
                char *list, size_t listsz);

// Built-in materials, for --describe and the documentation
struct fp_material_desc {
    const char *name;
    double db;
    const char *color;
    const char *basis;
};
extern const struct fp_material_desc fp_builtin_materials[];

#endif
