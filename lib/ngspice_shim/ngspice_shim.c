/*
 * ngspice API shim — drop-in libngspice.so replacement backed by Xyce.
 *
 * KiCad dlopen's libngspice.so and calls these functions.  We implement
 * them by driving Xyce through its C interface (libxycecinterface.so).
 *
 * Data flow:
 *   ngSpice_Circ()          → write netlist to temp file → xyce_initialize()
 *   ngSpice_Command("bg_run") → spawn thread: simulateUntil() loop,
 *                               accumulate all .PRINT vectors
 *   ngSpice_running()       → check thread-running flag
 *   ngSpice_CurPlot()       → return "tran1" / "ac1" / etc.
 *   ngSpice_AllVecs()       → return vector name list
 *   ngGet_Vec_Info()        → return pointer to accumulated data
 *
 * Copyright 2026 ltz project.  GPL-3.0-or-later.
 */

#ifndef SHARED_MODULE
#define SHARED_MODULE  /* so sharedspice.h uses _Bool for NG_BOOL */
#endif

#include <ngspice/sharedspice.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <pthread.h>
#include <unistd.h>
#include <math.h>
#include <ctype.h>

/* ------------------------------------------------------------------ */
/* Xyce C interface (from libxycecinterface.so)                       */
/* ------------------------------------------------------------------ */
extern void  xyce_open(void **ptr);
extern void  xyce_close(void **ptr);
extern int   xyce_initialize(void **ptr, int argc, char **argv);
extern int   xyce_runSimulation(void **ptr);
extern int   xyce_simulateUntil(void **ptr, double t, double *actual);
extern _Bool xyce_simulationComplete(void **ptr);
extern double xyce_getTime(void **ptr);
extern double xyce_getFinalTime(void **ptr);
extern int   xyce_obtainResponse(void **ptr, char *name, double *val);
extern _Bool xyce_checkCircuitParameterExists(void **ptr, char *name);
extern int   xyce_getMemBufData(void **ptr, const char **data, int *length);
extern void  xyce_advanceMemBufRead(void **ptr, int n);

/* ------------------------------------------------------------------ */
/* Internal state                                                     */
/* ------------------------------------------------------------------ */

/* Callbacks from KiCad */
static SendChar         *cb_sendchar;
static SendStat         *cb_sendstat;
static ControlledExit   *cb_exit;
static SendData         *cb_senddata;
static SendInitData     *cb_sendinit;
static BGThreadRunning  *cb_bgtrun;
static void             *cb_userdata;

/* Xyce instance */
static void *xyce_ptr = NULL;
static _Bool xyce_initialized = false;

/* Netlist storage */
static char *stored_netlist = NULL;

/* Simulation thread */
static pthread_t        sim_thread;
static volatile _Bool   sim_running = false;
static volatile _Bool   sim_halt    = false;
static pthread_mutex_t  sim_mutex   = PTHREAD_MUTEX_INITIALIZER;

/* ------------------------------------------------------------------ */
/* Vector / plot storage                                              */
/* ------------------------------------------------------------------ */

#define MAX_VECTORS   256
#define MAX_PLOTS     16
#define MAX_NAME_LEN  128

/* Simulation type (detected from netlist) */
typedef enum {
    SIM_TRAN = 0,
    SIM_AC,
    SIM_DC,
    SIM_OP,
    SIM_UNKNOWN
} sim_type_t;

/* A single stored vector */
typedef struct {
    char        name[MAX_NAME_LEN];
    double     *realdata;
    ngcomplex_t *compdata;
    int         length;
    int         capacity;
    int         type;       /* 0=real, 1=complex */
    _Bool       is_scale;
} stored_vec_t;

/* A plot (collection of vectors from one simulation run) */
typedef struct {
    char         name[MAX_NAME_LEN];   /* "tran1", "ac1", etc. */
    char         title[256];
    char         date[64];
    char         type_str[32];         /* "transient", "ac", etc. */
    sim_type_t   sim_type;
    stored_vec_t vecs[MAX_VECTORS];
    int          nvecs;
} stored_plot_t;

static stored_plot_t plots[MAX_PLOTS];
static int           nplots = 0;
static int           cur_plot = -1;

/* Static vector_info returned by ngGet_Vec_Info (reused between calls) */
static vector_info   ret_vecinfo;

/* Static arrays for AllPlots / AllVecs return values */
static char *all_plots_arr[MAX_PLOTS + 1];
static char *all_vecs_arr[MAX_VECTORS + 1];

