"""Thin DB layer over SQLAlchemy (psycopg 3). SQL is written with ? placeholders."""
import os
from decimal import Decimal
from flask import g
from sqlalchemy import create_engine, event
from psycopg.adapt import Loader

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg://avand:avand@127.0.0.1:5432/avand")
engine = create_engine(DATABASE_URL, pool_size=8, max_overflow=8, pool_pre_ping=True, future=True)


class _NumLoader(Loader):
    """numeric -> int when integral, else float (SUM(bigint) returns numeric in PG)."""
    def load(self, data):
        d = Decimal(bytes(data).decode())
        return int(d) if d == d.to_integral_value() else float(d)


@event.listens_for(engine, "connect")
def _on_connect(dbapi_conn, _rec):
    dbapi_conn.adapters.register_loader("numeric", _NumLoader)


def _sql(sql, args=None):
    return sql.replace("%", "%%").replace("?", "%s")


def conn():
    if "dbc" not in g:
        g.dbc = engine.connect()
    return g.dbc


def close(exc=None):
    c = g.pop("dbc", None)
    if c is not None:
        try:
            if c.in_transaction():
                c.rollback()
        finally:
            c.close()


def q(sql, args=(), one=False):
    res = conn().exec_driver_sql(_sql(sql, args), tuple(args or ()))
    rows = res.mappings().all()
    return (rows[0] if rows else None) if one else rows


def v1(sql, args=()):
    r = conn().exec_driver_sql(_sql(sql, args), tuple(args or ())).first()
    return (r[0] or 0) if r else 0


def x(sql, args=()):
    s = sql.strip()
    ret = s[:6].upper() == "INSERT" and "RETURNING" not in s.upper() and "ON CONFLICT" not in s.upper()
    if ret:
        s += " RETURNING id"
    res = conn().exec_driver_sql(_sql(s, args), tuple(args or ()))
    return res.scalar() if ret else None


def commit():
    c = g.get("dbc")
    if c is not None and c.in_transaction():
        c.commit()


def rollback():
    c = g.get("dbc")
    if c is not None and c.in_transaction():
        c.rollback()


def lock():
    """serialize writes (stock / numbering consistency)"""
    conn().exec_driver_sql("SELECT pg_advisory_xact_lock(424242)")
