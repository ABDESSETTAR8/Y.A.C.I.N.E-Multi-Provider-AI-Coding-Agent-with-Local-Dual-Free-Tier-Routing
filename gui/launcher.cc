// Y.A.C.I.N.E native GUI launcher.
// Spawns the bundled Python server (yacine_server.exe), waits for it to be
// ready, then opens a native WebView2 window rendering the local UI.
// No browser, no URL bar — a real desktop window.
#include "core/include/webview.h"
#include <windows.h>
#include <process.h>
#include <stdio.h>
#include <string.h>

static const int PORT = 8765;
static char url[256];
static char exe_dir[MAX_PATH];

// Wait until the server answers /health (up to ~30s).
static bool wait_for_server() {
    for (int i = 0; i < 60; i++) {
        char cmd[512];
        snprintf(cmd, sizeof(cmd),
                 "curl -s -o nul -w \"%%{http_code}\" http://127.0.0.1:%d/health", PORT);
        FILE* f = _popen(cmd, "r");
        if (f) {
            char buf[16] = {0};
            fread(buf, 1, sizeof(buf) - 1, f);
            _pclose(f);
            if (strstr(buf, "200")) return true;
        }
        Sleep(500);
    }
    return false;
}

int main() {
    // Locate our own directory so we can find yacine_server.exe + config.
    GetModuleFileNameA(NULL, exe_dir, MAX_PATH);
    char* slash = strrchr(exe_dir, '\\');
    if (slash) *slash = '\0';

    snprintf(url, sizeof(url), "http://127.0.0.1:%d/ui", PORT);

    // Build path to the bundled server exe.
    char server_exe[MAX_PATH];
    snprintf(server_exe, sizeof(server_exe), "%s\\yacine_server.exe", exe_dir);

    // Start the server as a detached child.
    char cmdline[1024];
    snprintf(cmdline, sizeof(cmdline),
             "\"%s\" serve --port %d --host 127.0.0.1", server_exe, PORT);
    STARTUPINFOA si = {0};
    PROCESS_INFORMATION pi = {0};
    si.cb = sizeof(si);
    // Run from exe_dir so config/ is found.
    if (!CreateProcessA(NULL, cmdline, NULL, NULL, FALSE,
                         CREATE_NEW_PROCESS_GROUP, NULL, exe_dir, &si, &pi)) {
        MessageBoxA(NULL, "Failed to start Y.A.C.I.N.E server (yacine_server.exe).",
                    "Y.A.C.I.N.E", MB_ICONERROR);
        return 1;
    }

    if (!wait_for_server()) {
        MessageBoxA(NULL, "Server did not start in time.", "Y.A.C.I.N.E", MB_ICONERROR);
        TerminateProcess(pi.hProcess, 1);
        return 1;
    }

    // Native WebView2 window.
    webview::webview w(true, nullptr);
    w.set_title("Y.A.C.I.N.E — Autonomous Engineer");
    w.set_size(1280, 800, WEBVIEW_HINT_NONE);
    w.navigate(url);
    w.run();

    // Cleanup: kill the server when the window closes.
    TerminateProcess(pi.hProcess, 0);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return 0;
}