/* Temp file for netlist */
static char netlist_tmpfile[512] = "";

/* ------------------------------------------------------------------ */
/* Helpers                                                            */
/* ------------------------------------------------------------------ */

static void send_msg(const char *msg)
{
    if (cb_sendchar)
        cb_sendchar((char *)msg, 0, cb_userdata);
}

/* Find or create a plot by name */
static stored_plot_t *get_or_create_plot(const char *name, sim_type_t st)
{
    for (int i = 0; i < nplots; i++) {
        if (strcmp(plots[i].name, name) == 0)
            return &plots[i];
    }
    if (nplots >= MAX_PLOTS)
        return NULL;
    stored_plot_t *p = &plots[nplots++];
    memset(p, 0, sizeof(*p));
    snprintf(p->name, sizeof(p->name), "%s", name);
    p->sim_type = st;
    switch (st) {
    case SIM_TRAN: snprintf(p->type_str, sizeof(p->type_str), "transient"); break;
    case SIM_AC:   snprintf(p->type_str, sizeof(p->type_str), "ac");        break;
    case SIM_DC:   snprintf(p->type_str, sizeof(p->type_str), "dc");        break;
    case SIM_OP:   snprintf(p->type_str, sizeof(p->type_str), "operating point"); break;
    default:       snprintf(p->type_str, sizeof(p->type_str), "unknown");   break;
    }
    snprintf(p->title, sizeof(p->title), "Xyce simulation");
    snprintf(p->date, sizeof(p->date), "today");
    cur_plot = nplots - 1;
    return p;
}

/* Find a vector in a plot; create if not found */
static stored_vec_t *get_or_create_vec(stored_plot_t *plot, const char *name,
                                       _Bool is_complex, _Bool is_scale)
{
    for (int i = 0; i < plot->nvecs; i++) {
        if (strcasecmp(plot->vecs[i].name, name) == 0)
            return &plot->vecs[i];
    }
    if (plot->nvecs >= MAX_VECTORS)
        return NULL;
    stored_vec_t *v = &plot->vecs[plot->nvecs++];
    memset(v, 0, sizeof(*v));
    snprintf(v->name, sizeof(v->name), "%s", name);
    v->type = is_complex ? 1 : 0;
    v->is_scale = is_scale;
    v->capacity = 4096;
    if (is_complex)
        v->compdata = calloc(v->capacity, sizeof(ngcomplex_t));
    else
        v->realdata = calloc(v->capacity, sizeof(double));
    return v;
}

/* Append a real value to a vector */
static void vec_append_real(stored_vec_t *v, double val)
{
    if (v->length >= v->capacity) {
        v->capacity *= 2;
        v->realdata = realloc(v->realdata, v->capacity * sizeof(double));
    }
    v->realdata[v->length++] = val;
}

/* Append a complex value to a vector */
static void vec_append_complex(stored_vec_t *v, double re, double im)
{
    if (v->length >= v->capacity) {
        v->capacity *= 2;
        v->compdata = realloc(v->compdata, v->capacity * sizeof(ngcomplex_t));
    }
    v->compdata[v->length].cx_real = re;
    v->compdata[v->length].cx_imag = im;
    v->length++;
}

/* Free all vectors in all plots */
static void clear_all_plots(void)
{
    for (int i = 0; i < nplots; i++) {
        for (int j = 0; j < plots[i].nvecs; j++) {
            free(plots[i].vecs[j].realdata);
            free(plots[i].vecs[j].compdata);
        }
    }
    memset(plots, 0, sizeof(plots));
    nplots = 0;
    cur_plot = -1;
}

/* Detect simulation type from netlist text */
static sim_type_t detect_sim_type(const char *netlist)
{
    const char *p = netlist;
    while (p && *p) {
        /* skip to start of line */
        while (*p && (*p == ' ' || *p == '\t'))
            p++;
        if (*p == '.') {
            if (strncasecmp(p, ".tran", 5) == 0 && !isalpha(p[5]))
                return SIM_TRAN;
            if (strncasecmp(p, ".ac", 3) == 0 && !isalpha(p[3]))
                return SIM_AC;
            if (strncasecmp(p, ".dc", 3) == 0 && !isalpha(p[3]))
                return SIM_DC;
            if (strncasecmp(p, ".op", 3) == 0 && !isalpha(p[3]))
                return SIM_OP;
        }
        /* advance to next line */
        while (*p && *p != '\n')
            p++;
        if (*p == '\n')
            p++;
    }
    return SIM_UNKNOWN;
}

