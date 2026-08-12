"""幂等执行 Agent 相关 SQL 迁移；装配层在创建 Postgres store 前调用。"""

from pathlib import Path

from psycopg import Connection

# 迁移文件固定相对项目根，避免 cwd 漂移。
# migrations.py 位于 app/agent/db/，上 3 级才是项目根 med-agent/。
MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"
# 当前 Agent 热路径只需要这两份 schema；可重复执行。
MIGRATION_FILES = (
    "0001_agent_runs.sql",
    "0002_agent_approvals.sql",
)


def apply_agent_migrations(connection: Connection) -> list[str]:
    """按固定顺序执行迁移 SQL；CREATE IF NOT EXISTS / ADD IF NOT EXISTS 保证幂等。"""

    applied: list[str] = []
    try:
        with connection.cursor() as cursor:
            for name in MIGRATION_FILES:
                sql_text = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
                cursor.execute(sql_text)
                applied.append(name)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return applied


def ping_database(connection: Connection) -> None:
    """轻量 readiness 探测；失败直接抛出，不回显连接串。"""

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if row is None or row[0] != 1:
            raise RuntimeError("数据库 readiness 探测失败")
    finally:
        if not connection.autocommit:
            connection.rollback()
