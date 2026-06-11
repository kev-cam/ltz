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

/* Args cross the Windows->WSL boundary via a file: backslashes and quoted
 * paths with spaces do not survive wsl.exe argv marshaling, but a bare PID
 * does. The bridge reads and parses the original cmdline tail itself. */
#define ARGS_DIR  "C:\\cygwin64\\tmp\\qshim-args"
#define BRIDGE_CMD_FMT \
  "wsl.exe -- bash /mnt/c/cygwin64/usr/local/src/ltz/qshim/bridge.sh --pid %lu"

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

/* Run a child. If stdin_file is non-NULL the child's stdin is that file
 * (lets both the bridge and a post-failure fallback read the same captured
 * netlist -- a pipe can only be drained once). */
static DWORD run(const char *cmdline, const char *stdin_file)
{
    STARTUPINFOA si; PROCESS_INFORMATION pi;
    ZeroMemory(&si, sizeof si); si.cb = sizeof si;
    HANDLE hin = INVALID_HANDLE_VALUE;
    if (stdin_file) {
        SECURITY_ATTRIBUTES sa = { sizeof sa, NULL, TRUE };
        hin = CreateFileA(stdin_file, GENERIC_READ, FILE_SHARE_READ, &sa,
                          OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (hin == INVALID_HANDLE_VALUE) {
            logline("cannot reopen stdin file %s (%lu)", stdin_file, GetLastError());
            return 127;
        }
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput  = hin;
        si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
        si.hStdError  = GetStdHandle(STD_ERROR_HANDLE);
    }
    char *cl = _strdup(cmdline);
    BOOL ok = CreateProcessA(NULL, cl, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi);
    free(cl);
    if (hin != INVALID_HANDLE_VALUE) CloseHandle(hin);
    if (!ok) {
        logline("CreateProcess FAILED (%lu): %s", GetLastError(), cmdline);
        return 127;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD rc = 1;
    GetExitCodeProcess(pi.hProcess, &rc);
    CloseHandle(pi.hProcess); CloseHandle(pi.hThread);
    return rc;
}

/* QUX learns sim progress/completion from window messages the engine posts
 * to the -hWnd handle (discovered with qspy.c): msg 0x8100 streams console
 * text 8 chars at a time (4 LE bytes in wParam + 4 in lParam), terminated by
 * a (0,0) post; 0x80FF marks the start. Without these QUX treats the run as
 * dead and never loads the rawfile. */
static void post_text(HWND hw, const char *s)
{
    size_t n = strlen(s);
    for (size_t i = 0; i < n; i += 8) {
        unsigned int w = 0, l = 0;
        size_t left = n - i;
        memcpy(&w, s + i, left > 4 ? 4 : left);
        if (left > 4) memcpy(&l, s + i + 4, left - 4 > 4 ? 4 : left - 4);
        PostMessageA(hw, 0x8100, (WPARAM)w, (LPARAM)l);
    }
    PostMessageA(hw, 0x8100, 0, 0);
}

static void notify_qux(const char *args, double secs)
{
    const char *hp = strstr(args, "-hWnd=");
    if (!hp) return;
    HWND hw = (HWND)(UINT_PTR)_strtoui64(hp + 6, NULL, 10);
    if (!hw || !IsWindow(hw)) { logline("hWnd %p not a window; no notify", (void*)hw); return; }
    PostMessageA(hw, 0x80FF, 0, 0x1C02CE);
    char banner[256];
    snprintf(banner, sizeof banner,
             "Simulated by Xyce (qspice-shim)\nTotal elapsed time: %.5g seconds.\n", secs);
    post_text(hw, banner);
    logline("posted completion to hWnd=%p", (void*)hw);
}

/* Drain our stdin to a file so it can be replayed for multiple children. */
static int capture_stdin(const char *path)
{
    HANDLE in = GetStdHandle(STD_INPUT_HANDLE);
    FILE *f = fopen(path, "wb");
    if (!f) return 0;
    char buf[65536];
    DWORD n;
    while (ReadFile(in, buf, sizeof buf, &n, NULL) && n > 0)
        fwrite(buf, 1, n, f);
    fclose(f);
    return 1;
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
    DWORD pid = GetCurrentProcessId();
    char netfile[MAX_PATH];
    const char *stdin_file = NULL;

    /* a piped netlist can only be drained once: capture it so both the
     * bridge and a post-failure fallback get the full deck */
    if (stype == FILE_TYPE_PIPE) {
        CreateDirectoryA(ARGS_DIR, NULL);
        snprintf(netfile, sizeof netfile, "%s\\netlist.%lu.cir", ARGS_DIR, pid);
        if (capture_stdin(netfile)) {
            stdin_file = netfile;
            logline("stdin captured -> %s", netfile);
        } else {
            logline("stdin capture failed; children inherit the pipe");
        }
    }

    DWORD rc = 127;

    if (_stricmp(mode, "xyce") == 0) {
        CreateDirectoryA(ARGS_DIR, NULL);
        char afile[MAX_PATH];
        snprintf(afile, sizeof afile, "%s\\%lu.txt", ARGS_DIR, pid);
        FILE *af = fopen(afile, "w");
        if (af) {
            fprintf(af, "%s\ncwd=%s\n", args, cwd);
            fclose(af);
            snprintf(cmd, sizeof cmd, BRIDGE_CMD_FMT, pid);
            logline("route=xyce: %s (args file %s)", cmd, afile);
            DWORD tstart = GetTickCount();
            rc = run(cmd, stdin_file);
            logline("xyce bridge rc=%lu", rc);
            DeleteFileA(afile);
            if (rc == 0) {
                notify_qux(args, (GetTickCount() - tstart) / 1000.0);
                goto done;
            }
            logline("bridge failed; falling back to real engine");
        } else {
            logline("cannot write args file %s; falling back", afile);
        }
    }

    if (GetFileAttributesA(real) == INVALID_FILE_ATTRIBUTES) {
        logline("real engine missing: %s", real);
        fprintf(stderr, "qspice-shim: %s not found\n", real);
        rc = 127;
        goto done;
    }
    snprintf(cmd, sizeof cmd, "\"%s\" %s", real, args);
    rc = run(cmd, stdin_file);
    logline("route=passthru rc=%lu", rc);

done:
    if (stdin_file) DeleteFileA(stdin_file);
    return (int)rc;
}
