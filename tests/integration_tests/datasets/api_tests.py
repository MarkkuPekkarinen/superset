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
from __future__ import annotations

import copy
import unittest
from datetime import timedelta
from io import BytesIO
from unittest.mock import ANY, patch
from zipfile import is_zipfile, ZipFile

import prison
import pytest
import yaml
from freezegun import freeze_time
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import joinedload
from sqlalchemy.sql import func

from superset.commands.dataset.exceptions import DatasetCreateFailedError
from superset.connectors.sqla.models import SqlaTable, SqlMetric, TableColumn
from superset.extensions import db, security_manager
from superset.models.core import Database
from superset.models.slice import Slice
from superset.utils import json
from superset.utils.core import backend, get_example_default_schema
from superset.utils.database import get_example_database, get_main_database
from superset.utils.dict_import_export import export_to_dict
from tests.integration_tests.base_tests import SupersetTestCase
from tests.integration_tests.conftest import (  # noqa: F401
    CTAS_SCHEMA_NAME,
    with_feature_flags,
)
from tests.integration_tests.constants import (
    ADMIN_USERNAME,
    ALPHA_USERNAME,
    GAMMA_USERNAME,
)
from tests.integration_tests.fixtures.birth_names_dashboard import (
    load_birth_names_dashboard_with_slices,  # noqa: F401
    load_birth_names_data,  # noqa: F401
)
from tests.integration_tests.fixtures.energy_dashboard import (
    load_energy_table_data,  # noqa: F401
    load_energy_table_with_slice,  # noqa: F401
)
from tests.integration_tests.fixtures.importexport import (
    database_config,
    dataset_config,
    dataset_ui_export,
)