/* Generate a plot name like "tran1", "ac1", etc. */
static void make_plot_name(sim_type_t st, char *buf, int buflen)
{
    const char *prefix;
    switch (st) {
    case SIM_TRAN: prefix = "tran"; break;
    case SIM_AC:   prefix = "ac";   break;
    case SIM_DC:   prefix = "dc";   break;
    case SIM_OP:   prefix = "op";   break;
    default:       prefix = "unknown"; break;
    }
    /* Count existing plots of this type */
    int n = 0;
    for (int i = 0; i < nplots; i++) {
        if (strncmp(plots[i].name, prefix, strlen(prefix)) == 0)
            n++;
    }
    snprintf(buf, buflen, "%s%d", prefix, n + 1);
}

/* Write netlist string to a temp file, return path */
static const char *write_netlist_tmpfile(const char *netlist)
{
    if (netlist_tmpfile[0] == '\0') {
        snprintf(netlist_tmpfile, sizeof(netlist_tmpfile),
                 "/tmp/ltz_ngshim_%d.cir", (int)getpid());
    }
    FILE *fp = fopen(netlist_tmpfile, "w");
    if (!fp) return NULL;
    fputs(netlist, fp);
    fclose(fp);
    return netlist_tmpfile;
}

/* mem:// base path for this process */
static char membuf_path[512] = "";

static const char *get_membuf_path(void)
{
    if (membuf_path[0] == '\0')
        snprintf(membuf_path, sizeof(membuf_path),
                 "/tmp/ltz_shim_%d", (int)getpid());
    return membuf_path;
}

/* Inject FILE=mem://... FORMAT=CSV into all .PRINT lines.
 * If no .PRINT exists, add one before .END.
 * Also removes any existing FILE= directives. */
static char *inject_mem_print(const char *netlist, sim_type_t st)
{
    const char *mem_path = get_membuf_path();
    size_t len = strlen(netlist);
    /* Generous allocation */
    char *result = malloc(len + 1024);
    if (!result) return strdup(netlist);
    result[0] = '\0';

    _Bool found_print = false;
    const char *p = netlist;

    while (p && *p) {
        const char *line_start = p;
        /* Find end of line */
        const char *line_end = p;
        while (*line_end && *line_end != '\n') line_end++;

        /* Check if this is a .PRINT line */
        const char *q = p;
        while (*q == ' ' || *q == '\t') q++;

        if (*q == '.' && strncasecmp(q, ".print", 6) == 0) {
            found_print = true;

            /* Reconstruct: .PRINT <analysis> FORMAT=CSV FILE=mem://... <vectors>
             * Must put directives BEFORE vector names. */

            /* Skip ".PRINT" */
            q += 6;
            while (q < line_end && (*q == ' ' || *q == '\t')) q++;

            /* Grab analysis type word (TRAN, AC, DC, etc.) */
            char analysis_word[32];
            int aw = 0;
            while (q < line_end && *q != ' ' && *q != '\t' && *q != '\n'
                   && aw < (int)sizeof(analysis_word) - 1)
                analysis_word[aw++] = *q++;
            analysis_word[aw] = '\0';

            /* Collect remaining tokens, skipping existing FILE= and FORMAT= */
            char vectors[4096];
            int vl = 0;
            while (q < line_end) {
                while (q < line_end && (*q == ' ' || *q == '\t')) q++;
                if (q >= line_end || *q == '\n') break;

                /* Check if this token is FILE= or FORMAT= */
                if (strncasecmp(q, "FILE=", 5) == 0 ||
                    strncasecmp(q, "FORMAT=", 7) == 0) {
                    while (q < line_end && *q != ' ' && *q != '\t' && *q != '\n')
                        q++;
                    continue;
                }

                /* Copy this token */
                if (vl > 0) vectors[vl++] = ' ';
                while (q < line_end && *q != ' ' && *q != '\t' && *q != '\n'
                       && vl < (int)sizeof(vectors) - 1)
                    vectors[vl++] = *q++;
            }
            vectors[vl] = '\0';

            /* Build the new .PRINT line */
            char inject[4096];
            snprintf(inject, sizeof(inject),
                     ".PRINT %s FORMAT=CSV FILE=mem://%s %s\n",
                     analysis_word, mem_path, vectors);
            strcat(result, inject);
        } else {
            /* Copy line as-is */
            size_t ll = line_end - line_start;
            size_t rlen = strlen(result);
            memcpy(result + rlen, line_start, ll);
            result[rlen + ll] = '\0';
            if (*line_end == '\n')
                strcat(result, "\n");
        }

        p = line_end;
        if (*p == '\n') p++;
    }

    /* If no .PRINT found, inject one before .END */
    if (!found_print) {
        const char *analysis;
        switch (st) {
        case SIM_TRAN: analysis = "TRAN"; break;
        case SIM_AC:   analysis = "AC";   break;
        case SIM_DC:   analysis = "DC";   break;
        default:       analysis = "TRAN"; break;
        }

        /* Find .END in result and insert before it */
        char *end_pos = NULL;
        char *rp = result;
        while (*rp) {
            char *ls = rp;
            while (*rp == ' ' || *rp == '\t') rp++;
            if (*rp == '.' && strncasecmp(rp, ".end", 4) == 0 &&
                (rp[4] == '\0' || rp[4] == '\n' || rp[4] == '\r' || rp[4] == ' ')) {
                end_pos = ls;
                break;
            }
            while (*rp && *rp != '\n') rp++;
            if (*rp == '\n') rp++;
        }

        if (end_pos) {
            /* Shift .END forward to make room */
            char tail[4096];
            strncpy(tail, end_pos, sizeof(tail) - 1);
            tail[sizeof(tail) - 1] = '\0';
            sprintf(end_pos, ".PRINT %s FORMAT=CSV FILE=mem://%s\n%s",
                    analysis, mem_path, tail);
        }
    }

    return result;
}

