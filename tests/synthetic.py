"""Builder for synthetic MyExpenses-like SQLite databases (no real data)."""

from __future__ import annotations

import sqlite3
import uuid as uuidlib
from datetime import date as Date
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA = Path(__file__).parent / "fixtures" / "myexpenses_schema.sql"
SPLIT_LABEL = "__SPLIT_TRANSACTION__"
CURRENCIES = {"EUR": 2, "CHF": 2, "USD": 2, "JPY": 0}


class SyntheticDB:
    """Create a MyExpenses-like database from the DDL and fill it with fake rows."""

    def __init__(self, path: Path, tz: str = "Europe/Rome") -> None:
        self.path = Path(path)
        self.tz = ZoneInfo(tz)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA.read_text())
        for code, digits in CURRENCIES.items():
            self.conn.execute(
                "INSERT INTO currency (code, fraction_digits) VALUES (?, ?)", (code, digits)
            )
        self._accounts: dict[str, tuple[int, str]] = {}
        self._categories: dict[tuple[str, ...], int] = {}
        self._payees: dict[str, int] = {}
        self._tags: dict[str, int] = {}
        self._methods: dict[str, int] = {}

    # -- lookup tables -------------------------------------------------
    def add_currency(self, code: str, fraction_digits: int | None = 2) -> None:
        self.conn.execute(
            "INSERT INTO currency (code, fraction_digits) VALUES (?, ?)", (code, fraction_digits)
        )

    def digits(self, code: str) -> int:
        row = self.conn.execute(
            "SELECT fraction_digits FROM currency WHERE code = ?", (code,)
        ).fetchone()
        return 2 if row is None or row[0] is None else row[0]

    def add_account(self, label: str, currency: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO accounts (label, currency, uuid) VALUES (?, ?, ?)",
            (label, currency, str(uuidlib.uuid4())),
        )
        self._accounts[label] = (cur.lastrowid, currency)
        return cur.lastrowid

    def add_category(self, path: str) -> int:
        """Create the category tree for 'Top > Sub > ...' and return the leaf id."""
        parent: int | None = None
        parts: list[str] = []
        for label in (p.strip() for p in path.split(">")):
            parts.append(label)
            key = tuple(parts)
            if key not in self._categories:
                cur = self.conn.execute(
                    "INSERT INTO categories (label, parent_id, uuid) VALUES (?, ?, ?)",
                    (label, parent, str(uuidlib.uuid4())),
                )
                self._categories[key] = cur.lastrowid
            parent = self._categories[key]
        assert parent is not None
        return parent

    def add_payee(self, name: str) -> int:
        if name not in self._payees:
            cur = self.conn.execute("INSERT INTO payee (name) VALUES (?)", (name,))
            self._payees[name] = cur.lastrowid
        return self._payees[name]

    def add_tag(self, label: str) -> int:
        if label not in self._tags:
            cur = self.conn.execute("INSERT INTO tags (label) VALUES (?)", (label,))
            self._tags[label] = cur.lastrowid
        return self._tags[label]

    def add_method(self, label: str) -> int:
        if label not in self._methods:
            cur = self.conn.execute("INSERT INTO paymentmethods (label) VALUES (?)", (label,))
            self._methods[label] = cur.lastrowid
        return self._methods[label]

    # -- helpers -------------------------------------------------------
    def to_epoch(self, when: Date | datetime) -> int:
        """Local midnight for dates; naive datetimes are local; aware ones are kept."""
        if isinstance(when, datetime):
            if when.tzinfo is None:
                when = when.replace(tzinfo=self.tz)
        else:
            when = datetime(when.year, when.month, when.day, tzinfo=self.tz)
        return int(when.timestamp())

    def _minor(self, amount: float, currency: str) -> int:
        return round(amount * 10 ** self.digits(currency))

    def uuid_of(self, tx_id: int) -> str | None:
        row = self.conn.execute("SELECT uuid FROM transactions WHERE _id = ?", (tx_id,)).fetchone()
        return row[0]

    # -- transactions --------------------------------------------------
    def add_transaction(
        self,
        account: str,
        date: Date | datetime,
        amount: float,
        category: str | None = None,
        payee: str | None = None,
        comment: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        original: tuple[float, str] | None = None,
        status: int = 0,
        parent: int | None = None,
        transfer_to: str | None = None,
        uuid: str | None = ...,  # type: ignore[assignment]
        method: str | None = None,
        transfer_amount: float | None = None,
    ) -> int:
        """Insert a transaction and return its row id (use uuid_of for the uuid).

        uuid=None stores a NULL uuid; omitted generates a random one.
        transfer_to creates the peer row in that account (amount defaults to -amount); like in
        the app, both legs of a transfer share the uuid (they live in different accounts).
        """
        account_id, currency = self._accounts[account]
        if uuid is ...:
            uuid = str(uuidlib.uuid4())
        epoch = self.to_epoch(date)
        cat_id = self.add_category(category) if category else None
        payee_id = self.add_payee(payee) if payee else None
        method_id = self.add_method(method) if method else None
        orig_amount = orig_currency = None
        if original is not None:
            orig_currency = original[1]
            orig_amount = self._minor(original[0], orig_currency)
        cur = self.conn.execute(
            "INSERT INTO transactions (comment, date, value_date, amount, cat_id, account_id,"
            " payee_id, method_id, parent_id, status, uuid, original_amount, original_currency)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (comment, epoch, epoch, self._minor(amount, currency), cat_id, account_id,
             payee_id, method_id, parent, status, uuid, orig_amount, orig_currency),
        )
        tx_id = cur.lastrowid
        for tag in tags:
            self.conn.execute(
                "INSERT INTO transactions_tags (tag_id, transaction_id) VALUES (?, ?)",
                (self.add_tag(tag), tx_id),
            )
        if transfer_to is not None:
            peer_id, peer_cur = self._accounts[transfer_to]
            peer_amount = -amount if transfer_amount is None else transfer_amount
            peer = self.conn.execute(
                "INSERT INTO transactions (date, value_date, amount, account_id, status, uuid,"
                " transfer_peer, transfer_account) VALUES (?,?,?,?,?,?,?,?)",
                (epoch, epoch, self._minor(peer_amount, peer_cur), peer_id, status,
                 uuid, tx_id, account_id),
            ).lastrowid
            self.conn.execute(
                "UPDATE transactions SET transfer_peer = ?, transfer_account = ? WHERE _id = ?",
                (peer, peer_id, tx_id),
            )
        return tx_id

    def add_split(
        self,
        account: str,
        date: Date | datetime,
        parts: list[dict],
        payee: str | None = None,
        comment: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
        status: int = 0,
    ) -> int:
        """Split parent (category __SPLIT_TRANSACTION__) with children from `parts`.

        Each part is a dict of add_transaction kwargs (at least `amount`); account and
        date default to the parent's. Returns the parent id.
        """
        total = sum(p["amount"] for p in parts)
        parent = self.add_transaction(
            account, date, total, category=SPLIT_LABEL, payee=payee, comment=comment,
            tags=tags, status=status,
        )
        for part in parts:
            kwargs = {"account": account, "date": date, "status": status, **part}
            self.add_transaction(parent=parent, **kwargs)
        return parent

    def add_archive(
        self,
        account: str,
        date: Date | datetime,
        parts: list[dict],
        payee: str | None = None,
        comment: str | None = None,
        tags: tuple[str, ...] | list[str] = (),
    ) -> int:
        """Archive parent (status 4) with archived children (status 5)."""
        total = sum(p["amount"] for p in parts)
        parent = self.add_transaction(
            account, date, total, payee=payee, comment=comment, tags=tags, status=4
        )
        for part in parts:
            kwargs = {"account": account, "date": date, "status": 5, **part}
            self.add_transaction(parent=parent, **kwargs)
        return parent

    def commit(self) -> Path:
        self.conn.commit()
        return self.path

    def close(self) -> Path:
        self.commit()
        self.conn.close()
        return self.path
