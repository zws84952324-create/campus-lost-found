"""PythonAnywhere 的 WSGI 文件可导入本模块的 application。"""
from app import create_app

application = create_app()
