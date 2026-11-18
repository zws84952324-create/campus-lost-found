"""本地启动入口：python 启动项目.py，演示版加 --demo。"""
import argparse
import os
import threading
import webbrowser
from pathlib import Path

from app import create_app, db


def main():
    parser = argparse.ArgumentParser(description='启动校园失物招领，仅供本地开发。')
    parser.add_argument('--demo', action='store_true', help='使用独立的演示数据库')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    config = {'SESSION_COOKIE_SECURE':False, 'TRUSTED_HOSTS':['localhost','127.0.0.1']}
    if args.demo:
        folder = Path(__file__).resolve().parent / 'instance'
        config.update(DEMO_MODE=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + (folder / '本地演示.db').as_posix(), UPLOAD_FOLDER=str(folder / '演示图片'))
    app = create_app(config)
    with app.app_context():
        db.create_all()
        if args.demo:
            result = app.test_cli_runner().invoke(args=['seed-demo'])
            if result.exit_code:
                raise RuntimeError(result.output)
    address = f'http://127.0.0.1:{args.port}'
    print(f'本地地址：{address}\n按 Ctrl+C 停止。演示数据与正式数据库独立。', flush=True)
    if not args.no_browser:
        threading.Timer(1, lambda: webbrowser.open(address)).start()
    app.run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == '__main__':
    main()
