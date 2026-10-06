# Run from a prozorro-catalog checkout: PYTHONPATH=src .venv/bin/python gen_catalog.py OUT.json VERSION
import json, sys
from aiohttp_pydantic.oas.view import generate_oas
from catalog.api import create_application

app = create_application()
spec = generate_oas([app], version_spec=sys.argv[2], title_spec="Prozorro Catalog API",
                    security={"Basic": {"type": "http", "scheme": "basic"}})
json.dump(spec, open(sys.argv[1], "w"), ensure_ascii=False, indent=2)
print(len(spec["paths"]), "paths")
