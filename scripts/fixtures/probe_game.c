/* DeckStream launch-rig probe: a stand-in "game" written for this repo (no third-party code or assets).
 * Build: x86_64-w64-mingw32-gcc -mwindows -o probe_game.exe probe_game.c
 * It opens a window, reads payload.bin from its own folder (so every byte comes through the streamed mount),
 * writes "<bytes> <fnv1a-64 hex>" to the file named by DS_PROBE_OUT, keeps the window up for DS_PROBE_HOLD_MS
 * (default 4000) and exits 0. */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>

int WINAPI WinMain(HINSTANCE h, HINSTANCE p, LPSTR c, int n) {
    HWND w = CreateWindowA("STATIC", "DeckStreamProbe", WS_OVERLAPPEDWINDOW | WS_VISIBLE, 0, 0, 320, 200, 0, 0, h, 0);
    unsigned long long hash = 1469598103934665603ULL, total = 0;
    FILE *f = fopen("payload.bin", "rb");
    if (f) {
        unsigned char buf[65536];
        size_t got;
        while ((got = fread(buf, 1, sizeof buf, f)) > 0) {
            for (size_t i = 0; i < got; i++) { hash ^= buf[i]; hash *= 1099511628211ULL; }
            total += got;
        }
        fclose(f);
    }
    const char *out = getenv("DS_PROBE_OUT");
    if (out) {
        FILE *o = fopen(out, "wb");
        if (o) { fprintf(o, "%llu %016llx\n", total, hash); fclose(o); }
    }
    const char *hold = getenv("DS_PROBE_HOLD_MS");
    Sleep(hold ? (DWORD)atoi(hold) : 4000);
    if (w) DestroyWindow(w);
    return f ? 0 : 3;
}
