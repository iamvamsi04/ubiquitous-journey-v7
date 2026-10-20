"""Remove retired financial and support storage, preserving populated legacy databases first."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import logging
import sqlite3
from uuid import uuid4

from config import settings

RETIRED_TABLES = ('billing_case_events', 'billing_cases', 'billing_disputes', 'payments', 'credit_notes')


def remove_retired_tables(conn, backup_dir=None):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    retired = [name for name in RETIRED_TABLES if name in existing]
    if not retired:
        return None
    conn.execute('BEGIN IMMEDIATE')
    backup = None
    try:
        if any(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in retired):
            path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            if not path.is_file():
                raise ValueError('Cannot remove populated legacy tables without a file backup.')
            folder = Path(backup_dir) if backup_dir else settings.ROOT.parent / 'previous_versions/database_backups'
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = folder / f'{path.stem}-before-table-cleanup-{stamp}-{uuid4().hex[:8]}.db'
            # A separate read connection can snapshot the committed database while
            # this connection holds the reserved write lock, before any drops.
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Legacy backup failed its integrity check.')
            logging.getLogger(__name__).warning('Retired records preserved in %s', backup)
        for table in retired:
            conn.execute(f'DROP TABLE {table}')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Table cleanup would leave broken references.')
        conn.commit()
        return str(backup) if backup else None
    except BaseException:
        conn.rollback()
        raise


def remove_views(conn):
    """Drop stored query definitions atomically; underlying records are preserved."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            conn.execute('DROP VIEW ' + quoted)
        conn.commit()
        return names
    except BaseException:
        conn.rollback()
        raise


def add_promotion_expiry(conn):
    """Legacy codes stay stored but expire until an operator assigns a real date."""
    if 'expires_on' not in {row[1] for row in conn.execute('PRAGMA table_info(promotions)')}:
        conn.execute("ALTER TABLE promotions ADD COLUMN expires_on DATE NOT NULL DEFAULT '1970-01-01' "
                     "CHECK(length(expires_on)=10 AND expires_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]' "
                     "AND date(expires_on,'+0 days') IS NOT NULL AND date(expires_on,'+0 days')=expires_on)")


def add_unknown_fulfillment(conn):
    """Preserve service records while allowing unknown legacy shipment status.

    Rebuild only the affected CHECK constraint; the savepoint makes a failure
    atomic and also works inside an existing seed transaction.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='order_service'").fetchone()
    if not row or "'unknown'" in row[0]:
        return
    definition = row[0]
    updated = definition.replace("DEFAULT 'placed'", "DEFAULT 'unknown'").replace(
        "IN ('placed','shipped','delivered','cancelled')",
        "IN ('unknown','placed','shipped','delivered','cancelled')")
    if updated == definition or "'unknown'" not in updated:
        raise ValueError('Unrecognized order_service schema; migration stopped without changing records.')
    import re
    updated = re.sub(r'(?i)CREATE TABLE\s+(?:IF NOT EXISTS\s+)?["`\[]?order_service["`\]]?',
                     'CREATE TABLE order_service_upgrade', updated, count=1)
    conn.execute('SAVEPOINT service_upgrade')
    try:
        objects = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name='order_service' "
                               "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()
        conn.execute(updated)
        columns = ','.join('"'+r[1].replace('"','""')+'"' for r in conn.execute('PRAGMA table_info(order_service)'))
        conn.execute(f'INSERT INTO order_service_upgrade ({columns}) SELECT {columns} FROM order_service')
        conn.execute('DROP TABLE order_service')
        conn.execute('ALTER TABLE order_service_upgrade RENAME TO order_service')
        for obj in objects:
            conn.execute(obj[0])
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Service upgrade would leave broken references.')
        conn.execute('RELEASE service_upgrade')
    except BaseException:
        conn.execute('ROLLBACK TO service_upgrade')
        conn.execute('RELEASE service_upgrade')
        raise