/* ------------------------------------------------------------------ */
/* Background simulation thread                                       */
/* ------------------------------------------------------------------ */

/* Parse CSV data from mem:// buffer into plot vectors.
 * CSV format: first line is header "TIME,V(in),V(out),..."
 * subsequent lines are comma-separated doubles.
 */
static void parse_csv_into_plot(stored_plot_t *plot, const char *csv, size_t len,
                                _Bool is_ac)
{
    char col_names[MAX_VECTORS][MAX_NAME_LEN];
    _Bool header_seen = false;
    const char *p = csv;
    const char *end = csv + len;
    char line[4096];

    memset(col_names, 0, sizeof(col_names));

    while (p < end) {
        /* Extract one line */
        int ll = 0;
        while (p < end && *p != '\n' && ll < (int)sizeof(line) - 1)
            line[ll++] = *p++;
        line[ll] = '\0';
        if (p < end && *p == '\n') p++;
        if (ll == 0) continue;

        /* Parse header line */
        if (!header_seen && (strncasecmp(line, "TIME", 4) == 0 ||
                             strncasecmp(line, "FREQ", 4) == 0)) {
            /* Parse column names from header */
            int ci = 0;
            char *tok = strtok(line, ",");
            while (tok && ci < MAX_VECTORS) {
                /* Strip leading/trailing whitespace */
                while (*tok == ' ') tok++;
                strncpy(col_names[ci], tok, MAX_NAME_LEN - 1);
                col_names[ci][MAX_NAME_LEN - 1] = '\0';
                /* Remove trailing whitespace/CR */
                int tl = strlen(col_names[ci]);
                while (tl > 0 && (col_names[ci][tl-1] == ' ' ||
                                   col_names[ci][tl-1] == '\r'))
                    col_names[ci][--tl] = '\0';
                ci++;
                tok = strtok(NULL, ",");
            }
            (void)ci;

            /* Create vectors for each column */
            for (int i = 0; i < ci; i++) {
                /* Lowercase the name for ngspice compatibility */
                for (char *c = col_names[i]; *c; c++)
                    *c = tolower(*c);
                get_or_create_vec(plot, col_names[i], is_ac, i == 0);
            }
            header_seen = true;
            continue;
        }

        if (!header_seen) continue;

        /* Parse data line */
        double vals[MAX_VECTORS];
        int nvals = 0;
        char *saveptr;
        char *tok = strtok_r(line, ",", &saveptr);
        while (tok && nvals < MAX_VECTORS) {
            vals[nvals++] = strtod(tok, NULL);
            tok = strtok_r(NULL, ",", &saveptr);
        }

        /* Append to vectors */
        for (int i = 0; i < nvals && i < plot->nvecs; i++) {
            if (is_ac)
                vec_append_complex(&plot->vecs[i], vals[i], 0.0);
            else
                vec_append_real(&plot->vecs[i], vals[i]);
        }
    }
}

