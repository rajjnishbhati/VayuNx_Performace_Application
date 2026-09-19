"""python -m profiler_service  ->  http://127.0.0.1:8010/"""

import uvicorn

from profiler_service import config

if __name__ == "__main__":
    print(f"VAYUNX Profiler Service on http://{config.HOST}:{config.PORT}/  (db: {config.DB_URL})", flush=True)
    uvicorn.run("profiler_service.api:app", host=config.HOST, port=config.PORT, log_level="warning")
