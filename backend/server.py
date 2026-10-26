# -*- coding: utf-8 -*-
"""飞脑服务入口：FEINAO_PORT 环境变量或 spec.json 中的 port 决定端口。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine
if __name__ == "__main__":
    engine.run()
