/* packaging/launcher.c : Subjetunes.exe
   Starts runtime\pythonw.exe -m subjetunes.desktop from the folder this exe is in.
   No console window, no registry, no installer. Build: see build_launcher.sh. */
#define WIN32_LEAN_AND_MEAN
#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif
#include <windows.h>
#include <shlwapi.h>
#include <stdio.h>

int WINAPI wWinMain(HINSTANCE h, HINSTANCE p, LPWSTR args, int show) {
    wchar_t dir[MAX_PATH], py[MAX_PATH], cmd[2 * MAX_PATH + 512];
    GetModuleFileNameW(NULL, dir, MAX_PATH);
    PathRemoveFileSpecW(dir);

    swprintf(py, MAX_PATH, L"%ls\\runtime\\pythonw.exe", dir);
    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxW(NULL, L"The runtime folder is missing next to Subjetunes.exe.\n\n"
                          L"Unzip the whole Subjetunes folder and run Subjetunes.exe from inside it.",
                    L"Subjetunes", MB_ICONERROR);
        return 1;
    }
    SetEnvironmentVariableW(L"SUBJETUNES_HOME", dir);
    SetEnvironmentVariableW(L"PYTHONUTF8", L"1");
    SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", NULL);
    swprintf(cmd, sizeof(cmd) / sizeof(cmd[0]), L"\"%ls\" -m subjetunes.desktop %ls", py, args ? args : L"");

    STARTUPINFOW si = { sizeof(si) };
    PROCESS_INFORMATION pi;
    if (!CreateProcessW(py, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        wchar_t msg[512];
        swprintf(msg, 512, L"Could not start Subjetunes (error %lu).", GetLastError());
        MessageBoxW(NULL, msg, L"Subjetunes", MB_ICONERROR);
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
