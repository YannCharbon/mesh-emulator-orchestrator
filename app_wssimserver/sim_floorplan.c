/* SPDX-License-Identifier: Apache-2.0
 *
 * Floor plan: docs/MEDIUM.md §11.11.
 *
 * The file is read once at the start. Walls are grouped by storey; each
 * storey has a uniform grid of cells listing the walls whose bounding box
 * (widened by the tolerance) meets the cell. A path is split by storey (its
 * storey coordinate is linear along it); on each storey, the cells that the
 * part of the path on that storey passes through give the candidate walls,
 * each tested once. The crossings are summed in the order of the walls'
 * indices, so the result does not depend on the grid
 * (WSSIM_FLOORPLAN_BRUTE=1 tests every wall instead: the tests compare both).
 */
#include <ctype.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "common/log.h"
#include "sim_floorplan.h"

#define FP_TOL 1e-6             // m: a point closer than this to a line is on it
#define FP_MAX_COORD 1e6        // m, as for positions
#define FP_MAX_FLOOR 10000
#define FP_NAME_MAX 64
#define FP_GRID_MAX 1024        // cells per side at most

/*
 * Built-in materials: the loss of one crossing at normal incidence at
 * 868-915 MHz. docs/MEDIUM.md §11.11 gives the thickness assumed and the
 * source of each value.
 */
const struct fp_material_desc fp_builtin_materials[] = {
    { "plasterboard",         2.0, "#d9d4c7", "measured: plasterboard stud wall, 900 MHz (Ericsson, via COST 231 §4.7.2: 2.1 dB)" },
    { "partition",            1.4, "#c8b99a", "measured: soft office partition, 914 MHz (Seidel and Rappaport: 1.39 dB)" },
    { "wood",                 2.8, "#a0703c", "measured: 38 mm dry lumber (NISTIR 6055: 2.69-2.77 dB)" },
    { "glass",                0.7, "#9ecae1", "measured: 6 mm glass (NISTIR 6055: 0.65-0.70 dB)" },
    { "double-glazing",       0.5, "#b8dcef", "computed: ITU-R P.2040-4, 4/16/4 mm" },
    { "low-e-glazing",       20.0, "#5f8fb0", "assumed: metallised glazing, sheet resistance 15-20 ohm/sq" },
    { "brick",                3.8, "#b5523b", "measured: 115 mm brick, interpolated (NISTIR 6055); P.2040-4: 3.5-3.7 dB" },
    { "brick-240",            6.0, "#9c4330", "measured: 240 mm brick, interpolated (NISTIR 6055); P.2040-4: 5.4-5.6 dB" },
    { "concrete-block",      11.3, "#9a948a", "measured: 203 mm hollow block (NISTIR 6055: 11.25-11.38 dB)" },
    { "concrete",            10.0, "#8c8c8c", "assumed: 150 mm wall, between P.2040-4 (5.4-5.7 dB) and NISTIR 6055 (16-20 dB)" },
    { "reinforced-concrete", 15.0, "#555555", "assumed: 200 mm wall with rebar, within COST 231's 10-20 dB" },
    { "concrete-slab",       15.0, "#6e6e6e", "measured: one floor, COST 231 Lf 18.3 dB at 1.8 GHz less 3.5 dB at 900 MHz" },
    { "metal",               26.0, "#3c3c50", "assumed: metal wall or floor, leakage-limited (Arnold et al. 815 MHz: 26 dB)" },
    { NULL, 0, NULL, NULL }
};

struct fp_mat {
    char name[FP_NAME_MAX];
    double db;
    int line;                   // 0: built-in
};

struct fp_wall {
    double x1, y1, x2, y2, len;
    double bx0, by0, bx1, by1;  // bounding box
    int floor, mat, line, edge; // edge: -1 for a wall line, K for edge K of an outline
    char matname[FP_NAME_MAX];  // until resolved
};

struct fp_slab {
    int floor, mat, line;
    char matname[FP_NAME_MAX];
};

struct fp_grid {
    int floor;
    int first, n;               // walls [first, first + n) of the sorted array
    double x0, y0, cell;
    int nx, ny;
    size_t *start;              // nx * ny + 1 offsets into items
    int *items;
};

static struct {
    bool loaded;
    const char *path;
    double h;
    bool has_h;
    struct fp_mat *mats;
    int n_mats;
    struct fp_wall *walls;
    int n_walls;
    struct fp_slab *slabs;
    int n_slabs;
    int n_outlines;
    int slab_default;           // material index of concrete-slab
    struct fp_grid *grids;
    int n_grids;
    unsigned *stamp;
    unsigned query;
    bool brute;
} fp;