static void *sim_thread_func(void *arg)
{
    (void)arg;

    sim_type_t st = detect_sim_type(stored_netlist);
    _Bool is_ac = (st == SIM_AC);

    /* Create plot */
    char plot_name[64];
    make_plot_name(st, plot_name, sizeof(plot_name));
    stored_plot_t *plot = get_or_create_plot(plot_name, st);
    if (!plot) {
        send_msg("stderr Error: too many plots\n");
        goto done;
    }

    /* Run the full simulation — Xyce accumulates CSV data in mem:// buffer */
    if (!xyce_initialized) {
        send_msg("stderr Error: no circuit loaded\n");
        goto done;
    }
    xyce_runSimulation(&xyce_ptr);

    /* Read all data from the mem:// buffer */
    {
        const char *data = NULL;
        int length = 0;
        if (xyce_getMemBufData(&xyce_ptr, &data, &length) == 1 && length > 0) {
            parse_csv_into_plot(plot, data, length, is_ac);
            xyce_advanceMemBufRead(&xyce_ptr, length);
        }
    }

    /* Send init data callback with final vector info */
    if (cb_sendinit && plot->nvecs > 0) {
        vecinfoall via;
        memset(&via, 0, sizeof(via));
        via.name = plot->name;
        via.title = plot->title;
        via.date = plot->date;
        via.type = plot->type_str;
        via.veccount = plot->nvecs;
        pvecinfo *vi_arr = calloc(plot->nvecs, sizeof(pvecinfo));
        for (int i = 0; i < plot->nvecs; i++) {
            vecinfo *vi = calloc(1, sizeof(vecinfo));
            vi->number = i;
            vi->vecname = plot->vecs[i].name;
            vi->is_real = !is_ac;
            vi_arr[i] = vi;
        }
        via.vecs = vi_arr;
        cb_sendinit(&via, 0, cb_userdata);
        for (int i = 0; i < plot->nvecs; i++)
            free(vi_arr[i]);
        free(vi_arr);
    }

done:
    pthread_mutex_lock(&sim_mutex);
    sim_running = false;
    pthread_mutex_unlock(&sim_mutex);

    if (cb_bgtrun)
        cb_bgtrun(true, 0, cb_userdata);  /* true = finished */

    return NULL;
}

/* ------------------------------------------------------------------ */
/* Exported ngspice API                                               */
/* ------------------------------------------------------------------ */

IMPEXP
int ngSpice_Init(SendChar *printfcn, SendStat *statfcn, ControlledExit *ngexit,
                 SendData *sdata, SendInitData *sinitdata,
                 BGThreadRunning *bgtrun, void *userData)
{
    cb_sendchar = printfcn;
    cb_sendstat = statfcn;
    cb_exit     = ngexit;
    cb_senddata = sdata;
    cb_sendinit = sinitdata;
    cb_bgtrun   = bgtrun;
    cb_userdata = userData;

    /* Initialize Xyce */
    if (!xyce_initialized) {
        xyce_open(&xyce_ptr);
        xyce_initialized = true;
    }

    send_msg("stdout ltz ngspice shim (Xyce backend) initialized\n");
    return 0;
}

IMPEXP
int ngSpice_Init_Sync(GetVSRCData *vsrcdat, GetISRCData *isrcdat,
                      GetSyncData *syncdat, int *ident, void *userData)
{
    /* Not implemented — KiCad doesn't use this */
    (void)vsrcdat; (void)isrcdat; (void)syncdat; (void)ident; (void)userData;
    return 0;
}

