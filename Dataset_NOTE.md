# 1. Note @ 0625: gPO correction and cold backups

The g_PO correction performed on June 25 was applied only to the extracted HDF5 (`.h5`) files.

All ZIP archives in cold storage remain unchanged and still contain the original, uncorrected gPO data. Therefore, any dataset restored or re-extracted from these archives will require the gPO remapping tool to be run again before use.
