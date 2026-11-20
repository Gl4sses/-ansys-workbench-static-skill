# encoding: utf-8
"""Read the imported geometry only. Never assign material, mesh, loads or solve."""
import codecs
import json
import os
import traceback

config_path = globals().get("_MCP_CONFIG_PATH") or os.environ.get("ANSYS_FEA_RUNTIME_CONFIG")
with codecs.open(config_path, "r", "utf-8") as stream:
    inspection_config = json.loads(stream.read())
inspection_paths = inspection_config["runtime_paths"]


def inspection_write(path, text):
    with codecs.open(path, "w", "utf-8") as stream:
        stream.write(text)


try:
    inspection_write(inspection_paths["progress"], u"Reading imported CAD geometry; awaiting user simulation inputs")
    geometry = capture_geometry(ExtAPI.DataModel.GeoData)
    if not geometry["bodies"]:
        raise RuntimeError("No bodies found in imported CAD")
    inspection_report = {"run_id": inspection_config["run_id"], "attempt": inspection_config["attempt"],
                         "geometry": geometry}
    inspection_write(inspection_paths["geometry_review"], json.dumps(inspection_report, ensure_ascii=True, indent=2))
    inspection_write(inspection_paths["result"], u"status=success\nrun_id=%s\nattempt=%s\nworkflow=inspect" %
                     (inspection_config["run_id"], inspection_config["attempt"]))
except Exception:
    error = traceback.format_exc()
    inspection_write(inspection_paths["result"], u"status=error\n" + error)
    inspection_write(inspection_paths["diagnostic"], json.dumps({
        "run_id": inspection_config["run_id"], "attempt": inspection_config["attempt"],
        "stage": "geometry", "error": error}, ensure_ascii=True, indent=2))
    raise
