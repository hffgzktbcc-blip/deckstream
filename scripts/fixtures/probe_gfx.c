/* DeckStream launch-rig graphics probe, written for this repo (no third-party code or assets).
 * Build: x86_64-w64-mingw32-gcc -mwindows -o probe_gfx.exe probe_gfx.c -ld3d11 -ldxgi -luuid
 * Creates a Direct3D 11 device, clears a 16x16 render target to (1.0, 0.5, 0.0, 1.0), reads a pixel back and writes
 * "fl=<feature level> pixel=<rgba hex>" to the file named by DS_PROBE_OUT. Under Wine this runs on Wine's own
 * renderer over software GL: it shows a Direct3D call chain works, not DXVK/VKD3D, a GPU, or performance. */
#define COBJMACROS
#include <windows.h>
#include <d3d11.h>
#include <stdio.h>
int WINAPI WinMain(HINSTANCE h, HINSTANCE p, LPSTR c, int n) {
    ID3D11Device *dev = 0; ID3D11DeviceContext *ctx = 0; D3D_FEATURE_LEVEL fl;
    HRESULT hr = D3D11CreateDevice(0, D3D_DRIVER_TYPE_HARDWARE, 0, 0, 0, 0, D3D11_SDK_VERSION, &dev, &fl, &ctx);
    FILE *o = fopen(getenv("DS_PROBE_OUT") ? getenv("DS_PROBE_OUT") : "gfx_out.txt", "wb");
    if (FAILED(hr)) { fprintf(o, "device_failed %08lx\n", hr); fclose(o); return 2; }
    D3D11_TEXTURE2D_DESC d = {16,16,1,1,DXGI_FORMAT_R8G8B8A8_UNORM,{1,0},D3D11_USAGE_DEFAULT,D3D11_BIND_RENDER_TARGET,0,0};
    ID3D11Texture2D *rt = 0, *st = 0; ID3D11RenderTargetView *rtv = 0;
    ID3D11Device_CreateTexture2D(dev, &d, 0, &rt);
    ID3D11Device_CreateRenderTargetView(dev, (ID3D11Resource*)rt, 0, &rtv);
    float col[4] = {1.0f, 0.5f, 0.0f, 1.0f};
    ID3D11DeviceContext_ClearRenderTargetView(ctx, rtv, col);
    d.Usage = D3D11_USAGE_STAGING; d.BindFlags = 0; d.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    ID3D11Device_CreateTexture2D(dev, &d, 0, &st);
    ID3D11DeviceContext_CopyResource(ctx, (ID3D11Resource*)st, (ID3D11Resource*)rt);
    D3D11_MAPPED_SUBRESOURCE m; hr = ID3D11DeviceContext_Map(ctx, (ID3D11Resource*)st, 0, D3D11_MAP_READ, 0, &m);
    if (FAILED(hr)) { fprintf(o, "map_failed %08lx\n", hr); fclose(o); return 3; }
    unsigned char *px = m.pData;
    fprintf(o, "fl=%x pixel=%02x%02x%02x%02x\n", fl, px[0], px[1], px[2], px[3]);
    fclose(o); return 0;
}