/* ---- parsing */

static bool fp_number(const char *tok, double min, double max, double *v)
{
    char *end;

    *v = strtod(tok, &end);
    return end != tok && !*end && isfinite(*v) && *v >= min && *v <= max;
}

static int fp_floor_token(const char *tok, int lineno)
{
    double v;

    FATAL_ON(!fp_number(tok, -FP_MAX_FLOOR, FP_MAX_FLOOR, &v) || v != floor(v), 1,
             "--prop-floorplan: %s:%d: bad FLOOR \"%s\" (an integer from %d to %d)", fp.path, lineno, tok,
             -FP_MAX_FLOOR, FP_MAX_FLOOR);
    return (int)v;
}

static double fp_coord(const char *tok, int lineno)
{
    double v;

    FATAL_ON(!fp_number(tok, -FP_MAX_COORD, FP_MAX_COORD, &v), 1,
             "--prop-floorplan: %s:%d: bad coordinate \"%s\" (a finite number from %g to %g)", fp.path, lineno, tok,
             -FP_MAX_COORD, FP_MAX_COORD);
    return v;
}

static void fp_name(char *dst, const char *tok, int lineno)
{
    size_t n = strlen(tok);

    FATAL_ON(n >= FP_NAME_MAX, 1, "--prop-floorplan: %s:%d: material name too long (at most %d characters): %s",
             fp.path, lineno, FP_NAME_MAX - 1, tok);
    for (size_t i = 0; i < n; i++)
        FATAL_ON(!isalnum((unsigned char)tok[i]) && !strchr("_-.+", tok[i]), 1,
                 "--prop-floorplan: %s:%d: bad material name \"%s\" (letters, digits, _ - . +)", fp.path, lineno,
                 tok);
    FATAL_ON(isdigit((unsigned char)tok[0]) || tok[0] == '-' || tok[0] == '+' || tok[0] == '.', 1,
             "--prop-floorplan: %s:%d: a material name starts with a letter or _: %s", fp.path, lineno, tok);
    strcpy(dst, tok);
}

static int fp_find_mat(const char *name)
{
    for (int i = 0; i < fp.n_mats; i++)
        if (!strcmp(fp.mats[i].name, name))
            return i;
    return -1;
}

static void fp_add_wall(int floor, double x1, double y1, double x2, double y2, const char *mat, int line, int edge)
{
    struct fp_wall *w;
    double len = hypot(x2 - x1, y2 - y1);

    FATAL_ON(len < 1e-3, 1, "--prop-floorplan: %s:%d: a wall%s shorter than 1 mm", fp.path, line,
             edge >= 0 ? " (outline edge)" : "");
    fp.walls = realloc(fp.walls, (fp.n_walls + 1) * sizeof(*fp.walls));
    FATAL_ON(!fp.walls, 1, "realloc: %m");
    w = &fp.walls[fp.n_walls++];
    memset(w, 0, sizeof(*w));
    w->x1 = x1;
    w->y1 = y1;
    w->x2 = x2;
    w->y2 = y2;
    w->len = len;
    w->bx0 = fmin(x1, x2);
    w->by0 = fmin(y1, y2);
    w->bx1 = fmax(x1, x2);
    w->by1 = fmax(y1, y2);
    w->floor = floor;
    w->line = line;
    w->edge = edge;
    strcpy(w->matname, mat);
}

