"""本地启动入口：python 启动项目.py，直接使用真实发布与认领流程。"""
import argparse
import threading
import webbrowser

from app import create_app, db


def main():
    parser = argparse.ArgumentParser(description='启动校园失物招领，仅供本地开发。')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    config = {'SESSION_COOKIE_SECURE':False, 'TRUSTED_HOSTS':['localhost','127.0.0.1']}
    app = create_app(config)
    with app.app_context():
        db.create_all()
    address = f'http://127.0.0.1:{args.port}'
    print(f'本地地址：{address}\n按 Ctrl+C 停止。请先创建管理员，审核同学提交的信息。', flush=True)
    if not args.no_browser:
        threading.Timer(1, lambda: webbrowser.open(address)).start()
    app.run(host='127.0.0.1', port=args.port, debug=False)


if __name__ == '__main__':
    main()
