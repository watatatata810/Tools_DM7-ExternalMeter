# Third-party notices

## Meter level table (`tools/dm7_levelwt_table.json`)

The DM7 `levelwt` → dB table is derived from `wtMtrTable.json` in
[bitfocus/companion-module-yamaha-rcp](https://github.com/bitfocus/companion-module-yamaha-rcp)
(MIT License). It was verified against a DM7C with its internal oscillator at
0 / -6 / -10 / -20 / -30 / -40 / -50 / -60 dBFS (see `tools/calibration_log.csv`).

## Runtime dependencies

- FastAPI (MIT), Starlette (BSD-3-Clause), Uvicorn (BSD-3-Clause), websockets (BSD-3-Clause), psutil (BSD-3-Clause)
- Binaries are built with PyInstaller (GPL-2.0 with a bootloader exception that permits distributing the built application under this project's license).

Yamaha and DM7 are trademarks of Yamaha Corporation. This project is not affiliated with or endorsed by Yamaha.