static void fp_parse_line(char **tok, int n, int lineno)
{
    const char *d = tok[0];
    double v;

    if (!strcmp(d, "floorheight")) {
        FATAL_ON(n != 2, 1, "--prop-floorplan: %s:%d: expected floorheight H", fp.path, lineno);
        FATAL_ON(fp.has_h, 1, "--prop-floorplan: %s:%d: floorheight given twice", fp.path, lineno);
        FATAL_ON(!fp_number(tok[1], 0.1, 1000, &v), 1,
                 "--prop-floorplan: %s:%d: bad floor height \"%s\" (metres, 0.1 to 1000)", fp.path, lineno, tok[1]);
        fp.h = v;
        fp.has_h = true;
    } else if (!strcmp(d, "material")) {
        struct fp_mat m = { .line = lineno };
        int i;

        // A COLOR (#rrggbb) after the loss starts with '#': a comment here
        FATAL_ON(n != 3, 1, "--prop-floorplan: %s:%d: expected material NAME LOSS_DB [#COLOR]", fp.path, lineno);
        fp_name(m.name, tok[1], lineno);
        FATAL_ON(!fp_number(tok[2], 0, 200, &m.db), 1,
                 "--prop-floorplan: %s:%d: bad loss \"%s\" (dB, 0 to 200)", fp.path, lineno, tok[2]);
        i = fp_find_mat(m.name);
        FATAL_ON(i >= 0 && fp.mats[i].line, 1, "--prop-floorplan: %s:%d: material %s defined twice (line %d)",
                 fp.path, lineno, m.name, fp.mats[i].line);
        if (i >= 0) {
            fp.mats[i] = m;     // overrides the built-in value
        } else {
            fp.mats = realloc(fp.mats, (fp.n_mats + 1) * sizeof(*fp.mats));
            FATAL_ON(!fp.mats, 1, "realloc: %m");
            fp.mats[fp.n_mats++] = m;
        }
    } else if (!strcmp(d, "wall")) {
        char mat[FP_NAME_MAX];
        int f;
        double x1, y1, x2, y2;

        FATAL_ON(n != 7, 1, "--prop-floorplan: %s:%d: expected wall FLOOR X1 Y1 X2 Y2 MATERIAL", fp.path, lineno);
        f = fp_floor_token(tok[1], lineno);
        x1 = fp_coord(tok[2], lineno);
        y1 = fp_coord(tok[3], lineno);
        x2 = fp_coord(tok[4], lineno);
        y2 = fp_coord(tok[5], lineno);
        fp_name(mat, tok[6], lineno);
        fp_add_wall(f, x1, y1, x2, y2, mat, lineno, -1);
    } else if (!strcmp(d, "slab")) {
        struct fp_slab s = { .line = lineno };

        FATAL_ON(n != 3, 1, "--prop-floorplan: %s:%d: expected slab FLOOR MATERIAL", fp.path, lineno);
        s.floor = fp_floor_token(tok[1], lineno);
        fp_name(s.matname, tok[2], lineno);
        for (int i = 0; i < fp.n_slabs; i++)
            FATAL_ON(fp.slabs[i].floor == s.floor, 1, "--prop-floorplan: %s:%d: slab of floor %d given twice (line %d)",
                     fp.path, lineno, s.floor, fp.slabs[i].line);
        fp.slabs = realloc(fp.slabs, (fp.n_slabs + 1) * sizeof(*fp.slabs));
        FATAL_ON(!fp.slabs, 1, "realloc: %m");
        fp.slabs[fp.n_slabs++] = s;
    } else if (!strcmp(d, "outline")) {
        int f, ncoord = n - 2, npts;
        char mat[FP_NAME_MAX] = "";
        double *xy;

        FATAL_ON(n < 2, 1, "--prop-floorplan: %s:%d: expected outline FLOOR X1 Y1 ... Xn Yn [MATERIAL]", fp.path,
                 lineno);
        f = fp_floor_token(tok[1], lineno);
        // An odd number of tokens after FLOOR: the last one is the material
        if (ncoord % 2) {
            fp_name(mat, tok[n - 1], lineno);
            ncoord--;
        }
        npts = ncoord / 2;
        xy = malloc(ncoord * sizeof(*xy));
        FATAL_ON(!xy, 1, "malloc: %m");
        for (int k = 0; k < ncoord; k++)
            xy[k] = fp_coord(tok[2 + k], lineno);
        // A closing point that repeats the first: the polygon closes anyway
        if (npts >= 2 && fabs(xy[ncoord - 2] - xy[0]) < FP_TOL && fabs(xy[ncoord - 1] - xy[1]) < FP_TOL)
            npts--;
        FATAL_ON(npts < 3, 1, "--prop-floorplan: %s:%d: an outline needs at least 3 points", fp.path, lineno);
        if (mat[0])
            for (int k = 0; k < npts; k++) {
                int k2 = (k + 1) % npts;
                fp_add_wall(f, xy[2 * k], xy[2 * k + 1], xy[2 * k2], xy[2 * k2 + 1], mat, lineno, k + 1);
            }
        free(xy);
        fp.n_outlines++;
    } else {
        FATAL(1, "--prop-floorplan: %s:%d: unknown directive \"%s\" (floorheight, material, wall, slab, outline)",
              fp.path, lineno, d);
    }
}

static int fp_wall_cmp(const void *a, const void *b)
{
    const struct fp_wall *x = a, *y = b;

    if (x->floor != y->floor)
        return x->floor < y->floor ? -1 : 1;
    if (x->line != y->line)
        return x->line < y->line ? -1 : 1;
    return (x->edge > y->edge) - (x->edge < y->edge);
}

static int fp_slab_cmp(const void *a, const void *b)
{
    const struct fp_slab *x = a, *y = b;

    return (x->floor > y->floor) - (x->floor < y->floor);
}