IMPEXP
int ngSpice_Circ(char **circarray)
{
    if (!circarray)
        return 1;

    /* Concatenate lines into a single netlist string */
    size_t total = 0;
    for (char **p = circarray; *p; p++)
        total += strlen(*p) + 1;  /* +1 for newline */

    free(stored_netlist);
    stored_netlist = malloc(total + 1);
    if (!stored_netlist)
        return 1;

    stored_netlist[0] = '\0';
    for (char **p = circarray; *p; p++) {
        strcat(stored_netlist, *p);
        strcat(stored_netlist, "\n");
    }

    sim_type_t st = detect_sim_type(stored_netlist);

    /* Skip trivial/empty circuits (KiCad sends [*, .end] on startup) */
    if (st == SIM_UNKNOWN) {
        send_msg("stdout Circuit stored (no analysis yet)\n");
        return 0;
    }

    /* Inject FILE=mem:// FORMAT=CSV into .PRINT lines */
    char *nl = inject_mem_print(stored_netlist, st);
    free(stored_netlist);
    stored_netlist = nl;

    /* Write to temp file and initialize Xyce */
    const char *tmpf = write_netlist_tmpfile(stored_netlist);
    if (!tmpf)
        return 1;

    /* Close and reopen Xyce for fresh state */
    if (xyce_initialized) {
        xyce_close(&xyce_ptr);
        xyce_ptr = NULL;
        xyce_initialized = false;
    }
    xyce_open(&xyce_ptr);
    xyce_initialized = true;

    char *argv[] = { "Xyce", "-quiet", (char *)tmpf };
    int rc = xyce_initialize(&xyce_ptr, 3, argv);

    if (rc != 1) {
        send_msg("stderr Error: Xyce failed to initialize circuit\n");
        /* Xyce is in bad state after failed init — tear it down */
        xyce_close(&xyce_ptr);
        xyce_ptr = NULL;
        xyce_initialized = false;
        return 1;
    }

    send_msg("stdout Circuit loaded successfully\n");
    return 0;
}

IMPEXP
int ngSpice_Command(char *command)
{
    if (!command)
        return 1;

    /* Skip leading whitespace */
    while (*command == ' ' || *command == '\t')
        command++;

    if (strncasecmp(command, "bg_run", 6) == 0 ||
        strcmp(command, "run") == 0) {
        /* Start simulation in background thread */
        pthread_mutex_lock(&sim_mutex);
        if (sim_running) {
            pthread_mutex_unlock(&sim_mutex);
            send_msg("stderr Simulation already running\n");
            return 1;
        }
        sim_running = true;
        sim_halt = false;
        pthread_mutex_unlock(&sim_mutex);

        if (cb_bgtrun)
            cb_bgtrun(false, 0, cb_userdata);  /* false = started */

        pthread_create(&sim_thread, NULL, sim_thread_func, NULL);
        pthread_detach(sim_thread);
        return 0;
    }

    if (strncasecmp(command, "bg_halt", 7) == 0 ||
        strcmp(command, "halt") == 0 ||
        strcmp(command, "stop") == 0) {
        sim_halt = true;
        return 0;
    }

    if (strcmp(command, "reset") == 0) {
        /* Reset — reload the circuit if we have a real one */
        if (stored_netlist && xyce_initialized &&
            detect_sim_type(stored_netlist) != SIM_UNKNOWN) {
            xyce_close(&xyce_ptr);
            xyce_ptr = NULL;
            xyce_initialized = false;
            xyce_open(&xyce_ptr);
            xyce_initialized = true;

            const char *tmpf = write_netlist_tmpfile(stored_netlist);
            if (tmpf) {
                char *argv[] = { "Xyce", "-quiet", (char *)tmpf };
                if (xyce_initialize(&xyce_ptr, 3, argv) != 1) {
                    xyce_close(&xyce_ptr);
                    xyce_ptr = NULL;
                    xyce_initialized = false;
                }
            }
        }
        return 0;
    }

    if (strcmp(command, "remcirc") == 0) {
        clear_all_plots();
        return 0;
    }

    if (strncmp(command, "destroy", 7) == 0) {
        clear_all_plots();
        return 0;
    }

    if (strcmp(command, "quit") == 0) {
        if (cb_exit)
            cb_exit(0, false, true, 0, cb_userdata);
        return 0;
    }

    /* "set" / "unset" commands — silently accept */
    if (strncmp(command, "set ", 4) == 0 ||
        strncmp(command, "unset ", 6) == 0) {
        return 0;
    }

    /* "echo" — forward to SendChar */
    if (strncmp(command, "echo ", 5) == 0) {
        char buf[1024];
        snprintf(buf, sizeof(buf), "stdout %s\n", command + 5);
        send_msg(buf);
        return 0;
    }

    /* "esave" — silently accept */
    if (strncmp(command, "esave", 5) == 0)
        return 0;

    /* "codemodel" — silently accept (we don't need ngspice codemodels) */
    if (strncmp(command, "codemodel", 9) == 0)
        return 0;

    /* Unknown command — log and accept */
    {
        char buf[1024];
        snprintf(buf, sizeof(buf),
                 "stdout ltz shim: ignoring command '%s'\n", command);
        send_msg(buf);
    }
    return 0;
}

