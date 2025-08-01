# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
from typing import Any

from flask import current_app as app

from superset import db, security_manager
from superset.commands.database.utils import add_permissions
from superset.commands.exceptions import ImportFailedError
from superset.databases.ssh_tunnel.models import SSHTunnel
from superset.databases.utils import make_url_safe
from superset.db_engine_specs.exceptions import SupersetDBAPIConnectionError
from superset.exceptions import SupersetSecurityException
from superset.models.core import Database
from superset.security.analytics_db_safety import check_sqlalchemy_uri
from superset.utils import json

logger = logging.getLogger(__name__)


def import_database(
    config: dict[str, Any],
    overwrite: bool = False,
    ignore_permissions: bool = False,
) -> Database:
    can_write = ignore_permissions or security_manager.can_access(
        "can_write",
        "Database",
    )
    existing = db.session.query(Database).filter_by(uuid=config["uuid"]).first()
    if existing:
        if not overwrite or not can_write:
            return existing
        config["id"] = existing.id
    elif not can_write:
        raise ImportFailedError(
            "Database doesn't exist and user doesn't have permission to create databases"  # noqa: E501
        )
    # Check if this URI is allowed
    if app.config["PREVENT_UNSAFE_DB_CONNECTIONS"]:
        try:
            check_sqlalchemy_uri(make_url_safe(config["sqlalchemy_uri"]))
        except SupersetSecurityException as exc:
            raise ImportFailedError(exc.message) from exc
    # https://github.com/apache/superset/pull/16756 renamed ``csv`` to ``file``.
    config["allow_file_upload"] = config.pop("allow_csv_upload")
    if "schemas_allowed_for_csv_upload" in config["extra"]:
        config["extra"]["schemas_allowed_for_file_upload"] = config["extra"].pop(
            "schemas_allowed_for_csv_upload"
        )

    # TODO (betodealmeida): move this logic to import_from_dict
    config["extra"] = json.dumps(config["extra"])

    # Before it gets removed in import_from_dict
    ssh_tunnel_config = config.pop("ssh_tunnel", None)

    # set SQLAlchemy URI via `set_sqlalchemy_uri` so that the password gets masked
    sqlalchemy_uri = config.pop("sqlalchemy_uri")
    database: Database = Database.import_from_dict(config, recursive=False)
    database.set_sqlalchemy_uri(sqlalchemy_uri)

    if database.id is None:
        db.session.flush()

    if ssh_tunnel_config:
        ssh_tunnel_config["database_id"] = database.id
        ssh_tunnel = SSHTunnel.import_from_dict(ssh_tunnel_config, recursive=False)
    else:
        ssh_tunnel = None

    # TODO (betodealmeida): we should use the `CreateDatabaseCommand` for imports

    try:
        add_permissions(database, ssh_tunnel)
    except SupersetDBAPIConnectionError as ex:
        logger.warning(ex.message)

    return database