// Calls fn(ctx, cell) for every cell of g that the segment a-b, widened by
// the tolerance, passes through: row by row, the columns of the segment's x
// range within the row. Used to list a wall in the cells it traverses, and
// to find the candidate walls of a path.
typedef void (*fp_cell_fn)(void *ctx, long cell);

static void fp_cells(const struct fp_grid *g, double ax, double ay, double bx, double by, fp_cell_fn fn, void *ctx)
{
    double xmin = fmin(ax, bx), xmax = fmax(ax, bx), ylo = fmin(ay, by) - FP_TOL, yhi = fmax(ay, by) + FP_TOL;
    long r0, r1;

    r0 = (long)floor((ylo - g->y0) / g->cell);
    r1 = (long)floor((yhi - g->y0) / g->cell);
    if (r1 < 0 || r0 >= g->ny || xmax + FP_TOL < g->x0 || xmin - FP_TOL > g->x0 + g->nx * g->cell)
        return;
    r0 = r0 < 0 ? 0 : r0;
    r1 = r1 >= g->ny ? g->ny - 1 : r1;
    for (long r = r0; r <= r1; r++) {
        double ya = fmax(ylo, g->y0 + r * g->cell), yb = fmin(yhi, g->y0 + (r + 1) * g->cell);
        double xa = xmin, xb = xmax;
        long c0, c1;

        if (fabs(by - ay) > 1e-12) {
            // x of the segment at the band's limits, within its x range
            double x1 = ax + (ya - ay) * (bx - ax) / (by - ay), x2 = ax + (yb - ay) * (bx - ax) / (by - ay);

            xa = fmax(xmin, fmin(x1, x2));
            xb = fmin(xmax, fmax(x1, x2));
        }
        c0 = (long)floor((xa - FP_TOL - g->x0) / g->cell);
        c1 = (long)floor((xb + FP_TOL - g->x0) / g->cell);
        c0 = c0 < 0 ? 0 : c0;
        c1 = c1 >= g->nx ? g->nx - 1 : c1;
        for (long c = c0; c <= c1; c++)
            fn(ctx, r * g->nx + c);
    }
}

struct fp_fill {
    struct fp_grid *g;
    size_t *count;
    int wall;
    bool fill;
};

static void fp_fill_cell(void *ctx, long cell)
{
    struct fp_fill *f = ctx;

    if (!f->fill)
        f->g->start[cell + 1]++;
    else
        f->g->items[f->g->start[cell] + f->count[cell]++] = f->wall;
}

static void fp_build_grid(struct fp_grid *g)
{
    double bx0 = INFINITY, by0 = INFINITY, bx1 = -INFINITY, by1 = -INFINITY, w, h, cell;
    struct fp_fill f = { .g = g };
    long cells;

    for (int i = g->first; i < g->first + g->n; i++) {
        bx0 = fmin(bx0, fp.walls[i].bx0);
        by0 = fmin(by0, fp.walls[i].by0);
        bx1 = fmax(bx1, fp.walls[i].bx1);
        by1 = fmax(by1, fp.walls[i].by1);
    }
    bx0 -= 2 * FP_TOL;
    by0 -= 2 * FP_TOL;
    w = fmax(bx1 - bx0 + 2 * FP_TOL, 1e-3);
    h = fmax(by1 - by0 + 2 * FP_TOL, 1e-3);
    // About one wall per cell, at most FP_GRID_MAX cells per side
    cell = sqrt(w * h / g->n);
    cell = fmax(cell, fmax(w, h) / FP_GRID_MAX);
    g->x0 = bx0;
    g->y0 = by0;
    g->cell = cell;
    g->nx = (int)fmin(ceil(w / cell), FP_GRID_MAX) + 1;
    g->ny = (int)fmin(ceil(h / cell), FP_GRID_MAX) + 1;
    cells = (long)g->nx * g->ny;
    g->start = calloc(cells + 1, sizeof(*g->start));
    f.count = calloc(cells, sizeof(*f.count));
    FATAL_ON(!g->start || !f.count, 1, "calloc: %m");
    // Each wall in the cells it traverses (a long diagonal wall is not listed
    // in its whole bounding box): counted, then filled
    for (int pass = 0; pass < 2; pass++) {
        f.fill = pass;
        for (int i = g->first; i < g->first + g->n; i++) {
            const struct fp_wall *wl = &fp.walls[i];

            f.wall = i;
            fp_cells(g, wl->x1, wl->y1, wl->x2, wl->y2, fp_fill_cell, &f);
        }
        if (!pass) {
            for (long k = 0; k < cells; k++)
                g->start[k + 1] += g->start[k];
            g->items = malloc((g->start[cells] + 1) * sizeof(*g->items));
            FATAL_ON(!g->items, 1, "malloc: %m");
        }
    }
    free(f.count);
}

