"""本地/容器启动入口：python -m app.main 或 python run.py"""

from . import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
