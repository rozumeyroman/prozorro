# Run from an openprocurement.api checkout: PYTHONPATH=src .venv/bin/python gen_cdb.py OUT.json VERSION
import json, sys, os
from aiohttp_pydantic.oas.view import generate_oas
from prozorro_cdb.api.main import get_aiohttp_sub_app
ini = os.path.abspath("etc/service.ini")
from configparser import RawConfigParser
p = RawConfigParser(); p.read(ini)
sec = [s for s in p.sections() if s.startswith("app:")][0]
settings = dict(p.items(sec, raw=True))
settings = {k: v.replace("%(here)s", os.path.dirname(ini)) for k, v in settings.items()}
app = get_aiohttp_sub_app({"__file__": ini, "here": os.path.dirname(ini)}, **settings)
spec = generate_oas([app], version_spec=sys.argv[2], title_spec="Prozorro CDB API (async part, prefix /api/2.5)",
                    security={"APIKeyHeader": {"type": "apiKey", "in": "header", "name": "Authorization"}})
json.dump(spec, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
print(len(spec["paths"]), "paths")