// Walls i and j on one line (parallel, and j's first end on i's line)
static bool fp_collinear(const struct fp_wall *a, const struct fp_wall *b)
{
    double ax = a->x2 - a->x1, ay = a->y2 - a->y1, bx = b->x2 - b->x1, by = b->y2 - b->y1;

    return fabs(ax * by - ay * bx) / (a->len * b->len) < 1e-9 &&
           fabs(ax * (b->y1 - a->y1) - ay * (b->x1 - a->x1)) / a->len < FP_TOL;
}

// Collinear walls of one storey that overlap over more than the tolerance
// (a wall drawn on an outline edge): a warning; a path crossing them at one
// point counts the strongest once (fp_walls)
static void fp_check_overlaps(void)
{
    long pairs = 0;
    int ex_a = 0, ex_b = 0;

    for (int i = 0; i < fp.n_walls; i++)
        for (int j = i + 1; j < fp.n_walls && fp.walls[j].floor == fp.walls[i].floor; j++) {
            const struct fp_wall *a = &fp.walls[i], *b = &fp.walls[j];
            double ux, uy, s1, s2;

            if (b->bx0 > a->bx1 + FP_TOL || b->bx1 < a->bx0 - FP_TOL || b->by0 > a->by1 + FP_TOL ||
                b->by1 < a->by0 - FP_TOL || !fp_collinear(a, b))
                continue;
            ux = (a->x2 - a->x1) / a->len;
            uy = (a->y2 - a->y1) / a->len;
            s1 = (b->x1 - a->x1) * ux + (b->y1 - a->y1) * uy;
            s2 = (b->x2 - a->x1) * ux + (b->y2 - a->y1) * uy;
            if (fmin(a->len, fmax(s1, s2)) - fmax(0, fmin(s1, s2)) > FP_TOL) {
                if (!pairs++) {
                    ex_a = i;
                    ex_b = j;
                }
            }
        }
    if (pairs)
        WARN("--prop-floorplan: %s: %ld pairs of walls overlap on one line (e.g. lines %d and %d, storey %d): a path "
             "through both at one point counts the one of higher loss", fp.path, pairs, fp.walls[ex_a].line,
             fp.walls[ex_b].line, fp.walls[ex_a].floor);
}

void fp_load(const char *path, double default_h)
{
    FILE *f = fopen(path, "r");
    char *line = NULL, **tok = NULL;
    size_t cap = 0;
    int lineno = 0, tcap = 0;

    FATAL_ON(!f, 1, "--prop-floorplan: %s: %m", path);
    fp.path = path;
    fp.h = default_h;
    for (int i = 0; fp_builtin_materials[i].name; i++) {
        fp.mats = realloc(fp.mats, (fp.n_mats + 1) * sizeof(*fp.mats));
        FATAL_ON(!fp.mats, 1, "realloc: %m");
        memset(&fp.mats[fp.n_mats], 0, sizeof(*fp.mats));
        strcpy(fp.mats[fp.n_mats].name, fp_builtin_materials[i].name);
        fp.mats[fp.n_mats++].db = fp_builtin_materials[i].db;
    }
    while (getline(&line, &cap, f) >= 0) {
        char *hash, *t, *save = NULL;
        int n = 0;

        lineno++;
        hash = strchr(line, '#');
        if (hash)
            *hash = '\0';
        for (t = strtok_r(line, " \t\r\n", &save); t; t = strtok_r(NULL, " \t\r\n", &save)) {
            if (n == tcap) {
                tcap = tcap ? 2 * tcap : 16;
                tok = realloc(tok, tcap * sizeof(*tok));
                FATAL_ON(!tok, 1, "realloc: %m");
            }
            tok[n++] = t;
        }
        if (n)
            fp_parse_line(tok, n, lineno);
    }
    free(tok);
    free(line);
    fclose(f);

    // Materials are resolved once the whole file is read: any order works
    for (int i = 0; i < fp.n_walls; i++) {
        fp.walls[i].mat = fp_find_mat(fp.walls[i].matname);
        FATAL_ON(fp.walls[i].mat < 0, 1, "--prop-floorplan: %s:%d: unknown material \"%s\"", path,
                 fp.walls[i].line, fp.walls[i].matname);
    }
    for (int i = 0; i < fp.n_slabs; i++) {
        fp.slabs[i].mat = fp_find_mat(fp.slabs[i].matname);
        FATAL_ON(fp.slabs[i].mat < 0, 1, "--prop-floorplan: %s:%d: unknown material \"%s\"", path,
                 fp.slabs[i].line, fp.slabs[i].matname);
    }
    fp.slab_default = fp_find_mat("concrete-slab");
    qsort(fp.walls, fp.n_walls, sizeof(*fp.walls), fp_wall_cmp);
    qsort(fp.slabs, fp.n_slabs, sizeof(*fp.slabs), fp_slab_cmp);
    for (int i = 0; i < fp.n_walls; ) {
        int j = i;
        struct fp_grid *g;

        while (j < fp.n_walls && fp.walls[j].floor == fp.walls[i].floor)
            j++;
        fp.grids = realloc(fp.grids, (fp.n_grids + 1) * sizeof(*fp.grids));
        FATAL_ON(!fp.grids, 1, "realloc: %m");
        g = &fp.grids[fp.n_grids++];
        memset(g, 0, sizeof(*g));
        g->floor = fp.walls[i].floor;
        g->first = i;
        g->n = j - i;
        fp_build_grid(g);
        i = j;
    }
    fp_check_overlaps();
    fp.stamp = calloc(fp.n_walls + 1, sizeof(*fp.stamp));
    FATAL_ON(!fp.stamp, 1, "calloc: %m");
    {
        const char *b = getenv("WSSIM_FLOORPLAN_BRUTE");
        fp.brute = b && *b && strcmp(b, "0");
    }
    fp.loaded = true;
}

