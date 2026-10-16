# HEC-RAS 6.6 on Linux: Wine drivers

`core/hecras_engine.py` uses these steps. They're **not** used by the LISFLOOD-FP or ANUGA engines.

- `terrain.cs`, `mesh.cs`, `drv.cs` are small C# programs that call HEC's RasMapperLib: terrain HDF, mesh, `complete`/`assoc`/`props`. Build them with any C# compiler against .NET Framework 4.8. Copy the `.exe` files **into the HEC-RAS folder** inside the Wine prefix (next to `RasMapperLib.dll` and `GDAL/`).
- One-time prefix setup: Wine 10, 64-bit prefix, `winetricks dotnet48`, then the HEC-RAS 6.6 Windows files. After that run `Ras.exe /regserver` and `RegAsm /codebase RasMapperLib.dll`, and register the VB6 OCX files from the installer archive with `regsvr32`.
- Linux solver: HEC's `Linux_RAS_v66.zip` (https://www.hec.usace.army.mil/software/hec-ras/downloads/Linux_RAS_v66.zip). Set `NDBREACH_HECRAS_LINUX_DIR` to the unzipped folder.
- Wine needs an X display (`NDBREACH_HECRAS_DISPLAY`, default `:77`). Start an Xvfb yourself.
- **HEC's Terms and Conditions (TCU):** Wine doesn't keep the acceptance, and the engine never clicks it. Accepting is the user's decision. A user who has accepted can set `NDBREACH_HECRAS_TCU_HOOK` to their own script, for example one that clicks "I agree" with xdotool. The engine starts that script next to `Ras.exe` and says so in the log.
- `muncie_check.py` reproduces the chain on HEC's Muncie example (about 3 minutes) and compares max WSE against HEC's run inputs.