IMPEXP
pvector_info ngGet_Vec_Info(char *vecname)
{
    if (!vecname || cur_plot < 0 || cur_plot >= nplots)
        return NULL;

    /* Check if name has plot prefix: "plotname.vecname" */
    stored_plot_t *plot = &plots[cur_plot];
    char *dot = strchr(vecname, '.');
    const char *vname = vecname;

    if (dot) {
        /* Find the specified plot */
        char pname[MAX_NAME_LEN];
        int plen = dot - vecname;
        if (plen >= MAX_NAME_LEN) plen = MAX_NAME_LEN - 1;
        memcpy(pname, vecname, plen);
        pname[plen] = '\0';
        vname = dot + 1;

        for (int i = 0; i < nplots; i++) {
            if (strcasecmp(plots[i].name, pname) == 0) {
                plot = &plots[i];
                break;
            }
        }
    }

    /* Find the vector */
    for (int i = 0; i < plot->nvecs; i++) {
        if (strcasecmp(plot->vecs[i].name, vname) == 0) {
            stored_vec_t *v = &plot->vecs[i];
            ret_vecinfo.v_name = v->name;
            ret_vecinfo.v_type = 0;
            ret_vecinfo.v_flags = v->type ? 0x02 : 0x01;  /* VF_COMPLEX : VF_REAL */
            ret_vecinfo.v_realdata = v->realdata;
            ret_vecinfo.v_compdata = v->compdata;
            ret_vecinfo.v_length = v->length;
            return &ret_vecinfo;
        }
    }

    return NULL;
}

IMPEXP
char *ngSpice_CurPlot(void)
{
    if (cur_plot >= 0 && cur_plot < nplots)
        return plots[cur_plot].name;
    return "const";
}

IMPEXP
char **ngSpice_AllPlots(void)
{
    for (int i = 0; i < nplots; i++)
        all_plots_arr[i] = plots[i].name;
    all_plots_arr[nplots] = NULL;
    return all_plots_arr;
}

IMPEXP
char **ngSpice_AllVecs(char *plotname)
{
    stored_plot_t *plot = NULL;

    if (plotname) {
        for (int i = 0; i < nplots; i++) {
            if (strcasecmp(plots[i].name, plotname) == 0) {
                plot = &plots[i];
                break;
            }
        }
    }
    if (!plot && cur_plot >= 0)
        plot = &plots[cur_plot];
    if (!plot) {
        all_vecs_arr[0] = NULL;
        return all_vecs_arr;
    }

    for (int i = 0; i < plot->nvecs; i++)
        all_vecs_arr[i] = plot->vecs[i].name;
    all_vecs_arr[plot->nvecs] = NULL;
    return all_vecs_arr;
}

IMPEXP
NG_BOOL ngSpice_running(void)
{
    return sim_running;
}

IMPEXP
NG_BOOL ngSpice_SetBkpt(double time)
{
    (void)time;
    return false;
}

IMPEXP
char *ngCM_Input_Path(const char *path)
{
    (void)path;
    return NULL;
}

/* Optional realloc lock functions — KiCad checks for these */
IMPEXP
int ngSpice_LockRealloc(void)
{
    pthread_mutex_lock(&sim_mutex);
    return 0;
}

IMPEXP
int ngSpice_UnlockRealloc(void)
{
    pthread_mutex_unlock(&sim_mutex);
    return 0;
}

/* ------------------------------------------------------------------ */
/* Library constructor/destructor                                     */
/* ------------------------------------------------------------------ */

static void cleanup_membuf_dir(void)
{
    if (membuf_path[0]) {
        char cmd[1024];
        snprintf(cmd, sizeof(cmd), "rm -rf %s", membuf_path);
        if (system(cmd) != 0) { /* best effort */ }
    }
}

__attribute__((destructor))
static void shim_cleanup(void)
{
    if (xyce_initialized) {
        xyce_close(&xyce_ptr);
        xyce_ptr = NULL;
        xyce_initialized = false;
    }
    free(stored_netlist);
    stored_netlist = NULL;
    clear_all_plots();
    if (netlist_tmpfile[0])
        unlink(netlist_tmpfile);
    cleanup_membuf_dir();
}
