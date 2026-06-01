import uuid
from typing import Annotated

import uuid_utils
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

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
    pass
