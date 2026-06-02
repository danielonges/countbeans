import uuid
from typing import Annotated

import uuid_utils.compat as uuid_utils  # .compat yields stdlib uuid.UUID instances
from sqlalchemy import MetaData
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

# Deterministic names for every constraint/index so Alembic autogenerate emits
# stable identifiers it can later reference in ALTER/DROP migrations. Without
# this, PK/FK/unique/index constraints get backend-assigned names that vary and
# can't be reliably targeted. See SQLAlchemy "Configuring Constraint Naming
# Conventions".
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Reusable annotated type for single-column UUID primary keys.
# default=uuid_utils.uuid7 generates a time-ordered UUID7 in the app layer on
# every INSERT, so the ID is available in Python before the row hits the DB.
UUIDpk = Annotated[
    uuid.UUID,
    # lambda wrapper: uuid_utils.uuid7 has optional kwargs so SQLAlchemy would
    # treat it as a context-sensitive default and pass the execution context as
    # the first positional arg. A zero-arg lambda avoids that.
    mapped_column(UUID(as_uuid=True), primary_key=True, default=lambda: uuid_utils.uuid7()),
]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
