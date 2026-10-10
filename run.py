"""启动入口:python run.py"""
import logging
import socket

import uvicorn

from app.config import settings


def lan_ips() -> list[str]:
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("223.5.5.5", 80))  # 不会真的发包,只用来取本机出口 IP
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("\n" + "=" * 60)
    print(f"  {settings.app_title} 正在启动")
    print(f"  本机访问:   http://127.0.0.1:{settings.port}")
    for ip in lan_ips():
        print(f"  局域网访问: http://{ip}:{settings.port}   (同一 WiFi 下的手机/电脑)")
    print(f"  管理后台:   /admin")
    print("=" * 60 + "\n")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")