bool fp_loaded(void)
{
    return fp.loaded;
}

double fp_floor_height(void)
{
    return fp.h;
}

void fp_log_summary(void)
{
    int user = 0, fmin_ = 0, fmax_ = 0;

    for (int i = 0; i < fp.n_mats; i++)
        user += fp.mats[i].line != 0;
    if (fp.n_walls) {
        fmin_ = fp.walls[0].floor;
        fmax_ = fp.walls[fp.n_walls - 1].floor;
    }
    INFO("propagation: floor plan %s: %d walls on %d storeys (%d to %d), %d slabs given (others: concrete-slab "
         "%.1f dB), %d outlines, %d materials (%d from the file), floor height %.2f m%s",
         fp.path, fp.n_walls, fp.n_grids, fmin_, fmax_, fp.n_slabs, fp.mats[fp.slab_default].db, fp.n_outlines,
         fp.n_mats, user, fp.h, fp.brute ? "; every wall tested (WSSIM_FLOORPLAN_BRUTE)" : "");
}

double fp_slab_db(int f)
{
    int lo = 0, hi = fp.n_slabs - 1;

    while (lo <= hi) {
        int mid = (lo + hi) / 2;

        if (fp.slabs[mid].floor == f)
            return fp.mats[fp.slabs[mid].mat].db;
        if (fp.slabs[mid].floor < f)
            lo = mid + 1;
        else
            hi = mid - 1;
    }
    return fp.mats[fp.slab_default].db;
}

/* ---- paths */

enum { FP_NONE, FP_CROSS, FP_VERTEX };

struct fp_hit {
    int wall, kind, side;       // side: FP_VERTEX, the side of the path the wall leaves to (+1 left, -1 right)
    double w, t, vx, vy;        // w: FP_CROSS's weight; vx, vy: the vertex on the path
};

// Whether the path p + t·d (t in [0, 1], plen = |d|) meets wall w, and how:
// FP_CROSS, a crossing (weight 1, or 1/2 at an end of the path, 1/4 if also
// at an end of the wall); FP_VERTEX, an end of the wall on the path's
// interior (decided per vertex, in fp_walls). A wall along the path's line
// (collinear) is not met.
static int fp_cross(const double p[2], const double d[2], double plen, const struct fp_wall *w, struct fp_hit *h)
{
    double da = (d[0] * (w->y1 - p[1]) - d[1] * (w->x1 - p[0])) / plen;
    double db = (d[0] * (w->y2 - p[1]) - d[1] * (w->x2 - p[0])) / plen;
    double ex = w->x2 - w->x1, ey = w->y2 - w->y1, dp, dq;
    bool za = fabs(da) < FP_TOL, zb = fabs(db) < FP_TOL, zp, zq;

    if (za && zb)
        return FP_NONE;
    if ((da >= FP_TOL && db >= FP_TOL) || (da <= -FP_TOL && db <= -FP_TOL))
        return FP_NONE;
    dp = (ex * (p[1] - w->y1) - ey * (p[0] - w->x1)) / w->len;
    dq = (ex * (p[1] + d[1] - w->y1) - ey * (p[0] + d[0] - w->x1)) / w->len;
    zp = fabs(dp) < FP_TOL;
    zq = fabs(dq) < FP_TOL;
    if (zp && zq)
        return FP_NONE;
    if ((dp >= FP_TOL && dq >= FP_TOL) || (dp <= -FP_TOL && dq <= -FP_TOL))
        return FP_NONE;
    h->t = zp ? 0 : zq ? 1 : dp / (dp - dq);
    if ((za || zb) && !zp && !zq) {
        h->vx = za ? w->x1 : w->x2;
        h->vy = za ? w->y1 : w->y2;
        h->side = (za ? db : da) > 0 ? 1 : -1;
        return FP_VERTEX;
    }
    h->w = (za || zb ? 0.5 : 1) * (zp || zq ? 0.5 : 1);
    return FP_CROSS;
}

