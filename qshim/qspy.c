/*
 * qspy — discover the QSPICE engine->GUI window-message protocol.
 *
 * Creates a window, spawns the REAL engine with -hWnd=<ours> (mimicking
 * QUX's invocation, netlist on stdin from a file), and logs every message
 * the engine posts: id, wParam, lParam, timing. Run from a console:
 *   qspy.exe <real-engine.exe> <netlist.cir> <out.qraw>
 */
#include <windows.h>
#include <stdio.h>

static FILE *lg;
static DWORD t0;

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    /* log everything except high-frequency system noise */
    if (m != WM_TIMER && m != WM_PAINT && m != WM_NCHITTEST) {
        fprintf(lg, "[+%6lums] msg=0x%04X (%5u) wParam=0x%llX (%llu) lParam=0x%llX (%lld)\n",
                GetTickCount() - t0, m, m,
                (unsigned long long)w, (unsigned long long)w,
                (unsigned long long)l, (long long)l);
        fflush(lg);
    }
    return DefWindowProcA(h, m, w, l);
}

int main(int argc, char **argv)
{
    if (argc < 4) { fprintf(stderr, "usage: qspy engine.exe deck.cir out.qraw\n"); return 2; }
    lg = fopen("qspy.log", "w");
    if (!lg) return 2;
    t0 = GetTickCount();

    WNDCLASSA wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "qspy";
    RegisterClassA(&wc);
    /* a real (hidden) top-level window: message-only windows can't receive
       broadcast or some posted messages */
    HWND hw = CreateWindowA("qspy", "qspy", 0, 0, 0, 10, 10, NULL, NULL,
                            wc.hInstance, NULL);
    fprintf(lg, "spy hwnd=%llu (0x%llX)\n",
            (unsigned long long)(UINT_PTR)hw, (unsigned long long)(UINT_PTR)hw);
    fflush(lg);

    SECURITY_ATTRIBUTES sa = { sizeof sa, NULL, TRUE };
    HANDLE hin = CreateFileA(argv[2], GENERIC_READ, FILE_SHARE_READ, &sa,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
    if (hin == INVALID_HANDLE_VALUE) { fprintf(lg, "no deck\n"); return 2; }

    char cmd[4096];
    snprintf(cmd, sizeof cmd,
             "\"%s\" -pipe -QUX -Op \"%s\" -r \"%s\" -hWnd=%llu",
             argv[1], argv[2], argv[3], (unsigned long long)(UINT_PTR)hw);
    fprintf(lg, "spawn: %s\n", cmd); fflush(lg);

    STARTUPINFOA si = {0}; si.cb = sizeof si;
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput  = hin;
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError  = GetStdHandle(STD_ERROR_HANDLE);
    PROCESS_INFORMATION pi;
    if (!CreateProcessA(NULL, cmd, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fprintf(lg, "spawn failed %lu\n", GetLastError()); return 2;
    }
    CloseHandle(hin);

    /* pump until the engine exits, then 2s grace for trailing messages */
    DWORD deadline = 0;
    for (;;) {
        MSG msg;
        while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        if (!deadline && WaitForSingleObject(pi.hProcess, 30) == WAIT_OBJECT_0) {
            DWORD rc; GetExitCodeProcess(pi.hProcess, &rc);
            fprintf(lg, "[+%6lums] engine exited rc=%lu\n", GetTickCount() - t0, rc);
            fflush(lg);
            deadline = GetTickCount() + 2000;
        }
        if (deadline) {
            if (GetTickCount() > deadline) break;
            Sleep(10);
        }
    }
    fprintf(lg, "done\n");
    fclose(lg);
    return 0;
}