class TestDatasetApi(SupersetTestCase):
    fixture_tables_names = ("ab_permission", "ab_permission_view", "ab_view_menu")
    fixture_virtual_table_names = ("sql_virtual_dataset_1", "sql_virtual_dataset_2")
    items_to_delete: list[SqlaTable | Database | TableColumn] = []

    def setUp(self):
        self.items_to_delete = []

    def tearDown(self):
        for item in self.items_to_delete:
            db.session.delete(item)
            db.session.commit()
        super().tearDown()

    @staticmethod
    def insert_dataset(
        table_name: str,
        owners: list[int],
        database: Database,
        sql: str | None = None,
        schema: str | None = None,
        catalog: str | None = None,
        fetch_metadata: bool = True,
    ) -> SqlaTable:
        obj_owners = list()  # noqa: C408
        for owner in owners:
            user = db.session.query(security_manager.user_model).get(owner)
            obj_owners.append(user)
        table = SqlaTable(
            table_name=table_name,
            schema=schema,
            owners=obj_owners,
            database=database,
            sql=sql,
            catalog=catalog,
        )
        db.session.add(table)
        db.session.commit()
        if fetch_metadata:
            table.fetch_metadata()
        return table

    def insert_default_dataset(self):
        return self.insert_dataset(
            "ab_permission", [self.get_user("admin").id], get_main_database()
        )

    def insert_database(self, name: str, allow_multi_catalog: bool = False) -> Database:
        db_connection = Database(
            database_name=name,
            sqlalchemy_uri=get_example_database().sqlalchemy_uri,
            extra=('{"allow_multi_catalog": true}' if allow_multi_catalog else "{}"),
        )
        db.session.add(db_connection)
        db.session.commit()
        return db_connection

    def get_fixture_datasets(self) -> list[SqlaTable]:
        return (
            db.session.query(SqlaTable)
            .options(joinedload(SqlaTable.database))
            .filter(SqlaTable.table_name.in_(self.fixture_tables_names))
            .all()
        )

    def get_fixture_virtual_datasets(self) -> list[SqlaTable]:
        return (
            db.session.query(SqlaTable)
            .filter(SqlaTable.table_name.in_(self.fixture_virtual_table_names))
            .all()
        )

    @pytest.fixture
    def create_virtual_datasets(self):
        with self.create_app().app_context():
            datasets = []
            admin = self.get_user("admin")
            main_db = get_main_database()
            for table_name in self.fixture_virtual_table_names:
                datasets.append(
                    self.insert_dataset(
                        table_name,
                        [admin.id],
                        main_db,
                        "SELECT * from ab_view_menu;",
                    )
                )
            yield datasets

            # rollback changes
            for dataset in datasets:
                db.session.delete(dataset)
            db.session.commit()

    @pytest.fixture
    def create_datasets(self):
        with self.create_app().app_context():
            datasets = []
            admin = self.get_user("admin")
            main_db = get_main_database()
            for tables_name in self.fixture_tables_names:
                datasets.append(self.insert_dataset(tables_name, [admin.id], main_db))

            yield datasets

            # rollback changes
            for dataset in datasets:
                state = inspect(dataset)
                if not state.was_deleted:
                    db.session.delete(dataset)
            db.session.commit()

    @staticmethod
    def get_energy_usage_dataset():
        example_db = get_example_database()
        return (
            db.session.query(SqlaTable)
            .filter_by(
                database=example_db,
                table_name="energy_usage",
                schema=get_example_default_schema(),
            )
            .one()
        )

    @pytest.mark.usefixtures("load_energy_table_with_slice")
    def test_user_gets_all_datasets(self):
        # test filtering on datasource_name
        gamma_user = security_manager.find_user(username="gamma")

        def count_datasets():
            uri = "api/v1/chart/"
            rv = self.client.get(uri, "get_list")
            assert rv.status_code == 200
            data = rv.get_json()
            return data["count"]

        with self.temporary_user(gamma_user, login=True) as user:
            assert count_datasets() == 0

        all_db_pvm = ("all_database_access", "all_database_access")
        with self.temporary_user(
            gamma_user, extra_pvms=[all_db_pvm], login=True
        ) as user:
            self.login(username=user.username)
            assert count_datasets() > 0

        all_db_pvm = ("all_datasource_access", "all_datasource_access")
        with self.temporary_user(
            gamma_user, extra_pvms=[all_db_pvm], login=True
        ) as user:
            self.login(username=user.username)
            assert count_datasets() > 0

        # Back to normal
        with self.temporary_user(gamma_user, login=True):
            assert count_datasets() == 0

    def test_get_dataset_list(self):
        """
        Dataset API: Test get dataset list
        """

        example_db = get_example_database()
        self.login(ADMIN_USERNAME)
        arguments = {
            "filters": [
                {"col": "database", "opr": "rel_o_m", "value": f"{example_db.id}"},
                {"col": "table_name", "opr": "eq", "value": "birth_names"},
            ]
        }
        uri = f"api/v1/dataset/?q={prison.dumps(arguments)}"
        rv = self.get_assert_metric(uri, "get_list")
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        assert response["count"] == 1
        expected_columns = [
            "catalog",
            "changed_by",
            "changed_by_name",
            "changed_on_delta_humanized",
            "changed_on_utc",
            "database",
            "datasource_type",
            "default_endpoint",
            "description",
            "explore_url",
            "extra",
            "id",
            "kind",
            "owners",
            "schema",
            "sql",
            "table_name",
            "uuid",
        ]
        assert sorted(response["result"][0]) == expected_columns

    def test_get_dataset_list_gamma(self):
        """
        Dataset API: Test get dataset list gamma
        """

        if backend() == "postgresql":
            # failing
            return

        self.login(GAMMA_USERNAME)
        uri = "api/v1/dataset/"
        rv = self.get_assert_metric(uri, "get_list")
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        assert response["result"] == []

    def test_get_dataset_list_gamma_has_database_access(self):
        """
        Dataset API: Test get dataset list with database access
        """

        if backend() == "postgresql":
            # failing
            return

        self.login(GAMMA_USERNAME)

        # create new dataset
        main_db = get_main_database()
        dataset = self.insert_dataset("ab_user", [], main_db)

        # make sure dataset is not visible due to missing perms
        uri = "api/v1/dataset/"
        rv = self.get_assert_metric(uri, "get_list")
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))

        assert response["count"] == 0

        # give database access to main db
        main_db_pvm = security_manager.find_permission_view_menu(
            "database_access", main_db.perm
        )
        gamma_role = security_manager.find_role("Gamma")
        gamma_role.permissions.append(main_db_pvm)
        db.session.commit()

        # make sure dataset is now visible
        uri = "api/v1/dataset/"
        rv = self.get_assert_metric(uri, "get_list")
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))

        tables = {tbl["table_name"] for tbl in response["result"]}
        assert tables == {"ab_user"}

        # revert gamma permission
        gamma_role.permissions.remove(main_db_pvm)
        self.items_to_delete = [dataset]

    def test_get_dataset_related_database_gamma(self):
        """
        Dataset API: Test get dataset related databases gamma
        """

        # Add main database access to gamma role
        main_db = get_main_database()
        main_db_pvm = security_manager.find_permission_view_menu(
            "database_access", main_db.perm
        )
        gamma_role = security_manager.find_role("Gamma")
        gamma_role.permissions.append(main_db_pvm)
        db.session.commit()

        self.login(GAMMA_USERNAME)
        uri = "api/v1/dataset/related/database"
        rv = self.client.get(uri)
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))

        assert response["count"] == 1
        main_db = get_main_database()
        assert filter(lambda x: x.text == main_db, response["result"]) != []

        # revert gamma permission
        gamma_role.permissions.remove(main_db_pvm)
        db.session.commit()

    @pytest.mark.usefixtures("load_energy_table_with_slice")
    def test_get_dataset_item(self):
        """
        Dataset API: Test get dataset item
        """

        table = self.get_energy_usage_dataset()
        main_db = get_main_database()
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{table.id}"
        rv = self.get_assert_metric(uri, "get")
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        expected_result = {
            "cache_timeout": None,
            "database": {
                "allow_multi_catalog": False,
                "backend": main_db.backend,
                "database_name": "examples",
                "id": 1,
            },
            "default_endpoint": None,
            "description": "Energy consumption",
            "extra": None,
            "fetch_values_predicate": None,
            "filter_select_enabled": True,
            "is_sqllab_view": False,
            "kind": "physical",
            "main_dttm_col": None,
            "offset": 0,
            "owners": [],
            "schema": get_example_default_schema(),
            "sql": None,
            "table_name": "energy_usage",
            "template_params": None,
            "uid": ANY,
            "datasource_name": "energy_usage",
            "name": f"{get_example_default_schema()}.energy_usage",
            "column_formats": {},
            "granularity_sqla": [],
            "time_grain_sqla": ANY,
            "order_by_choices": [
                ['["source", true]', "source [asc]"],
                ['["source", false]', "source [desc]"],
                ['["target", true]', "target [asc]"],
                ['["target", false]', "target [desc]"],
                ['["value", true]', "value [asc]"],
                ['["value", false]', "value [desc]"],
            ],
            "verbose_map": {
                "__timestamp": "Time",
                "count": "COUNT(*)",
                "source": "source",
                "sum__value": "sum__value",
                "target": "target",
                "value": "value",
            },
        }
        if response["result"]["database"]["backend"] not in ("presto", "hive"):
            assert {
                k: v for k, v in response["result"].items() if k in expected_result
            } == expected_result
        assert len(response["result"]["columns"]) == 3
        assert len(response["result"]["metrics"]) == 2

    def test_get_dataset_render_jinja(self):
        """
        Dataset API: Test get dataset with the render parameter.
        """
        database = get_example_database()
        dataset = SqlaTable(
            table_name="test_sql_table_with_jinja",
            database=database,
            schema=get_example_default_schema(),
            main_dttm_col="default_dttm",
            columns=[
                TableColumn(
                    column_name="my_user_id",
                    type="INTEGER",
                    is_dttm=False,
                ),
                TableColumn(
                    column_name="calculated_test",
                    type="VARCHAR(255)",
                    is_dttm=False,
                    expression="'{{ current_username() }}'",
                ),
            ],
            metrics=[
                SqlMetric(
                    metric_name="param_test",
                    expression="{{ url_param('multiplier') }} * 1.4",
                )
            ],
            sql="SELECT {{ current_user_id() }} as my_user_id",
        )
        db.session.add(dataset)
        db.session.commit()

        self.login(ADMIN_USERNAME)
        admin = self.get_user(ADMIN_USERNAME)
        uri = (
            f"api/v1/dataset/{dataset.id}?"
            "q=(columns:!(id,sql,columns.column_name,columns.expression,metrics.metric_name,metrics.expression))"
            "&include_rendered_sql=true&multiplier=4"
        )
        rv = self.get_assert_metric(uri, "get")
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))

        assert response["result"] == {
            "id": dataset.id,
            "sql": "SELECT {{ current_user_id() }} as my_user_id",
            "rendered_sql": f"SELECT {admin.id} as my_user_id",
            "columns": [
                {
                    "column_name": "my_user_id",
                    "expression": None,
                },
                {
                    "column_name": "calculated_test",
                    "expression": "'{{ current_username() }}'",
                    "rendered_expression": f"'{admin.username}'",
                },
            ],
            "metrics": [
                {
                    "metric_name": "param_test",
                    "expression": "{{ url_param('multiplier') }} * 1.4",
                    "rendered_expression": "4 * 1.4",
                },
            ],
        }

        self.items_to_delete = [dataset]

    def test_get_dataset_render_jinja_exceptions(self):
        """
        Dataset API: Test get dataset with the render parameter
        when rendering raises an exception.
        """
        database = get_example_database()
        dataset = SqlaTable(
            table_name="test_sql_table_with_incorrect_jinja",
            database=database,
            schema=get_example_default_schema(),
            main_dttm_col="default_dttm",
            columns=[
                TableColumn(
                    column_name="my_user_id",
                    type="INTEGER",
                    is_dttm=False,
                ),
                TableColumn(
                    column_name="calculated_test",
                    type="VARCHAR(255)",
                    is_dttm=False,
                    expression="'{{ current_username() }'",
                ),
            ],
            metrics=[
                SqlMetric(
                    metric_name="param_test",
                    expression="{{ url_param('multiplier') } * 1.4",
                )
            ],
            sql="SELECT {{ current_user_id() } as my_user_id",
        )
        db.session.add(dataset)
        db.session.commit()

        self.login(ADMIN_USERNAME)

        uri = f"api/v1/dataset/{dataset.id}?q=(columns:!(id,sql))&include_rendered_sql=true"  # noqa: E501
        rv = self.get_assert_metric(uri, "get")
        assert rv.status_code == 400
        response = json.loads(rv.data.decode("utf-8"))
        assert response["message"] == "Unable to render expression from dataset query."

        uri = (
            f"api/v1/dataset/{dataset.id}?q=(columns:!(id,metrics.expression))"
            "&include_rendered_sql=true&multiplier=4"
        )
        rv = self.get_assert_metric(uri, "get")
        assert rv.status_code == 400
        response = json.loads(rv.data.decode("utf-8"))
        assert response["message"] == "Unable to render expression from dataset metric."

        uri = (
            f"api/v1/dataset/{dataset.id}?q=(columns:!(id,columns.expression))"
            "&include_rendered_sql=true"
        )
        rv = self.get_assert_metric(uri, "get")
        assert rv.status_code == 400
        response = json.loads(rv.data.decode("utf-8"))
        assert (
            response["message"]
            == "Unable to render expression from dataset calculated column."
        )

        self.items_to_delete = [dataset]

    def test_get_dataset_distinct_schema(self):
        """
        Dataset API: Test get dataset distinct schema
        """

        def pg_test_query_parameter(query_parameter, expected_response):
            uri = f"api/v1/dataset/distinct/schema?q={prison.dumps(query_parameter)}"
            rv = self.client.get(uri)
            response = json.loads(rv.data.decode("utf-8"))
            assert rv.status_code == 200
            assert response == expected_response

        example_db = get_example_database()
        datasets = []
        if example_db.backend == "postgresql":
            datasets.append(
                self.insert_dataset(
                    "ab_permission", [], get_main_database(), schema="public"
                )
            )
            datasets.append(
                self.insert_dataset(
                    "columns",
                    [],
                    get_main_database(),
                    schema="information_schema",
                )
            )
            all_datasets = db.session.query(SqlaTable).all()
            schema_values = sorted(
                {
                    dataset.schema
                    for dataset in all_datasets
                    if dataset.schema is not None
                }
            )
            expected_response = {
                "count": len(schema_values),
                "result": [{"text": val, "value": val} for val in schema_values],
            }
            self.login(ADMIN_USERNAME)
            uri = "api/v1/dataset/distinct/schema"
            rv = self.client.get(uri)
            response = json.loads(rv.data.decode("utf-8"))
            assert rv.status_code == 200
            assert response == expected_response

            # Test filter
            query_parameter = {"filter": "inf"}
            pg_test_query_parameter(
                query_parameter,
                {
                    "count": 1,
                    "result": [
                        {"text": "information_schema", "value": "information_schema"}
                    ],
                },
            )

            query_parameter = {"page": 0, "page_size": 1}
            pg_test_query_parameter(
                query_parameter,
                {
                    "count": len(schema_values),
                    "result": [expected_response["result"][0]],
                },
            )

        self.items_to_delete = datasets

    def test_get_dataset_distinct_not_allowed(self):
        """
        Dataset API: Test get dataset distinct not allowed
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/distinct/table_name"
        rv = self.client.get(uri)
        assert rv.status_code == 404

    def test_get_dataset_distinct_gamma(self):
        """
        Dataset API: Test get dataset distinct with gamma
        """

        dataset = self.insert_default_dataset()

        self.login(GAMMA_USERNAME)
        uri = "api/v1/dataset/distinct/schema"
        rv = self.client.get(uri)
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        assert response["count"] == 0
        assert response["result"] == []

        self.items_to_delete = [dataset]

    def test_get_dataset_info(self):
        """
        Dataset API: Test get dataset info
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/_info"
        rv = self.get_assert_metric(uri, "info")
        assert rv.status_code == 200

    def test_info_security_dataset(self):
        """
        Dataset API: Test info security
        """

        self.login(ADMIN_USERNAME)
        params = {"keys": ["permissions"]}
        uri = f"api/v1/dataset/_info?q={prison.dumps(params)}"
        rv = self.get_assert_metric(uri, "info")
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        assert set(data["permissions"]) == {
            "can_read",
            "can_write",
            "can_export",
            "can_duplicate",
            "can_get_or_create_dataset",
            "can_warm_up_cache",
        }

    def test_create_dataset_item(self):
        """
        Dataset API: Test create dataset item
        """

        main_db = get_main_database()
        self.login(ADMIN_USERNAME)
        table_data = {
            "database": main_db.id,
            "schema": None,
            "table_name": "ab_permission",
        }
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, table_data, "post")
        assert rv.status_code == 201
        data = json.loads(rv.data.decode("utf-8"))
        table_id = data.get("id")
        model = db.session.query(SqlaTable).get(table_id)
        assert model.table_name == table_data["table_name"]
        assert model.database_id == table_data["database"]
        # normalize_columns should default to False
        assert model.normalize_columns is False

        # Assert that columns were created
        columns = (
            db.session.query(TableColumn)
            .filter_by(table_id=table_id)
            .order_by("column_name")
            .all()
        )
        assert columns[0].column_name == "id"
        assert columns[1].column_name == "name"

        # Assert that metrics were created
        columns = (
            db.session.query(SqlMetric)
            .filter_by(table_id=table_id)
            .order_by("metric_name")
            .all()
        )
        assert columns[0].expression == "COUNT(*)"

        self.items_to_delete = [model]

    def test_create_dataset_item_normalize(self):
        """
        Dataset API: Test create dataset item with column normalization enabled
        """

        main_db = get_main_database()
        self.login(ADMIN_USERNAME)
        table_data = {
            "database": main_db.id,
            "schema": None,
            "table_name": "ab_permission",
            "normalize_columns": True,
            "always_filter_main_dttm": False,
        }
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, table_data, "post")
        assert rv.status_code == 201
        data = json.loads(rv.data.decode("utf-8"))
        table_id = data.get("id")
        model = db.session.query(SqlaTable).get(table_id)
        assert model.table_name == table_data["table_name"]
        assert model.database_id == table_data["database"]
        assert model.normalize_columns is True

        self.items_to_delete = [model]

    def test_create_dataset_item_gamma(self):
        """
        Dataset API: Test create dataset item gamma
        """

        self.login(GAMMA_USERNAME)
        main_db = get_main_database()
        table_data = {
            "database": main_db.id,
            "schema": "",
            "table_name": "ab_permission",
        }
        uri = "api/v1/dataset/"
        rv = self.client.post(uri, json=table_data)
        assert rv.status_code == 403

    def test_create_dataset_item_owner(self):
        """
        Dataset API: Test create item owner
        """

        main_db = get_main_database()
        self.login(ALPHA_USERNAME)
        admin = self.get_user("admin")
        alpha = self.get_user("alpha")

        table_data = {
            "database": main_db.id,
            "schema": "",
            "table_name": "ab_permission",
            "owners": [admin.id],
        }
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, table_data, "post")
        assert rv.status_code == 201
        data = json.loads(rv.data.decode("utf-8"))
        model = db.session.query(SqlaTable).get(data.get("id"))
        assert admin in model.owners
        assert alpha in model.owners
        self.items_to_delete = [model]

    def test_create_dataset_item_owners_invalid(self):
        """
        Dataset API: Test create dataset item owner invalid
        """

        admin = self.get_user("admin")
        main_db = get_main_database()
        self.login(ADMIN_USERNAME)
        table_data = {
            "database": main_db.id,
            "schema": "",
            "table_name": "ab_permission",
            "owners": [admin.id, 1000],
        }
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, table_data, "post")
        assert rv.status_code == 422
        data = json.loads(rv.data.decode("utf-8"))
        expected_result = {"message": {"owners": ["Owners are invalid"]}}
        assert data == expected_result

    @pytest.mark.usefixtures("load_energy_table_with_slice")
    def test_create_dataset_with_sql(self):
        """
        Dataset API: Test create dataset with sql
        """

        energy_usage_ds = self.get_energy_usage_dataset()
        self.login(ALPHA_USERNAME)
        admin = self.get_user("admin")
        alpha = self.get_user("alpha")
        table_data = {
            "database": energy_usage_ds.database_id,
            "table_name": "energy_usage_virtual",
            "sql": "select * from energy_usage",
            "owners": [admin.id],
        }
        if schema := get_example_default_schema():
            table_data["schema"] = schema
        rv = self.post_assert_metric("/api/v1/dataset/", table_data, "post")
        assert rv.status_code == 201
        data = json.loads(rv.data.decode("utf-8"))
        model = db.session.query(SqlaTable).get(data.get("id"))
        assert admin in model.owners
        assert alpha in model.owners
        self.items_to_delete = [model]

    @unittest.skip("test is failing stochastically")
    def test_create_dataset_same_name_different_schema(self):
        if backend() == "sqlite":
            # sqlite doesn't support schemas
            return

        example_db = get_example_database()
        with example_db.get_sqla_engine() as engine:
            engine.execute(
                f"CREATE TABLE {CTAS_SCHEMA_NAME}.birth_names AS SELECT 2 as two"
            )

        self.login(ADMIN_USERNAME)
        table_data = {
            "database": example_db.id,
            "schema": CTAS_SCHEMA_NAME,
            "table_name": "birth_names",
        }

        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, table_data, "post")
        assert rv.status_code == 201

        # cleanup
        data = json.loads(rv.data.decode("utf-8"))
        uri = f"api/v1/dataset/{data.get('id')}"
        rv = self.client.delete(uri)
        assert rv.status_code == 200
        with example_db.get_sqla_engine() as engine:
            engine.execute(f"DROP TABLE {CTAS_SCHEMA_NAME}.birth_names")

    def test_create_dataset_validate_database(self):
        """
        Dataset API: Test create dataset validate database exists
        """

        self.login(ADMIN_USERNAME)
        dataset_data = {"database": 1000, "schema": "", "table_name": "birth_names"}
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, dataset_data, "post")
        assert rv.status_code == 422
        data = json.loads(rv.data.decode("utf-8"))
        assert data == {"message": {"database": ["Database does not exist"]}}

    def test_create_dataset_validate_tables_exists(self):
        """
        Dataset API: Test create dataset validate table exists
        """

        example_db = get_example_database()
        self.login(ADMIN_USERNAME)
        table_data = {
            "database": example_db.id,
            "schema": "",
            "table_name": "does_not_exist",
        }
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, table_data, "post")
        assert rv.status_code == 422

    @patch("superset.models.core.Database.get_columns")
    @patch("superset.models.core.Database.has_table")
    @patch("superset.models.core.Database.has_view")
    @patch("superset.models.core.Database.get_table")
    def test_create_dataset_validate_view_exists(
        self,
        mock_get_table,
        mock_has_table,
        mock_has_view,
        mock_get_columns,
    ):
        """
        Dataset API: Test create dataset validate view exists
        """

        mock_get_columns.return_value = [
            {
                "column_name": "col",
                "type": "VARCHAR",
                "type_generic": None,
                "is_dttm": None,
            }
        ]

        mock_has_table.return_value = False
        mock_has_view.return_value = True
        mock_get_table.return_value = None

        example_db = get_example_database()
        with example_db.get_sqla_engine() as engine:
            dialect = engine.dialect

            with patch.object(
                dialect, "get_view_names", wraps=dialect.get_view_names
            ) as patch_get_view_names:
                patch_get_view_names.return_value = {"test_case_view"}

            self.login(ADMIN_USERNAME)
            table_data = {
                "database": example_db.id,
                "schema": "",
                "table_name": "test_case_view",
            }

            uri = "api/v1/dataset/"
            rv = self.post_assert_metric(uri, table_data, "post")
            assert rv.status_code == 201

            # cleanup
            data = json.loads(rv.data.decode("utf-8"))
            uri = f"api/v1/dataset/{data.get('id')}"
            rv = self.client.delete(uri)
            assert rv.status_code == 200

    @patch("superset.daos.dataset.DatasetDAO.create")
    def test_create_dataset_sqlalchemy_error(self, mock_dao_create):
        """
        Dataset API: Test create dataset sqlalchemy error
        """

        mock_dao_create.side_effect = SQLAlchemyError()
        self.login(ADMIN_USERNAME)
        main_db = get_main_database()
        dataset_data = {
            "database": main_db.id,
            "schema": "",
            "table_name": "ab_permission",
        }
        uri = "api/v1/dataset/"
        rv = self.post_assert_metric(uri, dataset_data, "post")
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 422
        assert data == {"message": "Dataset could not be created."}

    @patch("superset.commands.dataset.create.security_manager.raise_for_access")
    def test_create_dataset_with_invalid_sql_validation(self, mock_raise_for_access):
        """
        Dataset API: Test create dataset with invalid SQL during validation returns 422
        """
        from superset.exceptions import SupersetParseError

        # Mock raise_for_access to throw SupersetParseError during validation
        mock_raise_for_access.side_effect = SupersetParseError(
            sql="SELECT FROM WHERE AND",
            engine="postgresql",
            message="Invalid SQL syntax",
        )

        self.login(ADMIN_USERNAME)
        examples_db = get_example_database()
        dataset_data = {
            "database": examples_db.id,
            "schema": "",
            "table_name": "invalid_sql_table",
            "sql": "SELECT FROM WHERE AND",
        }
        uri = "api/v1/dataset/"
        rv = self.client.post(uri, json=dataset_data)
        data = json.loads(rv.data.decode("utf-8"))
        # The error is caught during validation and returns 422
        assert rv.status_code == 422
        assert "sql" in data["message"]
        assert "Invalid SQL:" in data["message"]["sql"][0]

    def test_update_dataset_preserve_ownership(self):
        """
        Dataset API: Test update dataset preserves owner list (if un-changed)
        """

        dataset = self.insert_default_dataset()
        current_owners = dataset.owners
        self.login(username="admin")
        dataset_data = {"description": "new description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, dataset_data, "put")
        assert rv.status_code == 200
        model = db.session.query(SqlaTable).get(dataset.id)
        assert model.owners == current_owners

        self.items_to_delete = [dataset]

    def test_update_dataset_clear_owner_list(self):
        """
        Dataset API: Test update dataset admin can clear ownership config
        """

        dataset = self.insert_default_dataset()
        self.login(username="admin")
        dataset_data = {"owners": []}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, dataset_data, "put")
        assert rv.status_code == 200
        model = db.session.query(SqlaTable).get(dataset.id)
        assert model.owners == []

        self.items_to_delete = [dataset]

    def test_update_dataset_populate_owner(self):
        """
        Dataset API: Test update admin can update dataset with
        no owners to a different owner
        """
        self.login(username="admin")
        gamma = self.get_user("gamma")
        dataset = self.insert_dataset("ab_permission", [], get_main_database())
        dataset_data = {"owners": [gamma.id]}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, dataset_data, "put")
        assert rv.status_code == 200
        model = db.session.query(SqlaTable).get(dataset.id)
        assert model.owners == [gamma]

        self.items_to_delete = [dataset]

    def test_update_dataset_item(self):
        """
        Dataset API: Test update dataset item
        """

        dataset = self.insert_default_dataset()
        current_owners = dataset.owners
        self.login(ADMIN_USERNAME)
        dataset_data = {"description": "changed_description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, dataset_data, "put")
        assert rv.status_code == 200
        model = db.session.query(SqlaTable).get(dataset.id)
        assert model.description == dataset_data["description"]
        assert model.owners == current_owners

        self.items_to_delete = [dataset]

    def test_update_dataset_item_w_override_columns(self):
        """
        Dataset API: Test update dataset with override columns
        """

        # Add default dataset
        dataset = self.insert_default_dataset()
        self.login(ADMIN_USERNAME)
        new_col_dict = {
            "column_name": "new_col",
            "description": "description",
            "expression": "expression",
            "type": "INTEGER",
            "advanced_data_type": "ADVANCED_DATA_TYPE",
            "verbose_name": "New Col",
        }
        dataset_data = {
            "columns": [new_col_dict],
            "description": "changed description",
        }
        uri = f"api/v1/dataset/{dataset.id}?override_columns=true"
        rv = self.put_assert_metric(uri, dataset_data, "put")
        assert rv.status_code == 200

        columns = db.session.query(TableColumn).filter_by(table_id=dataset.id).all()

        assert new_col_dict["column_name"] in [col.column_name for col in columns]
        assert new_col_dict["description"] in [col.description for col in columns]
        assert new_col_dict["expression"] in [col.expression for col in columns]
        assert new_col_dict["type"] in [col.type for col in columns]
        assert new_col_dict["advanced_data_type"] in [
            col.advanced_data_type for col in columns
        ]

        self.items_to_delete = [dataset]

    def test_update_dataset_item_w_override_columns_same_columns(self):
        """
        Dataset API: Test update dataset with override columns
        """

        # Add default dataset
        get_main_database()  # noqa: F841
        dataset = self.insert_default_dataset()
        prev_col_len = len(dataset.columns)

        cols = [
            {
                "column_name": c.column_name,
                "description": c.description,
                "expression": c.expression,
                "type": c.type,
                "advanced_data_type": c.advanced_data_type,
                "verbose_name": c.verbose_name,
            }
            for c in dataset.columns
        ]

        cols.append(
            {
                "column_name": "new_col",
                "description": "description",
                "expression": "expression",
                "type": "INTEGER",
                "advanced_data_type": "ADVANCED_DATA_TYPE",
                "verbose_name": "New Col",
            }
        )

        self.login(ADMIN_USERNAME)
        dataset_data = {
            "columns": cols,
        }
        uri = f"api/v1/dataset/{dataset.id}?override_columns=true"
        rv = self.put_assert_metric(uri, dataset_data, "put")

        assert rv.status_code == 200

        columns = db.session.query(TableColumn).filter_by(table_id=dataset.id).all()
        assert len(columns) != prev_col_len
        assert len(columns) == 3
        self.items_to_delete = [dataset]

    def test_update_dataset_create_column_and_metric(self):
        """
        Dataset API: Test update dataset create column
        """
        # create example dataset by Command
        dataset = self.insert_default_dataset()
        current_changed_on = dataset.changed_on

        new_column_data = {
            "column_name": "new_col",
            "description": "description",
            "expression": "expression",
            "extra": '{"abc":123}',
            "type": "INTEGER",
            "advanced_data_type": "ADVANCED_DATA_TYPE",
            "verbose_name": "New Col",
            "uuid": "c626b60a-3fb2-4e99-9f01-53aca0b17166",
        }
        new_metric_data = {
            "d3format": None,
            "description": None,
            "expression": "COUNT(*)",
            "extra": '{"abc":123}',
            "metric_name": "my_count",
            "metric_type": None,
            "verbose_name": "My Count",
            "warning_text": None,
            "uuid": "051b5e72-4e6e-4860-b12b-4d530009dd2a",
        }
        uri = f"api/v1/dataset/{dataset.id}"

        # Get current cols and metrics and append the new ones
        self.login(ADMIN_USERNAME)
        rv = self.get_assert_metric(uri, "get")
        data = json.loads(rv.data.decode("utf-8"))

        for column in data["result"]["columns"]:
            column.pop("changed_on", None)
            column.pop("created_on", None)
            column.pop("type_generic", None)
        data["result"]["columns"].append(new_column_data)

        for metric in data["result"]["metrics"]:
            metric.pop("changed_on", None)
            metric.pop("created_on", None)
            metric.pop("type_generic", None)

        data["result"]["metrics"].append(new_metric_data)

        with freeze_time() as frozen:
            frozen.tick(delta=timedelta(seconds=3))
            rv = self.client.put(
                uri,
                json={
                    "columns": data["result"]["columns"],
                    "metrics": data["result"]["metrics"],
                },
            )

        assert rv.status_code == 200

        columns = (
            db.session.query(TableColumn)
            .filter_by(table_id=dataset.id)
            .order_by("column_name")
            .all()
        )

        assert columns[0].column_name == "id"
        assert columns[1].column_name == "name"
        assert columns[2].column_name == new_column_data["column_name"]
        assert columns[2].description == new_column_data["description"]
        assert columns[2].expression == new_column_data["expression"]
        assert columns[2].type == new_column_data["type"]
        assert columns[2].advanced_data_type == new_column_data["advanced_data_type"]
        assert columns[2].extra == new_column_data["extra"]
        assert columns[2].verbose_name == new_column_data["verbose_name"]
        assert str(columns[2].uuid) == new_column_data["uuid"]

        metrics = (
            db.session.query(SqlMetric)
            .filter_by(table_id=dataset.id)
            .order_by("metric_name")
            .all()
        )
        assert metrics[0].metric_name == "count"
        assert metrics[1].metric_name == "my_count"
        assert metrics[1].d3format == new_metric_data["d3format"]
        assert metrics[1].description == new_metric_data["description"]
        assert metrics[1].expression == new_metric_data["expression"]
        assert metrics[1].extra == new_metric_data["extra"]
        assert metrics[1].metric_type == new_metric_data["metric_type"]
        assert metrics[1].verbose_name == new_metric_data["verbose_name"]
        assert metrics[1].warning_text == new_metric_data["warning_text"]
        assert str(metrics[1].uuid) == new_metric_data["uuid"]

        # Validate that the changed_on is updated
        updated_dataset = db.session.query(SqlaTable).filter_by(id=dataset.id).first()
        assert updated_dataset.changed_on > current_changed_on

        self.items_to_delete = [dataset]

    def test_update_dataset_delete_column(self):
        """
        Dataset API: Test update dataset delete column
        """

        # create example dataset by Command
        dataset = self.insert_default_dataset()

        new_column_data = {
            "column_name": "new_col",
            "description": "description",
            "expression": "expression",
            "type": "INTEGER",
            "advanced_data_type": "ADVANCED_DATA_TYPE",
            "verbose_name": "New Col",
        }
        uri = f"api/v1/dataset/{dataset.id}"
        # Get current cols and append the new column
        self.login(ADMIN_USERNAME)
        rv = self.get_assert_metric(uri, "get")
        data = json.loads(rv.data.decode("utf-8"))

        for column in data["result"]["columns"]:
            column.pop("changed_on", None)
            column.pop("created_on", None)
            column.pop("type_generic", None)

        data["result"]["columns"].append(new_column_data)
        rv = self.client.put(uri, json={"columns": data["result"]["columns"]})

        assert rv.status_code == 200

        # Remove this new column
        data["result"]["columns"].remove(new_column_data)
        rv = self.client.put(uri, json={"columns": data["result"]["columns"]})
        assert rv.status_code == 200

        columns = (
            db.session.query(TableColumn)
            .filter_by(table_id=dataset.id)
            .order_by("column_name")
            .all()
        )
        assert columns[0].column_name == "id"
        assert columns[1].column_name == "name"
        assert len(columns) == 2

        self.items_to_delete = [dataset]

    def test_update_dataset_update_column(self):
        """
        Dataset API: Test update dataset columns
        """

        dataset = self.insert_default_dataset()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        # Get current cols and alter one
        rv = self.get_assert_metric(uri, "get")
        resp_columns = json.loads(rv.data.decode("utf-8"))["result"]["columns"]
        for column in resp_columns:
            column.pop("changed_on", None)
            column.pop("created_on", None)
            column.pop("type_generic", None)

        resp_columns[0]["groupby"] = False
        resp_columns[0]["filterable"] = False
        rv = self.client.put(uri, json={"columns": resp_columns})
        assert rv.status_code == 200
        columns = (
            db.session.query(TableColumn)
            .filter_by(table_id=dataset.id)
            .order_by("column_name")
            .all()
        )
        assert columns[0].column_name == "id"
        assert columns[1].column_name, "name"
        # TODO(bkyryliuk): find the reason why update is failing for the presto database
        if get_example_database().backend != "presto":
            assert columns[0].groupby is False
            assert columns[0].filterable is False

        self.items_to_delete = [dataset]

    def test_update_dataset_delete_metric(self):
        """
        Dataset API: Test update dataset delete metric
        """

        dataset = self.insert_default_dataset()
        metrics_query = (
            db.session.query(SqlMetric)
            .filter_by(table_id=dataset.id)
            .order_by("metric_name")
        )

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        data = {
            "metrics": [
                {"metric_name": "metric1", "expression": "COUNT(*)"},
                {"metric_name": "metric2", "expression": "DIFF_COUNT(*)"},
            ]
        }
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 200

        metrics = metrics_query.all()
        assert len(metrics) == 2

        data = {
            "metrics": [
                {
                    "id": metrics[0].id,
                    "metric_name": "metric1",
                    "expression": "COUNT(*)",
                },
            ]
        }
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 200

        metrics = metrics_query.all()
        assert len(metrics) == 1

        self.items_to_delete = [dataset]

    def test_update_dataset_update_column_uniqueness(self):
        """
        Dataset API: Test update dataset columns uniqueness
        """

        dataset = self.insert_default_dataset()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        # try to insert a new column ID that already exists
        data = {"columns": [{"column_name": "id", "type": "INTEGER"}]}
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 422
        data = json.loads(rv.data.decode("utf-8"))
        expected_result = {
            "message": {"columns": ["One or more columns already exist"]}
        }
        assert data == expected_result
        self.items_to_delete = [dataset]

    def test_update_dataset_update_metric_uniqueness(self):
        """
        Dataset API: Test update dataset metric uniqueness
        """

        dataset = self.insert_default_dataset()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        # try to insert a new column ID that already exists
        data = {"metrics": [{"metric_name": "count", "expression": "COUNT(*)"}]}
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 422
        data = json.loads(rv.data.decode("utf-8"))
        expected_result = {
            "message": {"metrics": ["One or more metrics already exist"]}
        }
        assert data == expected_result
        self.items_to_delete = [dataset]

    def test_update_dataset_update_column_duplicate(self):
        """
        Dataset API: Test update dataset columns duplicate
        """

        dataset = self.insert_default_dataset()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        # try to insert a new column ID that already exists
        data = {
            "columns": [
                {"column_name": "id", "type": "INTEGER"},
                {"column_name": "id", "type": "VARCHAR"},
            ]
        }
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 422
        data = json.loads(rv.data.decode("utf-8"))
        expected_result = {
            "message": {"columns": ["One or more columns are duplicated"]}
        }
        assert data == expected_result
        self.items_to_delete = [dataset]

    def test_update_dataset_update_metric_duplicate(self):
        """
        Dataset API: Test update dataset metric duplicate
        """

        dataset = self.insert_default_dataset()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        # try to insert a new column ID that already exists
        data = {
            "metrics": [
                {"metric_name": "dup", "expression": "COUNT(*)"},
                {"metric_name": "dup", "expression": "DIFF_COUNT(*)"},
            ]
        }
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 422
        data = json.loads(rv.data.decode("utf-8"))
        expected_result = {
            "message": {"metrics": ["One or more metrics are duplicated"]}
        }
        assert data == expected_result
        self.items_to_delete = [dataset]

    def test_update_dataset_update_metric_invalid_currency(self):
        """
        Dataset API: Test update dataset metric with an invalid currency config
        """

        dataset = self.insert_default_dataset()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        data = {
            "metrics": [
                {
                    "metric_name": "test",
                    "expression": "COUNT(*)",
                    "currency": '{"symbol": "USD", "symbolPosition": "suffix"}',
                },
            ]
        }
        rv = self.put_assert_metric(uri, data, "put")
        assert rv.status_code == 422

        self.items_to_delete = [dataset]

    def test_update_dataset_item_gamma(self):
        """
        Dataset API: Test update dataset item gamma
        """

        dataset = self.insert_default_dataset()
        self.login(GAMMA_USERNAME)
        table_data = {"description": "changed_description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.client.put(uri, json=table_data)
        assert rv.status_code == 403
        self.items_to_delete = [dataset]

    def test_dataset_get_list_no_username(self):
        """
        Dataset API: Tests that no username is returned
        """

        if backend() == "postgresql":
            # failing
            return

        dataset = self.insert_default_dataset()
        self.login(ADMIN_USERNAME)
        table_data = {"description": "changed_description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.client.put(uri, json=table_data)
        assert rv.status_code == 200

        response = self.get_assert_metric("api/v1/dataset/", "get_list")
        res = json.loads(response.data.decode("utf-8"))["result"]

        current_dataset = [d for d in res if d["id"] == dataset.id][0]
        assert current_dataset["description"] == "changed_description"
        assert "username" not in current_dataset["changed_by"].keys()

        self.items_to_delete = [dataset]

    def test_dataset_get_no_username(self):
        """
        Dataset API: Tests that no username is returned
        """

        dataset = self.insert_default_dataset()
        self.login(ADMIN_USERNAME)
        table_data = {"description": "changed_description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.client.put(uri, json=table_data)
        assert rv.status_code == 200

        response = self.get_assert_metric(uri, "get")
        res = json.loads(response.data.decode("utf-8"))["result"]

        assert res["description"] == "changed_description"
        assert "username" not in res["changed_by"].keys()

        self.items_to_delete = [dataset]

    def test_update_dataset_item_not_owned(self):
        """
        Dataset API: Test update dataset item not owned
        """

        dataset = self.insert_default_dataset()
        self.login(ALPHA_USERNAME)
        table_data = {"description": "changed_description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, table_data, "put")
        assert rv.status_code == 403
        self.items_to_delete = [dataset]

    def test_update_dataset_item_owners_invalid(self):
        """
        Dataset API: Test update dataset item owner invalid
        """

        dataset = self.insert_default_dataset()
        self.login(ADMIN_USERNAME)
        table_data = {"description": "changed_description", "owners": [1000]}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, table_data, "put")
        assert rv.status_code == 422
        self.items_to_delete = [dataset]

    @patch("superset.daos.dataset.DatasetDAO.update")
    def test_update_dataset_sqlalchemy_error(self, mock_dao_update):
        """
        Dataset API: Test update dataset sqlalchemy error
        """

        mock_dao_update.side_effect = SQLAlchemyError()

        dataset = self.insert_default_dataset()
        self.login(ADMIN_USERNAME)
        table_data = {"description": "changed_description"}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.client.put(uri, json=table_data)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 422
        assert data == {"message": "Dataset could not be updated."}

        self.items_to_delete = [dataset]

    @with_feature_flags(DATASET_FOLDERS=True)
    def test_update_dataset_add_folders(self):
        """
        Dataset API: Test adding folders to dataset
        """
        self.login(username="admin")

        dataset = self.insert_default_dataset()
        dataset_data = {
            "folders": [
                {
                    "type": "folder",
                    "uuid": "b49ac3dd-c79b-42a4-9082-39ee74f3b369",
                    "name": "My metrics",
                    "children": [
                        {
                            "type": "metric",
                            "uuid": str(dataset.metrics[0].uuid),
                        },
                    ],
                },
                {
                    "type": "folder",
                    "uuid": "f5db85fa-75d6-45e5-bdce-c6194db80642",
                    "name": "My columns",
                    "children": [
                        {
                            "type": "folder",
                            "uuid": "b5330233-e323-4157-b767-98b16f00ca93",
                            "name": "Dimensions",
                            "children": [
                                {
                                    "type": "column",
                                    "uuid": str(dataset.columns[1].uuid),
                                },
                            ],
                        },
                    ],
                },
            ]
        }

        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, dataset_data, "put")
        assert rv.status_code == 200

        model = db.session.query(SqlaTable).get(dataset.id)
        assert model.folders == [
            {
                "uuid": "b49ac3dd-c79b-42a4-9082-39ee74f3b369",
                "type": "folder",
                "name": "My metrics",
                "children": [
                    {
                        "uuid": str(dataset.metrics[0].uuid),
                        "type": "metric",
                    }
                ],
            },
            {
                "uuid": "f5db85fa-75d6-45e5-bdce-c6194db80642",
                "type": "folder",
                "name": "My columns",
                "children": [
                    {
                        "uuid": "b5330233-e323-4157-b767-98b16f00ca93",
                        "type": "folder",
                        "name": "Dimensions",
                        "children": [
                            {
                                "uuid": str(dataset.columns[1].uuid),
                                "type": "column",
                            }
                        ],
                    }
                ],
            },
        ]

        self.items_to_delete = [dataset]

    def test_update_dataset_change_db_connection_multi_catalog_disabled(self):
        """
        Dataset API: Test changing the DB connection powering the dataset
        to a connection with multi-catalog disabled.
        """
        self.login(ADMIN_USERNAME)

        db_connection = self.insert_database("db_connection")
        new_db_connection = self.insert_database("new_db_connection")
        dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=db_connection,
            sql="select 1 as one",
            schema="test_schema",
            catalog="old_default_catalog",
            fetch_metadata=False,
        )

        with patch.object(
            new_db_connection, "get_default_catalog", return_value="new_default_catalog"
        ):
            payload = {"database_id": new_db_connection.id}
            uri = f"api/v1/dataset/{dataset.id}"
            rv = self.put_assert_metric(uri, payload, "put")
            assert rv.status_code == 200

            model = db.session.query(SqlaTable).get(dataset.id)
            assert model.database == new_db_connection
            # Catalog should have been updated to new connection's default catalog
            assert model.catalog == "new_default_catalog"

        self.items_to_delete = [dataset, db_connection, new_db_connection]

    def test_update_dataset_change_db_connection_multi_catalog_enabled(self):
        """
        Dataset API: Test changing the DB connection powering the dataset
        to a connection with multi-catalog enabled.
        """
        self.login(ADMIN_USERNAME)

        db_connection = self.insert_database("db_connection")
        new_db_connection = self.insert_database(
            "new_db_connection", allow_multi_catalog=True
        )
        dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=db_connection,
            sql="select 1 as one",
            schema="test_schema",
            catalog="old_default_catalog",
            fetch_metadata=False,
        )

        with patch.object(
            new_db_connection, "get_default_catalog", return_value="default"
        ):
            payload = {"database_id": new_db_connection.id}
            uri = f"api/v1/dataset/{dataset.id}"
            rv = self.put_assert_metric(uri, payload, "put")
            assert rv.status_code == 200

        model = db.session.query(SqlaTable).get(dataset.id)
        assert model.database == new_db_connection
        # Catalog was not changed as not provided and multi-catalog is enabled
        assert model.catalog == "old_default_catalog"

        self.items_to_delete = [dataset, db_connection, new_db_connection]

    def test_update_dataset_change_db_connection_not_found(self):
        """
        Dataset API: Test changing the DB connection powering the dataset
        to an invalid DB connection.
        """
        self.login(ADMIN_USERNAME)

        dataset = self.insert_default_dataset()

        payload = {"database_id": 1500}
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.put_assert_metric(uri, payload, "put")
        response = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 422
        assert response["message"] == {"database": ["Database does not exist"]}

        self.items_to_delete = [dataset]

    def test_update_dataset_change_catalog(self):
        """
        Dataset API: Test changing the catalog associated with the dataset.
        """
        self.login(ADMIN_USERNAME)

        db_connection = self.insert_database("db_connection", allow_multi_catalog=True)
        dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=db_connection,
            sql="select 1 as one",
            schema="test_schema",
            catalog="test_catalog",
            fetch_metadata=False,
        )

        with patch.object(db_connection, "get_default_catalog", return_value="default"):
            payload = {"catalog": "other_catalog"}
            uri = f"api/v1/dataset/{dataset.id}"
            rv = self.put_assert_metric(uri, payload, "put")
            assert rv.status_code == 200

            model = db.session.query(SqlaTable).get(dataset.id)
            assert model.catalog == "other_catalog"

        self.items_to_delete = [dataset, db_connection]

    def test_update_dataset_change_catalog_not_allowed(self):
        """
        Dataset API: Test changing the catalog associated with the dataset fails
        when multi-catalog is disabled on the DB connection.
        """
        self.login(ADMIN_USERNAME)

        db_connection = self.insert_database("db_connection")
        dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=db_connection,
            sql="select 1 as one",
            schema="test_schema",
            catalog="test_catalog",
            fetch_metadata=False,
        )

        with patch.object(db_connection, "get_default_catalog", return_value="default"):
            payload = {"catalog": "other_catalog"}
            uri = f"api/v1/dataset/{dataset.id}"
            rv = self.put_assert_metric(uri, payload, "put")
            response = json.loads(rv.data.decode("utf-8"))
            assert rv.status_code == 422
            assert response["message"] == {
                "catalog": ["Only the default catalog is supported for this connection"]
            }

        self.items_to_delete = [dataset, db_connection]

    def test_update_dataset_validate_uniqueness(self):
        """
        Dataset API: Test the dataset uniqueness validation takes into
        consideration the new database connection.
        """
        test_db = get_main_database()
        if test_db.backend == "sqlite":
            # Skip this test for SQLite as it doesn't support multiple
            # schemas.
            return

        self.login(ADMIN_USERNAME)

        db_connection = self.insert_database("db_connection")
        new_db_connection = self.insert_database("new_db_connection")
        first_schema_dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=db_connection,
            sql="select 1 as one",
            schema="first_schema",
            fetch_metadata=False,
        )
        second_schema_dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=db_connection,
            sql="select 1 as one",
            schema="second_schema",
            fetch_metadata=False,
        )
        new_db_conn_dataset = self.insert_dataset(
            table_name="test_dataset",
            owners=[],
            database=new_db_connection,
            sql="select 1 as one",
            schema="first_schema",
            fetch_metadata=False,
        )

        with patch.object(
            db_connection,
            "get_default_catalog",
            return_value=None,
        ):
            payload = {"schema": "second_schema"}
            uri = f"api/v1/dataset/{first_schema_dataset.id}"
            rv = self.put_assert_metric(uri, payload, "put")
            response = json.loads(rv.data.decode("utf-8"))
            assert rv.status_code == 422
            assert response["message"] == {
                "table": ["Dataset second_schema.test_dataset already exists"]
            }

        with patch.object(
            new_db_connection,
            "get_default_catalog",
            return_value=None,
        ):
            payload["database_id"] = new_db_connection.id
            uri = f"api/v1/dataset/{first_schema_dataset.id}"
            rv = self.put_assert_metric(uri, payload, "put")
            assert rv.status_code == 200

        model = db.session.query(SqlaTable).get(first_schema_dataset.id)
        assert model.database == new_db_connection
        assert model.schema == "second_schema"

        self.items_to_delete = [
            first_schema_dataset,
            second_schema_dataset,
            new_db_conn_dataset,
            new_db_connection,
            db_connection,
        ]

    def test_delete_dataset_item(self):
        """
        Dataset API: Test delete dataset item
        """

        dataset = self.insert_default_dataset()
        view_menu = security_manager.find_view_menu(dataset.get_perm())
        assert view_menu is not None
        view_menu_id = view_menu.id
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 200
        non_view_menu = db.session.query(security_manager.viewmenu_model).get(
            view_menu_id
        )
        assert non_view_menu is None

    def test_delete_item_dataset_not_owned(self):
        """
        Dataset API: Test delete item not owned
        """

        dataset = self.insert_default_dataset()
        self.login(ALPHA_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.delete_assert_metric(uri, "delete")
        assert rv.status_code == 403
        self.items_to_delete = [dataset]

    def test_delete_dataset_item_not_authorized(self):
        """
        Dataset API: Test delete item not authorized
        """

        dataset = self.insert_default_dataset()
        self.login(GAMMA_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 403
        self.items_to_delete = [dataset]

    @patch("superset.daos.dataset.DatasetDAO.delete")
    def test_delete_dataset_sqlalchemy_error(self, mock_dao_delete):
        """
        Dataset API: Test delete dataset sqlalchemy error
        """

        mock_dao_delete.side_effect = SQLAlchemyError()

        dataset = self.insert_default_dataset()
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}"
        rv = self.delete_assert_metric(uri, "delete")
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 422
        assert data == {"message": "Datasets could not be deleted."}
        self.items_to_delete = [dataset]

    @pytest.mark.usefixtures("create_datasets")
    def test_delete_dataset_column(self):
        """
        Dataset API: Test delete dataset column
        """

        dataset = self.get_fixture_datasets()[0]
        column_id = dataset.columns[0].id
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/column/{column_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 200
        assert db.session.query(TableColumn).get(column_id) is None  # noqa: E711

    @pytest.mark.usefixtures("create_datasets")
    def test_delete_dataset_column_not_found(self):
        """
        Dataset API: Test delete dataset column not found
        """

        dataset = self.get_fixture_datasets()[0]
        non_id = self.get_nonexistent_numeric_id(TableColumn)

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/column/{non_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 404

        non_id = self.get_nonexistent_numeric_id(SqlaTable)
        column_id = dataset.columns[0].id

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{non_id}/column/{column_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 404

    @pytest.mark.usefixtures("create_datasets")
    def test_delete_dataset_column_not_owned(self):
        """
        Dataset API: Test delete dataset column not owned
        """

        dataset = self.get_fixture_datasets()[0]
        column_id = dataset.columns[0].id

        self.login(ALPHA_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/column/{column_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 403

    @pytest.mark.usefixtures("create_datasets")
    @patch("superset.daos.dataset.DatasetColumnDAO.delete")
    def test_delete_dataset_column_fail(self, mock_dao_delete):
        """
        Dataset API: Test delete dataset column
        """

        mock_dao_delete.side_effect = SQLAlchemyError()
        dataset = self.get_fixture_datasets()[0]
        column_id = dataset.columns[0].id
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/column/{column_id}"
        rv = self.client.delete(uri)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 422
        assert data == {"message": "Dataset column delete failed."}

    @pytest.mark.usefixtures("create_datasets")
    def test_delete_dataset_metric(self):
        """
        Dataset API: Test delete dataset metric
        """

        dataset = self.get_fixture_datasets()[0]
        test_metric = SqlMetric(
            metric_name="metric1", expression="COUNT(*)", table=dataset
        )
        db.session.add(test_metric)
        db.session.commit()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/metric/{test_metric.id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 200
        assert db.session.query(SqlMetric).get(test_metric.id) is None  # noqa: E711

    @pytest.mark.usefixtures("create_datasets")
    def test_delete_dataset_metric_not_found(self):
        """
        Dataset API: Test delete dataset metric not found
        """

        dataset = self.get_fixture_datasets()[0]
        non_id = self.get_nonexistent_numeric_id(SqlMetric)

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/metric/{non_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 404

        non_id = self.get_nonexistent_numeric_id(SqlaTable)
        metric_id = dataset.metrics[0].id

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{non_id}/metric/{metric_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 404

    @pytest.mark.usefixtures("create_datasets")
    def test_delete_dataset_metric_not_owned(self):
        """
        Dataset API: Test delete dataset metric not owned
        """

        dataset = self.get_fixture_datasets()[0]
        metric_id = dataset.metrics[0].id

        self.login(ALPHA_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/metric/{metric_id}"
        rv = self.client.delete(uri)
        assert rv.status_code == 403

    @pytest.mark.usefixtures("create_datasets")
    @patch("superset.daos.dataset.DatasetMetricDAO.delete")
    def test_delete_dataset_metric_fail(self, mock_dao_delete):
        """
        Dataset API: Test delete dataset metric
        """

        mock_dao_delete.side_effect = SQLAlchemyError()
        dataset = self.get_fixture_datasets()[0]
        column_id = dataset.metrics[0].id
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/metric/{column_id}"
        rv = self.client.delete(uri)
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 422
        assert data == {"message": "Dataset metric delete failed."}

    @pytest.mark.usefixtures("create_datasets")
    def test_bulk_delete_dataset_items(self):
        """
        Dataset API: Test bulk delete dataset items
        """

        datasets = self.get_fixture_datasets()
        dataset_ids = [dataset.id for dataset in datasets]

        view_menu_names = []
        for dataset in datasets:
            view_menu_names.append(dataset.get_perm())

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(dataset_ids)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        data = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        expected_response = {"message": f"Deleted {len(datasets)} datasets"}
        assert data == expected_response
        datasets = (
            db.session.query(SqlaTable)
            .filter(SqlaTable.table_name.in_(self.fixture_tables_names))
            .all()
        )
        assert datasets == []
        # Assert permissions get cleaned
        for view_menu_name in view_menu_names:
            assert security_manager.find_view_menu(view_menu_name) is None

    @pytest.mark.usefixtures("create_datasets")
    def test_bulk_delete_item_dataset_not_owned(self):
        """
        Dataset API: Test bulk delete item not owned
        """

        datasets = self.get_fixture_datasets()
        dataset_ids = [dataset.id for dataset in datasets]

        self.login(ALPHA_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(dataset_ids)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        assert rv.status_code == 403

    @pytest.mark.usefixtures("create_datasets")
    def test_bulk_delete_item_not_found(self):
        """
        Dataset API: Test bulk delete item not found
        """

        datasets = self.get_fixture_datasets()
        dataset_ids = [dataset.id for dataset in datasets]
        dataset_ids.append(db.session.query(func.max(SqlaTable.id)).scalar())

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(dataset_ids)}"
        rv = self.delete_assert_metric(uri, "bulk_delete")
        assert rv.status_code == 404

    @pytest.mark.usefixtures("create_datasets")
    def test_bulk_delete_dataset_item_not_authorized(self):
        """
        Dataset API: Test bulk delete item not authorized
        """

        datasets = self.get_fixture_datasets()
        dataset_ids = [dataset.id for dataset in datasets]

        self.login(GAMMA_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(dataset_ids)}"
        rv = self.client.delete(uri)
        assert rv.status_code == 403

    @pytest.mark.usefixtures("create_datasets")
    def test_bulk_delete_dataset_item_incorrect(self):
        """
        Dataset API: Test bulk delete item incorrect request
        """

        datasets = self.get_fixture_datasets()
        dataset_ids = [dataset.id for dataset in datasets]
        dataset_ids.append("Wrong")

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(dataset_ids)}"
        rv = self.client.delete(uri)
        assert rv.status_code == 400

    def test_dataset_item_refresh(self):
        """
        Dataset API: Test item refresh
        """

        dataset = self.insert_default_dataset()
        # delete a column
        id_column = (
            db.session.query(TableColumn)
            .filter_by(table_id=dataset.id, column_name="id")
            .one()
        )
        self.items_to_delete = [id_column]

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/refresh"
        rv = self.put_assert_metric(uri, {}, "refresh")
        assert rv.status_code == 200
        # Assert the column is restored on refresh
        id_column = (
            db.session.query(TableColumn)
            .filter_by(table_id=dataset.id, column_name="id")
            .one()
        )
        assert id_column is not None
        self.items_to_delete = [dataset]

    def test_dataset_item_refresh_not_found(self):
        """
        Dataset API: Test item refresh not found dataset
        """

        max_id = db.session.query(func.max(SqlaTable.id)).scalar()

        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/{max_id + 1}/refresh"
        rv = self.put_assert_metric(uri, {}, "refresh")
        assert rv.status_code == 404

    def test_dataset_item_refresh_not_owned(self):
        """
        Dataset API: Test item refresh not owned dataset
        """

        dataset = self.insert_default_dataset()
        self.login(ALPHA_USERNAME)
        uri = f"api/v1/dataset/{dataset.id}/refresh"
        rv = self.put_assert_metric(uri, {}, "refresh")
        assert rv.status_code == 403

        self.items_to_delete = [dataset]

    @unittest.skip("test is failing stochastically")
    def test_export_dataset(self):
        """
        Dataset API: Test export dataset
        """

        birth_names_dataset = self.get_birth_names_dataset()
        # TODO: fix test for presto
        # debug with dump: https://github.com/apache/superset/runs/1092546855
        if birth_names_dataset.database.backend in {"presto", "hive"}:
            return

        argument = [birth_names_dataset.id]
        uri = f"api/v1/dataset/export/?q={prison.dumps(argument)}"

        self.login(ADMIN_USERNAME)
        rv = self.get_assert_metric(uri, "export")
        assert rv.status_code == 200

        cli_export = export_to_dict(
            recursive=True,
            back_references=False,
            include_defaults=False,
        )
        cli_export_tables = cli_export["databases"][0]["tables"]
        expected_response = {}
        for export_table in cli_export_tables:
            if export_table["table_name"] == "birth_names":
                expected_response = export_table
                break
        ui_export = yaml.safe_load(rv.data.decode("utf-8"))
        assert ui_export[0] == expected_response

    def test_export_dataset_not_found(self):
        """
        Dataset API: Test export dataset not found
        """

        max_id = db.session.query(func.max(SqlaTable.id)).scalar()
        # Just one does not exist and we get 404
        argument = [max_id + 1, 1]
        uri = f"api/v1/dataset/export/?q={prison.dumps(argument)}"
        self.login(ADMIN_USERNAME)
        rv = self.get_assert_metric(uri, "export")
        assert rv.status_code == 404

    @pytest.mark.usefixtures("create_datasets")
    def test_export_dataset_gamma(self):
        """
        Dataset API: Test export dataset as gamma
        """

        dataset = self.get_fixture_datasets()[0]

        argument = [dataset.id]
        uri = f"api/v1/dataset/export/?q={prison.dumps(argument)}"

        self.login(GAMMA_USERNAME)
        rv = self.client.get(uri)
        assert rv.status_code in (403, 404)

        perm1 = security_manager.find_permission_view_menu("can_export", "Dataset")

        perm2 = security_manager.find_permission_view_menu(
            "datasource_access", dataset.perm
        )

        # add permissions to allow export + access to query this dataset
        gamma_role = security_manager.find_role("Gamma")
        security_manager.add_permission_role(gamma_role, perm1)
        security_manager.add_permission_role(gamma_role, perm2)

        rv = self.client.get(uri)
        assert rv.status_code == 200

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_export_dataset_bundle(self):
        """
        Dataset API: Test export dataset
        """

        birth_names_dataset = self.get_birth_names_dataset()
        # TODO: fix test for presto
        # debug with dump: https://github.com/apache/superset/runs/1092546855
        if birth_names_dataset.database.backend in {"presto", "hive"}:
            return

        argument = [birth_names_dataset.id]
        uri = f"api/v1/dataset/export/?q={prison.dumps(argument)}"

        self.login(ADMIN_USERNAME)
        rv = self.get_assert_metric(uri, "export")

        assert rv.status_code == 200

        buf = BytesIO(rv.data)
        assert is_zipfile(buf)

    def test_export_dataset_bundle_not_found(self):
        """
        Dataset API: Test export dataset not found
        """

        # Just one does not exist and we get 404
        argument = [-1, 1]
        uri = f"api/v1/dataset/export/?q={prison.dumps(argument)}"
        self.login(ADMIN_USERNAME)
        rv = self.get_assert_metric(uri, "export")

        assert rv.status_code in (403, 404)

    @pytest.mark.usefixtures("create_datasets")
    def test_export_dataset_bundle_gamma(self):
        """
        Dataset API: Test export dataset has gamma
        """

        dataset = self.get_fixture_datasets()[0]

        argument = [dataset.id]
        uri = f"api/v1/dataset/export/?q={prison.dumps(argument)}"

        self.login(GAMMA_USERNAME)
        rv = self.client.get(uri)
        # gamma users by default do not have access to this dataset
        assert rv.status_code in (403, 404)

    @unittest.skip("Number of related objects depend on DB")
    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    def test_get_dataset_related_objects(self):
        """
        Dataset API: Test get chart and dashboard count related to a dataset
        :return:
        """

        self.login(ADMIN_USERNAME)
        table = self.get_birth_names_dataset()
        uri = f"api/v1/dataset/{table.id}/related_objects"
        rv = self.get_assert_metric(uri, "related_objects")
        response = json.loads(rv.data.decode("utf-8"))
        assert rv.status_code == 200
        assert response["charts"]["count"] == 18
        assert response["dashboards"]["count"] == 1

    def test_get_dataset_related_objects_not_found(self):
        """
        Dataset API: Test related objects not found
        """

        max_id = db.session.query(func.max(SqlaTable.id)).scalar()
        # id does not exist and we get 404
        invalid_id = max_id + 1
        uri = f"api/v1/dataset/{invalid_id}/related_objects/"
        self.login(ADMIN_USERNAME)
        rv = self.client.get(uri)
        assert rv.status_code == 404
        self.logout()

        self.login(GAMMA_USERNAME)
        table = self.get_birth_names_dataset()
        uri = f"api/v1/dataset/{table.id}/related_objects"
        rv = self.client.get(uri)
        assert rv.status_code == 404

    @pytest.mark.usefixtures("create_datasets", "create_virtual_datasets")
    def test_get_datasets_custom_filter_sql(self):
        """
        Dataset API: Test custom dataset_is_null_or_empty filter for sql
        """

        arguments = {
            "filters": [
                {"col": "sql", "opr": "dataset_is_null_or_empty", "value": False}
            ]
        }
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)

        assert rv.status_code == 200

        data = json.loads(rv.data.decode("utf-8"))
        for table_name in self.fixture_virtual_table_names:
            assert table_name in [ds["table_name"] for ds in data["result"]]

        arguments = {
            "filters": [
                {"col": "sql", "opr": "dataset_is_null_or_empty", "value": True}
            ]
        }
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)
        assert rv.status_code == 200

        data = json.loads(rv.data.decode("utf-8"))
        for table_name in self.fixture_tables_names:
            assert table_name in [ds["table_name"] for ds in data["result"]]

    @patch("superset.commands.database.importers.v1.utils.add_permissions")
    def test_import_dataset(self, mock_add_permissions):
        """
        Dataset API: Test import dataset
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/import/"

        buf = self.create_import_v1_zip_file("dataset")
        form_data = {
            "formData": (buf, "dataset_export.zip"),
            "sync_columns": "true",
            "sync_metrics": "true",
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        database = (
            db.session.query(Database).filter_by(uuid=database_config["uuid"]).one()
        )

        assert database.database_name == "imported_database"

        assert len(database.tables) == 1
        dataset = database.tables[0]
        assert dataset.table_name == "imported_dataset"
        assert str(dataset.uuid) == dataset_config["uuid"]

        self.items_to_delete = [dataset, database]

    def test_import_dataset_v0_export(self):
        num_datasets = db.session.query(SqlaTable).count()

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/import/"

        buf = BytesIO()
        buf.write(json.dumps(dataset_ui_export).encode())
        buf.seek(0)
        form_data = {
            "formData": (buf, "dataset_export.zip"),
            "sync_columns": "true",
            "sync_metrics": "true",
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}
        assert db.session.query(SqlaTable).count() == num_datasets + 1

        dataset = (
            db.session.query(SqlaTable).filter_by(table_name="birth_names_2").one()
        )
        self.items_to_delete = [dataset]

    @patch("superset.commands.database.importers.v1.utils.add_permissions")
    def test_import_dataset_overwrite(self, mock_add_permissions):
        """
        Dataset API: Test import existing dataset
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/import/"

        buf = self.create_import_v1_zip_file("dataset")
        form_data = {
            "formData": (buf, "dataset_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        # import again without overwrite flag
        buf = self.create_import_v1_zip_file("dataset")
        form_data = {
            "formData": (buf, "dataset_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 422
        assert response == {
            "errors": [
                {
                    "message": "Error importing dataset",
                    "error_type": "GENERIC_COMMAND_ERROR",
                    "level": "warning",
                    "extra": {
                        "datasets/dataset.yaml": "Dataset already exists and `overwrite=true` was not passed",  # noqa: E501
                        "issue_codes": [
                            {
                                "code": 1010,
                                "message": "Issue 1010 - Superset encountered an error while running a command.",  # noqa: E501
                            }
                        ],
                    },
                }
            ]
        }

        # import with overwrite flag
        buf = self.create_import_v1_zip_file("dataset")
        form_data = {
            "formData": (buf, "dataset_export.zip"),
            "overwrite": "true",
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        # clean up
        database = (
            db.session.query(Database).filter_by(uuid=database_config["uuid"]).one()
        )
        dataset = database.tables[0]

        self.items_to_delete = [dataset, database]

    def test_import_dataset_invalid(self):
        """
        Dataset API: Test import invalid dataset
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/import/"

        buf = self.create_import_v1_zip_file("database", datasets=[dataset_config])
        form_data = {
            "formData": (buf, "dataset_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 422
        assert response == {
            "errors": [
                {
                    "message": "Error importing dataset",
                    "error_type": "GENERIC_COMMAND_ERROR",
                    "level": "warning",
                    "extra": {
                        "metadata.yaml": {"type": ["Must be equal to SqlaTable."]},
                        "issue_codes": [
                            {
                                "code": 1010,
                                "message": (
                                    "Issue 1010 - Superset encountered "
                                    "an error while running a command."
                                ),
                            }
                        ],
                    },
                }
            ]
        }

    def test_import_dataset_invalid_v0_validation(self):
        """
        Dataset API: Test import invalid dataset
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/import/"

        buf = BytesIO()
        with ZipFile(buf, "w") as bundle:
            with bundle.open(
                "dataset_export/databases/imported_database.yaml", "w"
            ) as fp:
                fp.write(yaml.safe_dump(database_config).encode())
            with bundle.open(
                "dataset_export/datasets/imported_dataset.yaml", "w"
            ) as fp:
                fp.write(yaml.safe_dump(dataset_config).encode())
        buf.seek(0)

        form_data = {
            "formData": (buf, "dataset_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 422
        assert response == {
            "errors": [
                {
                    "message": "Could not find a valid command to import file",
                    "error_type": "GENERIC_COMMAND_ERROR",
                    "level": "warning",
                    "extra": {
                        "issue_codes": [
                            {
                                "code": 1010,
                                "message": "Issue 1010 - Superset encountered an error while running a command.",  # noqa: E501
                            }
                        ]
                    },
                }
            ]
        }

    def test_import_dataset_currency_config(self):
        """
        Dataset API: Test import metric with currency config.

        This test confirms that importing a metric with a currency config
        set as either string (for backwards compatibility) or dict works properly.
        """
        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/import/"
        dataset_with_currency = copy.deepcopy(dataset_config)
        dataset_with_currency["metrics"][0]["currency"] = {
            "symbol": "USD",
            "symbolPosition": "left",
        }
        dataset_with_currency["metrics"].append(
            {
                "metric_name": "count_new",
                "verbose_name": "",
                "metric_type": None,
                "expression": "count(1)",
                "description": None,
                "d3format": None,
                "extra": {},
                "warning_text": None,
                "currency": '{"symbol": "EUR","symbolPosition": "left"}',
            }
        )

        buf = self.create_import_v1_zip_file(
            "dataset", datasets=[dataset_with_currency]
        )
        form_data = {
            "formData": (buf, "dataset_export.zip"),
        }
        rv = self.client.post(uri, data=form_data, content_type="multipart/form-data")
        response = json.loads(rv.data.decode("utf-8"))

        assert rv.status_code == 200
        assert response == {"message": "OK"}

        database = (
            db.session.query(Database).filter_by(uuid=database_config["uuid"]).one()
        )

        assert database.database_name == database_config["database_name"]

        assert len(database.tables) == 1
        assert len(database.tables[0].metrics) == 2
        final_metrics = []
        for metric in database.tables[0].metrics:
            final_metrics.append(metric.currency)
        assert final_metrics == [
            {"symbol": "USD", "symbolPosition": "left"},
            {"symbol": "EUR", "symbolPosition": "left"},
        ]
        dataset = database.tables[0]
        assert dataset.table_name == dataset_with_currency["table_name"]
        assert str(dataset.uuid) == dataset_with_currency["uuid"]

        self.items_to_delete = [dataset, database]

    @pytest.mark.usefixtures("create_datasets")
    def test_get_datasets_is_certified_filter(self):
        """
        Dataset API: Test custom dataset_is_certified filter
        """

        table_w_certification = SqlaTable(
            table_name="foo",
            schema=None,
            owners=[],
            database=get_main_database(),
            sql=None,
            extra='{"certification": 1}',
        )
        db.session.add(table_w_certification)
        db.session.commit()

        arguments = {
            "filters": [{"col": "id", "opr": "dataset_is_certified", "value": True}]
        }
        self.login(ADMIN_USERNAME)
        uri = f"api/v1/dataset/?q={prison.dumps(arguments)}"
        rv = self.client.get(uri)

        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        assert response.get("count") == 1

        self.items_to_delete = [table_w_certification]

    @pytest.mark.usefixtures("create_virtual_datasets")
    def test_duplicate_virtual_dataset(self):
        """
        Dataset API: Test duplicate virtual dataset
        """

        dataset = self.get_fixture_virtual_datasets()[0]

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/duplicate"  # noqa: F541
        table_data = {"base_model_id": dataset.id, "table_name": "Dupe1"}
        rv = self.post_assert_metric(uri, table_data, "duplicate")
        assert rv.status_code == 201
        rv_data = json.loads(rv.data)
        new_dataset: SqlaTable = (
            db.session.query(SqlaTable).filter_by(id=rv_data["id"]).one_or_none()
        )
        assert new_dataset is not None
        assert new_dataset.id != dataset.id
        assert new_dataset.table_name == "Dupe1"
        assert len(new_dataset.columns) == 2
        assert new_dataset.columns[0].column_name == "id"
        assert new_dataset.columns[1].column_name == "name"
        self.items_to_delete = [new_dataset]

    @pytest.mark.usefixtures("create_datasets")
    def test_duplicate_physical_dataset(self):
        """
        Dataset API: Test duplicate physical dataset
        """

        dataset = self.get_fixture_datasets()[0]

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/duplicate"  # noqa: F541
        table_data = {"base_model_id": dataset.id, "table_name": "Dupe2"}
        rv = self.post_assert_metric(uri, table_data, "duplicate")
        assert rv.status_code == 422

    @pytest.mark.usefixtures("create_virtual_datasets")
    def test_duplicate_existing_dataset(self):
        """
        Dataset API: Test duplicate dataset with existing name
        """

        dataset = self.get_fixture_virtual_datasets()[0]

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/duplicate"  # noqa: F541
        table_data = {
            "base_model_id": dataset.id,
            "table_name": "sql_virtual_dataset_2",
        }
        rv = self.post_assert_metric(uri, table_data, "duplicate")
        assert rv.status_code == 422

    def test_duplicate_invalid_dataset(self):
        """
        Dataset API: Test duplicate invalid dataset
        """

        self.login(ADMIN_USERNAME)
        uri = "api/v1/dataset/duplicate"  # noqa: F541
        table_data = {
            "base_model_id": -1,
            "table_name": "Dupe3",
        }
        rv = self.post_assert_metric(uri, table_data, "duplicate")
        assert rv.status_code == 422

    @pytest.mark.usefixtures("app_context", "virtual_dataset")
    def test_get_or_create_dataset_already_exists(self):
        """
        Dataset API: Test get or create endpoint when table already exists
        """
        self.login(ADMIN_USERNAME)
        rv = self.client.post(
            "api/v1/dataset/get_or_create/",
            json={
                "table_name": "virtual_dataset",
                "database_id": get_example_database().id,
            },
        )
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        dataset = (
            db.session.query(SqlaTable)
            .filter(SqlaTable.table_name == "virtual_dataset")
            .one()
        )
        assert response["result"] == {"table_id": dataset.id}

    def test_get_or_create_dataset_database_not_found(self):
        """
        Dataset API: Test get or create endpoint when database doesn't exist
        """
        self.login(ADMIN_USERNAME)
        rv = self.client.post(
            "api/v1/dataset/get_or_create/",
            json={"table_name": "virtual_dataset", "database_id": 999},
        )
        assert rv.status_code == 422
        response = json.loads(rv.data.decode("utf-8"))
        assert response["message"] == {"database": ["Database does not exist"]}

    @patch("superset.commands.dataset.create.CreateDatasetCommand.run")
    def test_get_or_create_dataset_create_fails(self, command_run_mock):
        """
        Dataset API: Test get or create endpoint when create fails
        """
        command_run_mock.side_effect = DatasetCreateFailedError
        self.login(ADMIN_USERNAME)
        rv = self.client.post(
            "api/v1/dataset/get_or_create/",
            json={
                "table_name": "virtual_dataset",
                "database_id": get_example_database().id,
            },
        )
        assert rv.status_code == 422
        response = json.loads(rv.data.decode("utf-8"))
        assert response["message"] == "Dataset could not be created."

    def test_get_or_create_dataset_creates_table(self):
        """
        Dataset API: Test get or create endpoint when table is created
        """
        self.login(ADMIN_USERNAME)

        examples_db = get_example_database()
        with examples_db.get_sqla_engine() as engine:
            engine.execute("DROP TABLE IF EXISTS test_create_sqla_table_api")
            engine.execute("CREATE TABLE test_create_sqla_table_api AS SELECT 2 as col")

        rv = self.client.post(
            "api/v1/dataset/get_or_create/",
            json={
                "table_name": "test_create_sqla_table_api",
                "database_id": examples_db.id,
                "template_params": '{"param": 1}',
            },
        )
        assert rv.status_code == 200
        response = json.loads(rv.data.decode("utf-8"))
        table = (
            db.session.query(SqlaTable)
            .filter_by(table_name="test_create_sqla_table_api")
            .one()
        )
        assert response["result"] == {"table_id": table.id}
        assert table.template_params == '{"param": 1}'
        assert table.normalize_columns is False

        self.items_to_delete = [table]

        with examples_db.get_sqla_engine() as engine:
            engine.execute("DROP TABLE test_create_sqla_table_api")

    @pytest.mark.usefixtures(
        "load_energy_table_with_slice", "load_birth_names_dashboard_with_slices"
    )
    def test_warm_up_cache(self):
        """
        Dataset API: Test warm up cache endpoint
        """
        self.login(ADMIN_USERNAME)
        energy_table = self.get_energy_usage_dataset()
        energy_charts = (
            db.session.query(Slice)
            .filter(
                Slice.datasource_id == energy_table.id, Slice.datasource_type == "table"
            )
            .all()
        )
        rv = self.client.put(
            "/api/v1/dataset/warm_up_cache",
            json={
                "table_name": "energy_usage",
                "db_name": get_example_database().database_name,
            },
        )
        assert rv.status_code == 200
        data = json.loads(rv.data.decode("utf-8"))
        assert len(data["result"]) == len(energy_charts)
        for chart_result in data["result"]:
            assert "chart_id" in chart_result
            assert "viz_error" in chart_result
            assert "viz_status" in chart_result

        # With dashboard id
        dashboard = self.get_dash_by_slug("births")
        birth_table = self.get_birth_names_dataset()
        birth_charts = (
            db.session.query(Slice)
            .filter(
                Slice.datasource_id == birth_table.id, Slice.datasource_type == "table"
            )
            .all()
        )
        rv = self.client.put(
            "/api/v1/dataset/warm_up_cache",
            json={
                "table_name": "birth_names",
                "db_name": get_example_database().database_name,
                "dashboard_id": dashboard.id,
            },
        )
        assert rv.status_code == 200
        data = json.loads(rv.data.decode("utf-8"))
        assert len(data["result"]) == len(birth_charts)
        for chart_result in data["result"]:
            assert "chart_id" in chart_result
            assert "viz_error" in chart_result
            assert "viz_status" in chart_result

        # With extra filters
        rv = self.client.put(
            "/api/v1/dataset/warm_up_cache",
            json={
                "table_name": "birth_names",
                "db_name": get_example_database().database_name,
                "dashboard_id": dashboard.id,
                "extra_filters": json.dumps(
                    [{"col": "name", "op": "in", "val": ["Jennifer"]}]
                ),
            },
        )
        assert rv.status_code == 200
        data = json.loads(rv.data.decode("utf-8"))
        assert len(data["result"]) == len(birth_charts)
        for chart_result in data["result"]:
            assert "chart_id" in chart_result
            assert "viz_error" in chart_result
            assert "viz_status" in chart_result

    def test_warm_up_cache_db_and_table_name_required(self):
        self.login(ADMIN_USERNAME)
        rv = self.client.put("/api/v1/dataset/warm_up_cache", json={"dashboard_id": 1})
        assert rv.status_code == 400
        data = json.loads(rv.data.decode("utf-8"))
        assert data == {
            "message": {
                "db_name": ["Missing data for required field."],
                "table_name": ["Missing data for required field."],
            }
        }

    def test_warm_up_cache_table_not_found(self):
        self.login(ADMIN_USERNAME)
        rv = self.client.put(
            "/api/v1/dataset/warm_up_cache",
            json={"table_name": "not_here", "db_name": "abc"},
        )
        assert rv.status_code == 404
        data = json.loads(rv.data.decode("utf-8"))
        assert data == {
            "message": "The provided table was not found in the provided database"
        }