static struct fp_hit *hits, *picked;
static int n_hits, cap_hits, n_picked, cap_picked;

static void fp_push(struct fp_hit **a, int *n, int *cap, const struct fp_hit *h)
{
    if (*n == *cap) {
        *cap = *cap ? 2 * *cap : 64;
        *a = realloc(*a, *cap * sizeof(**a));
        FATAL_ON(!*a, 1, "realloc: %m");
    }
    (*a)[(*n)++] = *h;
}

struct fp_q {
    const double *p, *d;
    double plen, sa, sb;
};

static void fp_try(struct fp_q *q, int i)
{
    struct fp_hit h = { .wall = i };
    int kind;

    if (fp.stamp[i] == fp.query)
        return;
    fp.stamp[i] = fp.query;
    kind = fp_cross(q->p, q->d, q->plen, &fp.walls[i], &h);
    if (kind == FP_NONE || (int)floor(q->sa + h.t * (q->sb - q->sa)) != fp.walls[i].floor)
        return;
    h.kind = kind;
    fp_push(&hits, &n_hits, &cap_hits, &h);
}

static const struct fp_grid *fp_cur_grid;

static void fp_query_cell(void *ctx, long cell)
{
    for (size_t k = fp_cur_grid->start[cell]; k < fp_cur_grid->start[cell + 1]; k++)
        fp_try(ctx, fp_cur_grid->items[k]);
}

// The candidate walls of storey g for the part [t0, t1] of the path
static void fp_query(const struct fp_grid *g, struct fp_q *q, double t0, double t1)
{
    fp.query++;
    if (fp.brute) {
        for (int i = g->first; i < g->first + g->n; i++)
            fp_try(q, i);
        return;
    }
    fp_cur_grid = g;
    fp_cells(g, q->p[0] + t0 * q->d[0], q->p[1] + t0 * q->d[1], q->p[0] + t1 * q->d[0], q->p[1] + t1 * q->d[1],
             fp_query_cell, q);
}

static const struct fp_grid *fp_grid_of(int floor)
{
    int lo = 0, hi = fp.n_grids - 1;

    while (lo <= hi) {
        int mid = (lo + hi) / 2;

        if (fp.grids[mid].floor == floor)
            return &fp.grids[mid];
        if (fp.grids[mid].floor < floor)
            lo = mid + 1;
        else
            hi = mid - 1;
    }
    return NULL;
}

static int fp_hit_wall_cmp(const void *a, const void *b)
{
    const struct fp_hit *x = a, *y = b;

    return (x->wall > y->wall) - (x->wall < y->wall);
}

// By t, then wall: the contacts of one vertex come together
static int fp_hit_t_cmp(const void *a, const void *b)
{
    const struct fp_hit *x = a, *y = b;

    if (x->t != y->t)
        return x->t < y->t ? -1 : 1;
    return (x->wall > y->wall) - (x->wall < y->wall);
}

// Of two hits on overlapping collinear walls, the one to drop: the lower
// loss, then the higher index
static bool fp_weaker(const struct fp_hit *a, const struct fp_hit *b)
{
    double la = fp.mats[fp.walls[a->wall].mat].db, lb = fp.mats[fp.walls[b->wall].mat].db;

    return la < lb || (la == lb && a->wall > b->wall);
}

