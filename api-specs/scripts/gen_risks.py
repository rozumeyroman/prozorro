# Usage: python gen_risks.py <prozorro-risks>/swagger OUT.yaml VERSION  (needs pyyaml)
import sys, yaml, os
src, out, ver = sys.argv[1:4]
routes = [  # from src/prozorro/risks/api.py setup_swagger()
    ("/api/ping", "ping.yaml"),
    ("/api/version", "version.yaml"),
    ("/api/risks/{tender_id}", "risks.yaml"),
    ("/api/risks", "risks_list.yaml"),
    ("/api/filter-values", "filter_values.yaml"),
    ("/api/risks-report", "download_risks_report.yaml"),
    ("/api/risks-feed", "risks_feed.yaml"),
]
paths = {}
for path, f in routes:
    op = yaml.safe_load(open(os.path.join(src, f)))
    if "{tender_id}" in path and not any(p.get("in") == "path" for p in op.get("parameters", [])):
        op.setdefault("parameters", []).insert(0, {"in": "path", "name": "tender_id", "required": True, "schema": {"type": "string"}})
    paths[path] = {"get": op}
spec = {"openapi": "3.0.0", "info": {"title": "Prozorro Risks API", "version": ver}, "paths": paths}
yaml.safe_dump(spec, open(out, "w"), allow_unicode=True, sort_keys=False)
print(len(paths), "paths")
