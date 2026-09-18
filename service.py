"""造血干细胞捐献协同的运行入口。

- python3 service.py --check           检查基础配置与领域装配
- python3 service.py --port 8000       启动服务，/health 守健康检查，/api/* 为协同接口
"""

import argparse
import json
from http.server import ThreadingHTTPServer

from coord.api import make_handler
from coord.app import App, seed_demo

SERVICE_ID = "stem-cell-donation"
SERVICE_NAME = "造血干细胞捐献协同"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_app(seed=True):
    app = App()
    if seed:
        seed_demo(app)
    return app


# 默认装配一份演示目录，供本地联调；契约测试导入 Handler 时也得到一致行为。
app = build_app(seed=True)
Handler = make_handler(app)


def run_checks():
    assert health_payload()["service"] == SERVICE_ID
    # 装配自检：时区、审计链、工作流均可实例化
    from coord.clock import get_zone
    get_zone("Asia/Urumqi")
    assert app.audit.verify()["ok"] is True
    assert app.directory.orgs, "演示目录未装配"
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-seed", action="store_true", help="不装配演示目录")
    args = parser.parse_args()
    if args.check:
        run_checks()
        return
    global app, Handler
    if args.no_seed:
        app = build_app(seed=False)
        Handler = make_handler(app)
    print(f"{SERVICE_NAME} 监听 0.0.0.0:{args.port}（GET /health）")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