// The walls the path really crosses, from the hits found (in any order):
// - crossings at one point by overlapping collinear walls count once, the
//   wall of higher loss;
// - at a vertex on the path where walls end (a corner, a T, a cross), the
//   walls form sectors; the path crosses from the sector it comes from to the
//   one it goes to through the walls on its left or those on its right: the
//   side of lower loss is counted (with only walls on one side, none) (a corner that the path only grazes: none;
//   a wall split at the vertex: one; a T: one).
static void fp_resolve(double plen)
{
    n_picked = 0;
    if (n_hits > 1)
        qsort(hits, n_hits, sizeof(*hits), fp_hit_t_cmp);
    for (int i = 0; i < n_hits; ) {
        int j = i;
        double sum[2] = { 0, 0 };
        int cnt[2] = { 0, 0 }, low[2] = { INT32_MAX, INT32_MAX }, side;

        // The hits at one point: the same t (within the tolerance)
        while (j < n_hits && (hits[j].t - hits[i].t) * plen < 2 * FP_TOL)
            j++;
        // Overlapping collinear walls among them: keep the stronger
        for (int a = i; a < j; a++)
            for (int b = a + 1; b < j; b++) {
                if (hits[a].kind == FP_NONE || hits[b].kind == FP_NONE || hits[a].kind != hits[b].kind ||
                    (hits[a].kind == FP_VERTEX && hits[a].side != hits[b].side) ||
                    !fp_collinear(&fp.walls[hits[a].wall], &fp.walls[hits[b].wall]))
                    continue;
                if (fp_weaker(&hits[a], &hits[b]))
                    hits[a].kind = FP_NONE;
                else
                    hits[b].kind = FP_NONE;
            }
        for (int a = i; a < j; a++)
            if (hits[a].kind == FP_CROSS) {
                fp_push(&picked, &n_picked, &cap_picked, &hits[a]);
            } else if (hits[a].kind == FP_VERTEX) {
                side = hits[a].side > 0;
                sum[side] += fp.mats[fp.walls[hits[a].wall].mat].db;
                cnt[side]++;
                low[side] = hits[a].wall < low[side] ? hits[a].wall : low[side];
            }
        // The vertex: the side of lower loss, then of fewer walls, then the
        // one with the lowest wall (whichever the path's direction)
        side = sum[1] < sum[0] || (sum[1] == sum[0] && (cnt[1] < cnt[0] || (cnt[1] == cnt[0] && low[1] < low[0])));
        for (int a = i; a < j; a++)
            if (hits[a].kind == FP_VERTEX && (hits[a].side > 0) == side) {
                struct fp_hit h = hits[a];

                h.w = 1;
                fp_push(&picked, &n_picked, &cap_picked, &h);
            }
        i = j;
    }
}

double fp_walls(const double a[2], double sa, const double b[2], double sb, double *n, int *count, char *list,
                size_t listsz)
{
    double d[2] = { b[0] - a[0], b[1] - a[1] }, plen = hypot(d[0], d[1]), db = 0, wn = 0;
    int flo = (int)floor(fmin(sa, sb)), fhi = (int)floor(fmax(sa, sb));
    struct fp_q q = { .p = a, .d = d, .plen = plen, .sa = sa, .sb = sb };
    size_t used = 0;

    n_hits = 0;
    n_picked = 0;
    if (plen >= FP_TOL && fp.n_walls) {
        // Each storey the path passes through, with its part of the path
        for (int f = flo; f <= fhi; f++) {
            const struct fp_grid *g = fp_grid_of(f);
            double t0 = 0, t1 = 1;

            if (!g)
                continue;
            if (sb != sa) {
                t0 = (f - sa) / (sb - sa);
                t1 = (f + 1 - sa) / (sb - sa);
                if (t0 > t1) {
                    double x = t0;
                    t0 = t1;
                    t1 = x;
                }
                t0 = fmax(0, t0 - 1e-9);
                t1 = fmin(1, t1 + 1e-9);
            }
            fp_query(g, &q, t0, t1);
        }
        fp_resolve(plen);
    }
    // In the walls' order: the same sum whichever way they were found
    if (n_picked > 1)
        qsort(picked, n_picked, sizeof(*picked), fp_hit_wall_cmp);
    if (list && listsz)
        list[0] = '\0';
    for (int k = 0; k < n_picked; k++) {
        const struct fp_wall *w = &fp.walls[picked[k].wall];

        db += picked[k].w * fp.mats[w->mat].db;
        wn += picked[k].w;
        if (list && used + 32 < listsz) {
            used += snprintf(list + used, listsz - used, "%s%d", k ? "," : "", w->line);
            if (w->edge >= 0)
                used += snprintf(list + used, listsz - used, ".%d", w->edge);
            if (picked[k].w != 1)
                used += snprintf(list + used, listsz - used, ":%g", picked[k].w);
        } else if (list && used + 4 < listsz && !strstr(list, "...")) {
            used += snprintf(list + used, listsz - used, ",...");
        }
    }
    if (list && listsz > 1 && !n_picked)
        snprintf(list, listsz, "-");
    *n = wn;
    *count = n_picked;
    return db;
}
