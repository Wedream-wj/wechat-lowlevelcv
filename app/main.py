"""服务入口：python -m app.main 或 uvicorn app.api:app --reload"""
from __future__ import annotations

import os

import uvicorn

from .config import settings


def main() -> None:
    uvicorn.run(
        "app.api:app",
        host=os.getenv("LLCV_HOST", "127.0.0.1"),
        port=int(os.getenv("LLCV_PORT", "8000")),
        reload=os.getenv("LLCV_RELOAD", "0") == "1",
        log_level="info",
    )


if __name__ == "__main__":
    print(f"[llcv] out={settings.out_dir} db={settings.db_path} llm={'live' if settings.llm_ready else 'mock'}")
    main()
