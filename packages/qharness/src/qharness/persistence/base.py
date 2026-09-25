# -*- coding: utf-8 -*-
"""QHarness 全部 SQLAlchemy ORM 模型共享的声明基类。"""

from sqlalchemy.orm import DeclarativeBase


class OrmBase(DeclarativeBase):
    """集中保存跨功能模块的 SQLAlchemy Metadata。"""
