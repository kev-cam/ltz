/*
 * qspice-shim — stands in for QSPICE64.exe / QSPICE80.exe so the QSPICE GUI
 * (QUX.exe) can drive Xyce as its simulation engine.
 *
 * The installer renames the real engine to <name>.real.exe and copies this
 * shim in as <name>.exe. Behavior is controlled by the first line of
 *   %LOCALAPPDATA%\qspice-shim\mode.txt
 *     passthru   (default) log the invocation, run the real engine
 *     xyce       run the WSL Xyce bridge; fall back to the real engine if
 *                the bridge exits nonzero (the GUI never breaks)
 *
 * Every invocation appends a record to %LOCALAPPDATA%\qspice-shim\shim.log:
 * command line, working directory, whether stdin is a pipe (QUX pipes the
 * netlist for marching waveforms), and the chosen route. That log is the
 * protocol-discovery instrument for wiring the bridge.
 */
#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>

#define BRIDGE_CMD_FMT \
  "wsl.exe -- bash /mnt/c/cygwin64/usr/local/src/ltz/qshim/bridge.sh %s"

static char logdir[MAX_PATH];

static void ensure_logdir(void)
{
    char base[MAX_PATH];
    DWORD n = GetEnvironmentVariableA("LOCALAPPDATA", base, sizeof base);
    if (n == 0 || n >= sizeof base) strcpy(base, "C:\\Temp");
    snprintf(logdir, sizeof logdir, "%s\\qspice-shim", base);
    CreateDirectoryA(logdir, NULL);
}

static void logline(const char *fmt, ...)
{
    char path[MAX_PATH];
    snprintf(path, sizeof path, "%s\\shim.log", logdir);
    FILE *f = fopen(path, "a");
    if (!f) return;
    SYSTEMTIME st; GetLocalTime(&st);
    fprintf(f, "[%04d-%02d-%02d %02d:%02d:%02d] ",
            st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    va_list ap; va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static void read_mode(char *mode, size_t sz)
{
    char path[MAX_PATH];
    snprintf(path, sizeof path, "%s\\mode.txt", logdir);
    strcpy(mode, "passthru");
    FILE *f = fopen(path, "r");
    if (!f) return;
    if (fgets(mode, (int)sz, f)) {
        char *e = mode + strlen(mode);
        while (e > mode && (e[-1] == '\n' || e[-1] == '\r' || e[-1] == ' ')) *--e = 0;
    }
    fclose(f);
    if (!mode[0]) strcpy(mode, "passthru");
}

/* command line with the leading program token removed */
static const char *cmdline_args(void)
{
    const char *c = GetCommandLineA();
    if (*c == '"') { c++; while (*c && *c != '"') c++; if (*c) c++; }
    else           { while (*c && *c != ' ' && *c != '\t') c++; }
    while (*c == ' ' || *c == '\t') c++;
    return c;
}

static DWORD run(const char *cmdline)
{
    STARTUPINFOA si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    /* inherit std handles: QUX's netlist pipe and output flow through */
    char *cl = _strdup(cmdline);
    if (!CreateProcessA(NULL, cl, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        logline("CreateProcess FAILED (%lu): %s", GetLastError(), cmdline);
        free(cl);
        return 127;
    }
    free(cl);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD rc = 1;
    GetExitCodeProcess(pi.hProcess, &rc);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    return rc;
}

int main(void)
{
    ensure_logdir();

    char self[MAX_PATH];
    GetModuleFileNameA(NULL, self, sizeof self);

    char real[MAX_PATH + 16];
    snprintf(real, sizeof real, "%s", self);
    char *dot = strrchr(real, '.');
    if (dot && _stricmp(dot, ".exe") == 0) *dot = 0;
    strcat(real, ".real.exe");

    char mode[64];
    read_mode(mode, sizeof mode);

    const char *args = cmdline_args();
    char cwd[MAX_PATH];
    GetCurrentDirectoryA(sizeof cwd, cwd);
    DWORD stype = GetFileType(GetStdHandle(STD_INPUT_HANDLE));

    logline("invoked self=%s mode=%s cwd=%s stdin=%s args=[%s]",
            self, mode,
            cwd,
            stype == FILE_TYPE_PIPE ? "PIPE" : stype == FILE_TYPE_DISK ? "FILE" : "other",
            args);

    char cmd[8192];

    if (_stricmp(mode, "xyce") == 0) {
        snprintf(cmd, sizeof cmd, BRIDGE_CMD_FMT, args);
        logline("route=xyce: %s", cmd);
        DWORD rc = run(cmd);
        logline("xyce bridge rc=%lu", rc);
        if (rc == 0) return 0;
        logline("bridge failed; falling back to real engine");
    }

    if (GetFileAttributesA(real) == INVALID_FILE_ATTRIBUTES) {
        logline("real engine missing: %s", real);
        fprintf(stderr, "qspice-shim: %s not found\n", real);
        return 127;
    }
    snprintf(cmd, sizeof cmd, "\"%s\" %s", real, args);
    DWORD rc = run(cmd);
    logline("route=passthru rc=%lu", rc);
    return (int)rc;
}
