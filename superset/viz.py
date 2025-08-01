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
# pylint: disable=too-many-lines
"""This module contains the 'Viz' objects

These objects represent the backend of all the visualizations that
Superset can render.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import math
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from itertools import product
from typing import Any, cast, Optional, TYPE_CHECKING

import geohash
import numpy as np
import pandas as pd
import polyline
from dateutil import relativedelta as rdelta
from deprecation import deprecated
from flask import current_app, request
from flask_babel import lazy_gettext as _
from geopy.point import Point
from pandas.tseries.frequencies import to_offset

from superset.common.db_query_status import QueryStatus
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import (
    CacheLoadError,
    NullValueException,
    QueryObjectValidationError,
    SpatialException,
    SupersetSecurityException,
)
from superset.extensions import cache_manager, security_manager
from superset.models.helpers import QueryResult
from superset.sql.parse import sanitize_clause
from superset.superset_typing import (
    Column,
    Metric,
    QueryObjectDict,
    VizData,
    VizPayload,
)
from superset.utils import core as utils, csv, json
from superset.utils.cache import set_and_log_cache
from superset.utils.core import (
    apply_max_row_limit,
    DateColumn,
    DTTM_ALIAS,
    ExtraFiltersReasonType,
    get_column_name,
    get_column_names,
    get_column_names_from_columns,
    JS_MAX_INTEGER,
    merge_extra_filters,
    simple_filter_to_adhoc,
)
from superset.utils.date_parser import get_since_until, parse_past_timedelta
from superset.utils.hashing import md5_sha_from_str

if TYPE_CHECKING:
    from superset.connectors.sqla.models import BaseDatasource

logger = logging.getLogger(__name__)

METRIC_KEYS = [
    "metric",
    "metrics",
    "percent_metrics",
    "metric_2",
    "secondary_metric",
    "x",
    "y",
    "size",
]


class BaseViz:  # pylint: disable=too-many-public-methods
    """All visualizations derive this base class"""

    viz_type: str | None = None
    verbose_name = "Base Viz"
    credits = ""
    is_timeseries = False
    cache_type = "df"
    enforce_numerical_metrics = True

    @deprecated(deprecated_in="3.0")
    def __init__(
        self,
        datasource: BaseDatasource,
        form_data: dict[str, Any],
        force: bool = False,
        force_cached: bool = False,
    ) -> None:
        if not datasource:
            raise QueryObjectValidationError(_("Viz is missing a datasource"))

        self.datasource = datasource
        self.request = request
        self.viz_type = form_data.get("viz_type")
        self.form_data = form_data

        self.query = ""
        self.token = utils.get_form_data_token(form_data)

        self.groupby: list[Column] = self.form_data.get("groupby") or []
        self.time_shift = timedelta()

        self.status: str | None = None
        self.error_msg = ""
        self.results: QueryResult | None = None
        self.applied_filter_columns: list[Column] = []
        self.rejected_filter_columns: list[Column] = []
        self.errors: list[dict[str, Any]] = []
        self.force = force
        self._force_cached = force_cached
        self.from_dttm: datetime | None = None
        self.to_dttm: datetime | None = None
        self._extra_chart_data: list[tuple[str, pd.DataFrame]] = []

        self.process_metrics()

        self.applied_filters: list[dict[str, str]] = []
        self.rejected_filters: list[dict[str, str]] = []

    @property
    @deprecated(deprecated_in="3.0")
    def force_cached(self) -> bool:
        return self._force_cached

    @deprecated(deprecated_in="3.0")
    def process_metrics(self) -> None:
        # metrics in Viz is order sensitive, so metric_dict should be
        # OrderedDict
        self.metric_dict = OrderedDict()
        for mkey in METRIC_KEYS:
            val = self.form_data.get(mkey)
            if val:
                if not isinstance(val, list):
                    val = [val]
                for o in val:
                    label = utils.get_metric_name(o)
                    self.metric_dict[label] = o

        # Cast to list needed to return serializable object in py3
        self.all_metrics = list(self.metric_dict.values())
        self.metric_labels = list(self.metric_dict.keys())

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def handle_js_int_overflow(
        data: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        for record in data.get("records", {}):
            for k, v in list(record.items()):
                if isinstance(v, int):
                    # if an int is too big for Java Script to handle
                    # convert it to a string
                    if abs(v) > JS_MAX_INTEGER:
                        record[k] = str(v)
        return data

    @deprecated(deprecated_in="3.0")
    def run_extra_queries(self) -> None:
        """Lifecycle method to use when more than one query is needed

        In rare-ish cases, a visualization may need to execute multiple
        queries. That is the case for FilterBox or for time comparison
        in Line chart for instance.

        In those cases, we need to make sure these queries run before the
        main `get_payload` method gets called, so that the overall caching
        metadata can be right. The way it works here is that if any of
        the previous `get_df_payload` calls hit the cache, the main
        payload's metadata will reflect that.

        The multi-query support may need more work to become a first class
        use case in the framework, and for the UI to reflect the subtleties
        (show that only some of the queries were served from cache for
        instance). In the meantime, since multi-query is rare, we treat
        it with a bit of a hack. Note that the hack became necessary
        when moving from caching the visualization's data itself, to caching
        the underlying query(ies).
        """

    @deprecated(deprecated_in="3.0")
    def apply_rolling(self, df: pd.DataFrame) -> pd.DataFrame:
        rolling_type = self.form_data.get("rolling_type")
        rolling_periods = int(self.form_data.get("rolling_periods") or 0)
        min_periods = int(self.form_data.get("min_periods") or 0)

        if rolling_type in ("mean", "std", "sum") and rolling_periods:
            kwargs = {"window": rolling_periods, "min_periods": min_periods}
            if rolling_type == "mean":
                df = df.rolling(**kwargs).mean()
            elif rolling_type == "std":
                df = df.rolling(**kwargs).std()
            elif rolling_type == "sum":
                df = df.rolling(**kwargs).sum()
        elif rolling_type == "cumsum":
            df = df.cumsum()
        if min_periods:
            df = df[min_periods:]
        if df.empty:
            raise QueryObjectValidationError(
                _(
                    "Applied rolling window did not return any data. Please make sure "
                    "the source query satisfies the minimum periods defined in the "
                    "rolling window."
                )
            )
        return df

    @deprecated(deprecated_in="3.0")
    def get_samples(self) -> dict[str, Any]:
        query_obj = self.query_obj()
        query_obj.update(
            {
                "is_timeseries": False,
                "groupby": [],
                "metrics": [],
                "orderby": [],
                "row_limit": current_app.config["SAMPLES_ROW_LIMIT"],
                "columns": [o.column_name for o in self.datasource.columns],
                "from_dttm": None,
                "to_dttm": None,
            }
        )
        payload = self.get_df_payload(query_obj)  # leverage caching logic
        return {
            "data": payload["df"].to_dict(orient="records"),
            "colnames": payload.get("colnames"),
            "coltypes": payload.get("coltypes"),
            "rowcount": payload.get("rowcount"),
            "sql_rowcount": payload.get("sql_rowcount"),
        }

    @deprecated(deprecated_in="3.0")
    def get_df(self, query_obj: QueryObjectDict | None = None) -> pd.DataFrame:
        """Returns a pandas dataframe based on the query object"""
        if not query_obj:
            query_obj = self.query_obj()
        if not query_obj:
            return pd.DataFrame()

        self.error_msg = ""

        timestamp_format = None
        if self.datasource.type == "table":
            granularity_col = self.datasource.get_column(query_obj["granularity"])
            if granularity_col:
                timestamp_format = granularity_col.python_date_format

        # The datasource here can be different backend but the interface is common
        self.results = self.datasource.query(query_obj)
        self.applied_filter_columns = self.results.applied_filter_columns or []
        self.rejected_filter_columns = self.results.rejected_filter_columns or []
        self.query = self.results.query
        self.status = self.results.status
        self.errors = self.results.errors

        df = self.results.df
        # Transform the timestamp we received from database to pandas supported
        # datetime format. If no python_date_format is specified, the pattern will
        # be considered as the default ISO date format
        # If the datetime format is unix, the parse will use the corresponding
        # parsing logic.
        if not df.empty:
            utils.normalize_dttm_col(
                df=df,
                dttm_cols=tuple(  # noqa: C409
                    [
                        DateColumn.get_legacy_time_column(
                            timestamp_format=timestamp_format,
                            offset=self.datasource.offset,
                            time_shift=self.form_data.get("time_shift"),
                        )
                    ]
                ),
            )

            if self.enforce_numerical_metrics:
                self.df_metrics_to_num(df)

            df.replace([np.inf, -np.inf], np.nan, inplace=True)
        return df

    @deprecated(deprecated_in="3.0")
    def df_metrics_to_num(self, df: pd.DataFrame) -> None:
        """Converting metrics to numeric when pandas.read_sql cannot"""
        metrics = self.metric_labels
        for col, dtype in df.dtypes.items():
            if dtype.type == np.object_ and col in metrics:
                df[col] = pd.to_numeric(df[col], errors="coerce")

    @deprecated(deprecated_in="3.0")
    def process_query_filters(self) -> None:
        utils.convert_legacy_filters_into_adhoc(self.form_data)
        merge_extra_filters(self.form_data)
        engine = self.datasource.database.db_engine_spec.engine
        utils.split_adhoc_filters_into_base_filters(self.form_data, engine)

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def dedup_columns(*columns_args: list[Column] | None) -> list[Column]:
        # dedup groupby and columns while preserving order
        labels: list[str] = []
        deduped_columns: list[Column] = []
        for columns in columns_args:
            for column in columns or []:
                label = get_column_name(column)
                if label not in labels:
                    deduped_columns.append(column)
        return deduped_columns

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:  # pylint: disable=too-many-locals
        """Building a query object"""
        self.process_query_filters()

        metrics = self.all_metrics or []

        groupby = self.dedup_columns(self.groupby, self.form_data.get("columns"))

        is_timeseries = self.is_timeseries

        if DTTM_ALIAS in (groupby_labels := get_column_names(groupby)):
            del groupby[groupby_labels.index(DTTM_ALIAS)]
            is_timeseries = True

        granularity = self.form_data.get("granularity_sqla")
        limit = int(self.form_data.get("limit") or 0)
        timeseries_limit_metric = self.form_data.get("timeseries_limit_metric")

        # apply row limit to query
        row_limit = int(
            self.form_data.get("row_limit") or current_app.config["ROW_LIMIT"]
        )
        row_limit = apply_max_row_limit(row_limit)

        # default order direction
        order_desc = self.form_data.get("order_desc", True)

        try:
            since, until = get_since_until(
                relative_start=current_app.config["DEFAULT_RELATIVE_START_TIME"],
                relative_end=current_app.config["DEFAULT_RELATIVE_END_TIME"],
                time_range=self.form_data.get("time_range"),
                since=self.form_data.get("since"),
                until=self.form_data.get("until"),
            )
        except ValueError as ex:
            raise QueryObjectValidationError(str(ex)) from ex

        time_shift = self.form_data.get("time_shift", "")
        self.time_shift = parse_past_timedelta(time_shift)
        from_dttm = None if since is None else (since - self.time_shift)
        to_dttm = None if until is None else (until - self.time_shift)
        if from_dttm and to_dttm and from_dttm > to_dttm:
            raise QueryObjectValidationError(
                _("From date cannot be larger than to date")
            )

        self.from_dttm = from_dttm
        self.to_dttm = to_dttm

        # validate sql filters
        for param in ("where", "having"):
            clause = self.form_data.get(param)
            if clause:
                engine = self.datasource.database.db_engine_spec.engine
                sanitized_clause = sanitize_clause(clause, engine)
                if sanitized_clause != clause:
                    self.form_data[param] = sanitized_clause

        # extras are used to query elements specific to a datasource type
        # for instance the extra where clause that applies only to Tables
        extras = {
            "having": self.form_data.get("having", ""),
            "time_grain_sqla": self.form_data.get("time_grain_sqla"),
            "where": self.form_data.get("where", ""),
        }

        return {
            "granularity": granularity,
            "from_dttm": from_dttm,
            "to_dttm": to_dttm,
            "is_timeseries": is_timeseries,
            "groupby": groupby,
            "metrics": metrics,
            "row_limit": row_limit,
            "filter": self.form_data.get("filters", []),
            "timeseries_limit": limit,
            "extras": extras,
            "timeseries_limit_metric": timeseries_limit_metric,
            "order_desc": order_desc,
        }

    @property
    @deprecated(deprecated_in="3.0")
    def cache_timeout(self) -> int:
        if self.form_data.get("cache_timeout") is not None:
            return int(self.form_data["cache_timeout"])
        if self.datasource.cache_timeout is not None:
            return self.datasource.cache_timeout
        if (
            hasattr(self.datasource, "database")
            and self.datasource.database.cache_timeout
        ) is not None:
            return self.datasource.database.cache_timeout
        if (
            current_app.config["DATA_CACHE_CONFIG"].get("CACHE_DEFAULT_TIMEOUT")
            is not None
        ):
            return current_app.config["DATA_CACHE_CONFIG"]["CACHE_DEFAULT_TIMEOUT"]
        return current_app.config["CACHE_DEFAULT_TIMEOUT"]

    @deprecated(deprecated_in="3.0")
    def get_json(self) -> str:
        return json.dumps(
            self.get_payload(), default=json.json_int_dttm_ser, ignore_nan=True
        )

    @deprecated(deprecated_in="3.0")
    def cache_key(self, query_obj: QueryObjectDict, **extra: Any) -> str:
        """
        The cache key is made out of the key/values in `query_obj`, plus any
        other key/values in `extra`.

        We remove datetime bounds that are hard values, and replace them with
        the use-provided inputs to bounds, which may be time-relative (as in
        "5 days ago" or "now").

        The `extra` arguments are currently used by time shift queries, since
        different time shifts will differ only in the `from_dttm`, `to_dttm`,
        `inner_from_dttm`, and `inner_to_dttm` values which are stripped.
        """
        cache_dict = copy.copy(query_obj)
        cache_dict.update(extra)

        for k in ["from_dttm", "to_dttm", "inner_from_dttm", "inner_to_dttm"]:
            if k in cache_dict:
                del cache_dict[k]

        cache_dict["time_range"] = self.form_data.get("time_range")
        cache_dict["datasource"] = self.datasource.uid
        cache_dict["extra_cache_keys"] = self.datasource.get_extra_cache_keys(query_obj)
        cache_dict["rls"] = security_manager.get_rls_cache_key(self.datasource)
        cache_dict["changed_on"] = self.datasource.changed_on
        json_data = self.json_dumps(cache_dict, sort_keys=True)
        return md5_sha_from_str(json_data)

    @deprecated(deprecated_in="3.0")
    def get_payload(self, query_obj: QueryObjectDict | None = None) -> VizPayload:
        """Returns a payload of metadata and data"""

        try:
            self.run_extra_queries()
        except SupersetSecurityException as ex:
            error = dataclasses.asdict(ex.error)
            self.errors.append(error)
            self.status = QueryStatus.FAILED

        payload = self.get_df_payload(query_obj)

        # if payload does not have a df, we are raising an error here.
        df = cast(Optional[pd.DataFrame], payload["df"])

        if self.status != QueryStatus.FAILED:
            payload["data"] = self.get_data(df)
        if "df" in payload:
            del payload["df"]

        applied_filter_columns = self.applied_filter_columns or []
        rejected_filter_columns = self.rejected_filter_columns or []
        applied_time_extras = self.form_data.get("applied_time_extras", {})
        applied_time_columns, rejected_time_columns = utils.get_time_filter_status(
            self.datasource, applied_time_extras
        )
        payload["applied_filters"] = [
            {"column": get_column_name(col)} for col in applied_filter_columns
        ] + applied_time_columns
        payload["rejected_filters"] = [
            {
                "reason": ExtraFiltersReasonType.COL_NOT_IN_DATASOURCE,
                "column": get_column_name(col),
            }
            for col in rejected_filter_columns
        ] + rejected_time_columns
        if df is not None:
            payload["colnames"] = list(df.columns)
        return payload

    @deprecated(deprecated_in="3.0")
    def get_df_payload(  # pylint: disable=too-many-statements  # noqa: C901
        self, query_obj: QueryObjectDict | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """Handles caching around the df payload retrieval"""
        if not query_obj:
            query_obj = self.query_obj()
        cache_key = self.cache_key(query_obj, **kwargs) if query_obj else None
        cache_value = None
        logger.info("Cache key: %s", cache_key)
        is_loaded = False
        stacktrace = None
        df = None
        cache_timeout = self.cache_timeout
        force = self.force or cache_timeout == -1
        if cache_key and cache_manager.data_cache and not force:
            cache_value = cache_manager.data_cache.get(cache_key)
            if cache_value:
                current_app.config["STATS_LOGGER"].incr("loading_from_cache")
                try:
                    df = cache_value["df"]
                    self.query = cache_value["query"]
                    self.applied_filter_columns = cache_value.get(
                        "applied_filter_columns", []
                    )
                    self.rejected_filter_columns = cache_value.get(
                        "rejected_filter_columns", []
                    )
                    self.status = QueryStatus.SUCCESS
                    is_loaded = True
                    current_app.config["STATS_LOGGER"].incr("loaded_from_cache")
                except Exception as ex:  # pylint: disable=broad-except
                    logger.exception(ex)
                    logger.error(
                        "Error reading cache: %s",
                        utils.error_msg_from_exception(ex),
                        exc_info=True,
                    )
                logger.info("Serving from cache")

        if query_obj and not is_loaded:
            if self.force_cached:
                logger.warning(
                    "force_cached (viz.py): value not found for cache key %s",
                    cache_key,
                )
                raise CacheLoadError(_("Cached value not found"))
            try:
                invalid_columns = [
                    col
                    for col in get_column_names_from_columns(
                        query_obj.get("columns") or []
                    )
                    + get_column_names_from_columns(query_obj.get("groupby") or [])
                    + utils.get_column_names_from_metrics(
                        cast(list[Metric], query_obj.get("metrics") or [])
                    )
                    if col not in self.datasource.column_names
                ]
                if invalid_columns:
                    raise QueryObjectValidationError(
                        _(
                            "Columns missing in datasource: %(invalid_columns)s",
                            invalid_columns=invalid_columns,
                        )
                    )
                df = self.get_df(query_obj)
                if self.status != QueryStatus.FAILED:
                    current_app.config["STATS_LOGGER"].incr("loaded_from_source")
                    if not self.force:
                        current_app.config["STATS_LOGGER"].incr(
                            "loaded_from_source_without_force"
                        )
                    is_loaded = True
            except QueryObjectValidationError as ex:
                error = dataclasses.asdict(
                    SupersetError(
                        message=str(ex),
                        level=ErrorLevel.ERROR,
                        error_type=SupersetErrorType.VIZ_GET_DF_ERROR,
                    )
                )
                self.errors.append(error)
                self.status = QueryStatus.FAILED
            except Exception as ex:  # pylint: disable=broad-except
                logger.exception(ex)

                error = dataclasses.asdict(
                    SupersetError(
                        message=str(ex),
                        level=ErrorLevel.ERROR,
                        error_type=SupersetErrorType.VIZ_GET_DF_ERROR,
                    )
                )
                self.errors.append(error)
                self.status = QueryStatus.FAILED
                stacktrace = utils.get_stacktrace()

            if is_loaded and cache_key and self.status != QueryStatus.FAILED:
                set_and_log_cache(
                    cache_instance=cache_manager.data_cache,
                    cache_key=cache_key,
                    cache_value={"df": df, "query": self.query},
                    cache_timeout=cache_timeout,
                    datasource_uid=self.datasource.uid,
                )
        return {
            "cache_key": cache_key,
            "cached_dttm": cache_value["dttm"] if cache_value is not None else None,
            "cache_timeout": cache_timeout,
            "df": df,
            "errors": self.errors,
            "form_data": self.form_data,
            "is_cached": cache_value is not None,
            "query": self.query,
            "from_dttm": self.from_dttm,
            "to_dttm": self.to_dttm,
            "status": self.status,
            "stacktrace": stacktrace,
            "rowcount": len(df.index) if df is not None else 0,
            "colnames": list(df.columns) if df is not None else None,
            "coltypes": (
                utils.extract_dataframe_dtypes(df, self.datasource)
                if df is not None
                else None
            ),
        }

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def json_dumps(query_obj: Any, sort_keys: bool = False) -> str:
        return json.dumps(
            query_obj,
            default=json.json_int_dttm_ser,
            ignore_nan=True,
            sort_keys=sort_keys,
        )

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def has_error(payload: VizPayload) -> bool:
        return (
            payload.get("status") == QueryStatus.FAILED
            or payload.get("error") is not None
            or bool(payload.get("errors"))
        )

    @deprecated(deprecated_in="3.0")
    def payload_json_and_has_error(self, payload: VizPayload) -> tuple[str, bool]:
        return self.json_dumps(payload), self.has_error(payload)

    @property
    @deprecated(deprecated_in="3.0")
    def data(self) -> dict[str, Any]:
        """This is the data object serialized to the js layer"""
        content = {
            "form_data": self.form_data,
            "token": self.token,
            "viz_name": self.viz_type,
            "filter_select_enabled": self.datasource.filter_select_enabled,
        }
        return content

    @deprecated(deprecated_in="3.0")
    def get_csv(self) -> str | None:
        df = self.get_df_payload()["df"]  # leverage caching logic
        include_index = not isinstance(df.index, pd.RangeIndex)
        return csv.df_to_escaped_csv(
            df, index=include_index, **current_app.config["CSV_EXPORT"]
        )

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        return df.to_dict(orient="records")

    @property
    @deprecated(deprecated_in="3.0")
    def json_data(self) -> str:
        return json.dumps(self.data)

    @deprecated(deprecated_in="3.0")
    def raise_for_access(self) -> None:
        """
        Raise an exception if the user cannot access the resource.

        :raises SupersetSecurityException: If the user cannot access the resource
        """

        security_manager.raise_for_access(viz=self)


class TimeTableViz(BaseViz):
    """A data table with rich time-series related columns"""

    viz_type = "time_table"
    verbose_name = _("Time Table View")
    credits = 'a <a href="https://github.com/airbnb/superset">Superset</a> original'
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()

        if not self.form_data.get("metrics"):
            raise QueryObjectValidationError(_("Pick at least one metric"))

        if self.form_data.get("groupby") and len(self.form_data["metrics"]) > 1:
            raise QueryObjectValidationError(
                _("When using 'Group By' you are limited to use a single metric")
            )

        sort_by = utils.get_first_metric_name(query_obj["metrics"])
        is_asc = not query_obj.get("order_desc")
        query_obj["orderby"] = [(sort_by, is_asc)]

        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        columns = None
        values: list[str] | str = self.metric_labels
        if self.form_data.get("groupby"):
            values = self.metric_labels[0]
            columns = get_column_names(self.form_data.get("groupby"))
        pt = df.pivot_table(index=DTTM_ALIAS, columns=columns, values=values)
        pt.index = pt.index.map(str)
        pt = pt.sort_index()
        return {
            "records": pt.to_dict(orient="index"),
            "columns": list(pt.columns),
            "is_group_by": bool(self.form_data.get("groupby")),
        }


class CalHeatmapViz(BaseViz):
    """Calendar heatmap."""

    viz_type = "cal_heatmap"
    verbose_name = _("Calendar Heatmap")
    credits = "<a href=https://github.com/wa0x6e/cal-heatmap>cal-heatmap</a>"
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:  # pylint: disable=too-many-locals  # noqa: C901
        if df.empty:
            return None

        form_data = self.form_data
        data = {}
        records = df.to_dict("records")
        for metric in self.metric_labels:
            values = {}
            for query_obj in records:
                v = query_obj[DTTM_ALIAS]
                if hasattr(v, "value"):
                    v = v.value
                values[str(v / 10**9)] = query_obj.get(metric)
            data[metric] = values

        try:
            start, end = get_since_until(
                relative_start=current_app.config["DEFAULT_RELATIVE_START_TIME"],
                relative_end=current_app.config["DEFAULT_RELATIVE_END_TIME"],
                time_range=form_data.get("time_range"),
                since=form_data.get("since"),
                until=form_data.get("until"),
            )
        except ValueError as ex:
            raise QueryObjectValidationError(str(ex)) from ex
        if not start or not end:
            raise QueryObjectValidationError(
                "Please provide both time bounds (Since and Until)"
            )
        domain = form_data.get("domain_granularity")
        diff_delta = rdelta.relativedelta(end, start)
        diff_secs = (end - start).total_seconds()

        if domain == "year":
            range_ = end.year - start.year + 1
        elif domain == "month":
            range_ = diff_delta.years * 12 + diff_delta.months + 1
        elif domain == "week":
            range_ = diff_delta.years * 53 + diff_delta.weeks + 1
        elif domain == "day":
            range_ = diff_secs // (24 * 60 * 60) + 1  # type: ignore
        else:
            range_ = diff_secs // (60 * 60) + 1  # type: ignore

        return {
            "data": data,
            "start": start,
            "domain": domain,
            "subdomain": form_data.get("subdomain_granularity"),
            "range": range_,
        }

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["metrics"] = self.form_data.get("metrics")
        mapping = {
            "min": "PT1M",
            "hour": "PT1H",
            "day": "P1D",
            "week": "P1W",
            "month": "P1M",
            "year": "P1Y",
        }
        query_obj["extras"]["time_grain_sqla"] = mapping[
            self.form_data.get("subdomain_granularity", "min")
        ]
        return query_obj


class NVD3Viz(BaseViz):
    """Base class for all nvd3 vizs"""

    credits = '<a href="http://nvd3.org/">NVD3.org</a>'
    viz_type: str | None = None
    verbose_name = "Base NVD3 Viz"
    is_timeseries = False


class BubbleViz(NVD3Viz):
    """Based on the NVD3 bubble chart"""

    viz_type = "bubble"
    verbose_name = _("Bubble Chart")
    is_timeseries = False

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [self.form_data.get("entity")]
        if self.form_data.get("series"):
            query_obj["groupby"].append(self.form_data.get("series"))

        # dedup groupby if it happens to be the same
        query_obj["groupby"] = self.dedup_columns(query_obj["groupby"])

        # pylint: disable=attribute-defined-outside-init
        self.x_metric = self.form_data["x"]
        self.y_metric = self.form_data["y"]
        self.z_metric = self.form_data["size"]
        self.entity = self.form_data.get("entity")
        self.series = self.form_data.get("series") or self.entity
        query_obj["row_limit"] = self.form_data.get("limit")

        query_obj["metrics"] = [self.z_metric, self.x_metric, self.y_metric]
        if len(set(self.metric_labels)) < 3:
            raise QueryObjectValidationError(_("Please use 3 different metric labels"))
        if not all(query_obj["metrics"] + [self.entity]):
            raise QueryObjectValidationError(_("Pick a metric for x, y and size"))
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df["x"] = df[[utils.get_metric_name(self.x_metric)]]
        df["y"] = df[[utils.get_metric_name(self.y_metric)]]
        df["size"] = df[[utils.get_metric_name(self.z_metric)]]
        df["shape"] = "circle"
        df["group"] = df[[get_column_name(self.series)]]  # type: ignore

        series: dict[Any, list[Any]] = defaultdict(list)
        for row in df.to_dict(orient="records"):
            series[row["group"]].append(row)
        chart_data = []
        for k, v in series.items():
            chart_data.append({"key": k, "values": v})
        return chart_data


class BulletViz(NVD3Viz):
    """Based on the NVD3 bullet chart"""

    viz_type = "bullet"
    verbose_name = _("Bullet Chart")
    is_timeseries = False

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        form_data = self.form_data
        query_obj = super().query_obj()
        self.metric = form_data[  # pylint: disable=attribute-defined-outside-init
            "metric"
        ]

        query_obj["metrics"] = [self.metric]
        if not self.metric:
            raise QueryObjectValidationError(_("Pick a metric to display"))
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        df["metric"] = df[[utils.get_metric_name(self.metric)]]
        values = df["metric"].values
        return {
            "measures": values.tolist(),
        }


class NVD3TimeSeriesViz(NVD3Viz):
    """A rich line chart component with tons of options"""

    viz_type = "line"
    verbose_name = _("Time Series - Line Chart")
    sort_series = False
    is_timeseries = True
    pivot_fill_value: int | None = None

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        sort_by = self.form_data.get(
            "timeseries_limit_metric"
        ) or utils.get_first_metric_name(query_obj.get("metrics") or [])
        is_asc = not self.form_data.get("order_desc")
        if sort_by:
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            query_obj["orderby"] = [(sort_by, is_asc)]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def to_series(  # pylint: disable=too-many-branches  # noqa: C901
        self, df: pd.DataFrame, classed: str = "", title_suffix: str = ""
    ) -> list[dict[str, Any]]:
        cols = []
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        series = df.to_dict("series")

        chart_data = []
        for name in df.T.index.tolist():
            ys = series[name]
            if df[name].dtype.kind not in "biufc":
                continue
            series_title: list[str] | str | tuple[str, ...]
            if isinstance(name, list):
                series_title = [str(title) for title in name]
            elif isinstance(name, tuple):
                series_title = tuple(str(title) for title in name)
            else:
                series_title = str(name)
            if (
                isinstance(series_title, (list, tuple))
                and len(series_title) > 1
                and len(self.metric_labels) == 1
            ):
                # Removing metric from series name if only one metric
                series_title = series_title[1:]
            if title_suffix:
                if isinstance(series_title, str):
                    series_title = (series_title, title_suffix)
                elif isinstance(series_title, list):
                    series_title = series_title + [title_suffix]
                elif isinstance(series_title, tuple):
                    series_title = series_title + (title_suffix,)

            values = []
            non_nan_cnt = 0
            for ds in df.index:
                if ds in ys:
                    data = {"x": ds, "y": ys[ds]}
                    if not np.isnan(ys[ds]):
                        non_nan_cnt += 1
                else:
                    data = {}
                values.append(data)

            if non_nan_cnt == 0:
                continue

            data = {"key": series_title, "values": values}
            if classed:
                data["classed"] = classed
            chart_data.append(data)
        return chart_data

    @deprecated(deprecated_in="3.0")
    def process_data(self, df: pd.DataFrame, aggregate: bool = False) -> VizData:
        if df.empty:
            return df

        if aggregate:
            df = df.pivot_table(
                index=DTTM_ALIAS,
                columns=get_column_names(self.form_data.get("groupby")),
                values=self.metric_labels,
                fill_value=0,
                aggfunc=sum,
            )
        else:
            df = df.pivot_table(
                index=DTTM_ALIAS,
                columns=get_column_names(self.form_data.get("groupby")),
                values=self.metric_labels,
                fill_value=self.pivot_fill_value,
            )

        rule = self.form_data.get("resample_rule")
        method = self.form_data.get("resample_method")

        if rule and method:
            df = getattr(df.resample(rule), method)()

        if self.sort_series:
            dfs = df.sum()
            dfs.sort_values(ascending=False, inplace=True)
            df = df[dfs.index]

        df = self.apply_rolling(df)
        if self.form_data.get("contribution"):
            dft = df.T
            df = (dft / dft.sum()).T

        return df

    @deprecated(deprecated_in="3.0")
    def run_extra_queries(self) -> None:
        time_compare = self.form_data.get("time_compare") or []
        # backwards compatibility
        if not isinstance(time_compare, list):
            time_compare = [time_compare]

        for option in time_compare:
            query_object = self.query_obj()
            try:
                delta = parse_past_timedelta(option)
            except ValueError as ex:
                raise QueryObjectValidationError(str(ex)) from ex
            query_object["inner_from_dttm"] = query_object["from_dttm"]
            query_object["inner_to_dttm"] = query_object["to_dttm"]

            if not query_object["from_dttm"] or not query_object["to_dttm"]:
                raise QueryObjectValidationError(
                    _(
                        "An enclosed time range (both start and end) must be specified "
                        "when using a Time Comparison."
                    )
                )
            query_object["from_dttm"] -= delta
            query_object["to_dttm"] -= delta

            df2 = self.get_df_payload(query_object, time_compare=option).get("df")
            if df2 is not None and DTTM_ALIAS in df2:
                dttm_series = df2[DTTM_ALIAS] + delta
                df2 = df2.drop(DTTM_ALIAS, axis=1)
                df2 = pd.concat([dttm_series, df2], axis=1)
                label = f"{option} offset"
                df2 = self.process_data(df2)
                self._extra_chart_data.append((label, df2))

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        comparison_type = self.form_data.get("comparison_type") or "values"
        df = self.process_data(df)
        if comparison_type == "values":
            # Filter out series with all NaN
            chart_data = self.to_series(df.dropna(axis=1, how="all"))

            for i, (label, df2) in enumerate(self._extra_chart_data):
                chart_data.extend(
                    self.to_series(df2, classed=f"time-shift-{i}", title_suffix=label)
                )
        else:
            chart_data = []
            for i, (label, df2) in enumerate(self._extra_chart_data):
                # reindex df2 into the df2 index
                combined_index = df.index.union(df2.index)
                df2 = (
                    df2.reindex(combined_index)
                    .interpolate(method="time")
                    .reindex(df.index)
                )

                if comparison_type == "absolute":
                    diff = df - df2
                elif comparison_type == "percentage":
                    diff = (df - df2) / df2
                elif comparison_type == "ratio":
                    diff = df / df2
                else:
                    raise QueryObjectValidationError(
                        f"Invalid `comparison_type`: {comparison_type}"
                    )

                # remove leading/trailing NaNs from the time shift difference
                diff = diff[diff.first_valid_index() : diff.last_valid_index()]

                chart_data.extend(
                    self.to_series(diff, classed=f"time-shift-{i}", title_suffix=label)
                )

        if not self.sort_series:
            chart_data = sorted(chart_data, key=lambda x: tuple(x["key"]))
        return chart_data


class NVD3TimePivotViz(NVD3TimeSeriesViz):
    """Time Series - Periodicity Pivot"""

    viz_type = "time_pivot"
    sort_series = True
    verbose_name = _("Time Series - Period Pivot")

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["metrics"] = [self.form_data.get("metric")]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df = self.process_data(df)
        freq = to_offset(self.form_data.get("freq"))
        try:
            freq = type(freq)(freq.n, normalize=True, **freq.kwds)
        except ValueError:
            freq = type(freq)(freq.n, **freq.kwds)
        df.index.name = None
        df[DTTM_ALIAS] = df.index.map(freq.rollback)
        df["ranked"] = df[DTTM_ALIAS].rank(method="dense", ascending=False) - 1
        df.ranked = df.ranked.map(int)
        df["series"] = "-" + df.ranked.map(str)
        df["series"] = df["series"].str.replace("-0", "current")
        rank_lookup = {
            row["series"]: row["ranked"] for row in df.to_dict(orient="records")
        }
        max_ts = df[DTTM_ALIAS].max()
        max_rank = df["ranked"].max()
        df[DTTM_ALIAS] = df.index + (max_ts - df[DTTM_ALIAS])
        df = df.pivot_table(
            index=DTTM_ALIAS,
            columns="series",
            values=utils.get_metric_name(self.form_data["metric"]),
        )
        chart_data = self.to_series(df)
        for series in chart_data:
            series["rank"] = rank_lookup[series["key"]]
            series["perc"] = 1 - (series["rank"] / (max_rank + 1))
        return chart_data


class NVD3CompareTimeSeriesViz(NVD3TimeSeriesViz):
    """A line chart component where you can compare the % change over time"""

    viz_type = "compare"
    verbose_name = _("Time Series - Percent Change")


class ChordViz(BaseViz):
    """A Chord diagram"""

    viz_type = "chord"
    verbose_name = _("Directed Force Layout")
    credits = '<a href="https://github.com/d3/d3-chord">Bostock</a>'
    is_timeseries = False

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [
            self.form_data.get("groupby"),
            self.form_data.get("columns"),
        ]
        query_obj["metrics"] = [self.form_data.get("metric")]
        if self.form_data.get("sort_by_metric", False):
            query_obj["orderby"] = [(query_obj["metrics"][0], False)]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        df.columns = ["source", "target", "value"]

        # Preparing a symmetrical matrix like d3.chords calls for
        nodes = list(set(df["source"]) | set(df["target"]))
        matrix = {}
        for source, target in product(nodes, nodes):
            matrix[(source, target)] = 0
        for source, target, value in df.to_records(index=False):
            matrix[(source, target)] = value
        return {
            "nodes": list(nodes),
            "matrix": [[matrix[(n1, n2)] for n1 in nodes] for n2 in nodes],
        }


class CountryMapViz(BaseViz):
    """A country centric"""

    viz_type = "country_map"
    verbose_name = _("Country Map")
    is_timeseries = False
    credits = "From bl.ocks.org By john-guerra"

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        metric = self.form_data.get("metric")
        entity = self.form_data.get("entity")
        if not self.form_data.get("select_country"):
            raise QueryObjectValidationError("Must specify a country")
        if not metric:
            raise QueryObjectValidationError("Must specify a metric")
        if not entity:
            raise QueryObjectValidationError("Must provide ISO codes")
        query_obj["metrics"] = [metric]
        query_obj["groupby"] = [entity]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        cols = get_column_names([self.form_data.get("entity")])  # type: ignore
        metric = self.metric_labels[0]
        cols += [metric]
        ndf = df[cols]
        df = ndf
        df.columns = ["country_id", "metric"]
        return df.to_dict(orient="records")


class WorldMapViz(BaseViz):
    """A country centric world map"""

    viz_type = "world_map"
    verbose_name = _("World Map")
    is_timeseries = False
    credits = 'datamaps on <a href="https://www.npmjs.com/package/datamaps">npm</a>'

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [self.form_data["entity"]]
        if self.form_data.get("sort_by_metric", False):
            query_obj["orderby"] = [(query_obj["metrics"][0], False)]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        # pylint: disable=import-outside-toplevel
        from superset.examples import countries

        cols = get_column_names([self.form_data.get("entity")])  # type: ignore
        metric = utils.get_metric_name(self.form_data["metric"])
        secondary_metric = (
            utils.get_metric_name(self.form_data["secondary_metric"])
            if self.form_data.get("secondary_metric")
            else None
        )
        columns = ["country", "m1", "m2"]
        if metric == secondary_metric:
            ndf = df[cols]
            ndf["m1"] = df[metric]
            ndf["m2"] = ndf["m1"]
        else:
            if secondary_metric:
                cols += [metric, secondary_metric]
            else:
                cols += [metric]
                columns = ["country", "m1"]
            ndf = df[cols]
        df = ndf
        df.columns = columns
        data = df.to_dict(orient="records")
        for row in data:
            country = None
            if isinstance(row["country"], str):
                if "country_fieldtype" in self.form_data:
                    country = countries.get(
                        self.form_data["country_fieldtype"], row["country"]
                    )
            if country:
                row["country"] = country["cca3"]
                row["latitude"] = country["lat"]
                row["longitude"] = country["lng"]
                row["name"] = country["name"]
            else:
                row["country"] = "XXX"
        return data


class ParallelCoordinatesViz(BaseViz):
    """Interactive parallel coordinate implementation

    Uses this amazing javascript library
    https://github.com/syntagmatic/parallel-coordinates
    """

    viz_type = "para"
    verbose_name = _("Parallel Coordinates")
    credits = (
        '<a href="https://syntagmatic.github.io/parallel-coordinates/">'
        "Syntagmatic's library</a>"
    )
    is_timeseries = False

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["groupby"] = [self.form_data.get("series")]
        if sort_by := self.form_data.get("timeseries_limit_metric"):
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                query_obj["orderby"] = [
                    (sort_by, not self.form_data.get("order_desc", True))
                ]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        return df.to_dict(orient="records")


class HorizonViz(NVD3TimeSeriesViz):
    """Horizon chart

    https://www.npmjs.com/package/d3-horizon-chart
    """

    viz_type = "horizon"
    verbose_name = _("Horizon Charts")
    credits = (
        '<a href="https://www.npmjs.com/package/d3-horizon-chart">d3-horizon-chart</a>'
    )


class MapboxViz(BaseViz):
    """Rich maps made with Mapbox"""

    viz_type = "mapbox"
    verbose_name = _("Mapbox")
    is_timeseries = False
    credits = "<a href=https://www.mapbox.com/mapbox-gl-js/api/>Mapbox GL JS</a>"

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        label_col = self.form_data.get("mapbox_label")

        if not self.form_data.get("groupby"):
            if (
                self.form_data.get("all_columns_x") is None
                or self.form_data.get("all_columns_y") is None
            ):
                raise QueryObjectValidationError(
                    _("[Longitude] and [Latitude] must be set")
                )
            query_obj["columns"] = [
                self.form_data.get("all_columns_x"),
                self.form_data.get("all_columns_y"),
            ]

            if label_col and len(label_col) >= 1:
                if label_col[0] == "count":
                    raise QueryObjectValidationError(
                        _(
                            "Must have a [Group By] column to have 'count' as the "
                            + "[Label]"
                        )
                    )
                query_obj["columns"].append(label_col[0])

            if self.form_data.get("point_radius") != "Auto":
                query_obj["columns"].append(self.form_data.get("point_radius"))

            # Ensure this value is sorted so that it does not
            # cause the cache key generation (which hashes the
            # query object) to generate different keys for values
            # that should be considered the same.
            query_obj["columns"] = sorted(set(query_obj["columns"]))
        else:
            # Ensuring columns chosen are all in group by
            if (
                label_col
                and len(label_col) >= 1
                and label_col[0] != "count"
                and label_col[0] not in self.form_data["groupby"]
            ):
                raise QueryObjectValidationError(
                    _("Choice of [Label] must be present in [Group By]")
                )

            if (
                self.form_data.get("point_radius") != "Auto"
                and self.form_data.get("point_radius") not in self.form_data["groupby"]
            ):
                raise QueryObjectValidationError(
                    _("Choice of [Point Radius] must be present in [Group By]")
                )

            if (
                self.form_data.get("all_columns_x") not in self.form_data["groupby"]
                or self.form_data.get("all_columns_y") not in self.form_data["groupby"]
            ):
                raise QueryObjectValidationError(
                    _(
                        "[Longitude] and [Latitude] columns must be present in "
                        + "[Group By]"
                    )
                )
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        label_col = self.form_data.get("mapbox_label")
        has_custom_metric = label_col is not None and len(label_col) > 0
        metric_col = [None] * len(df.index)
        if has_custom_metric:
            if label_col[0] == self.form_data.get("all_columns_x"):  # type: ignore
                metric_col = df[self.form_data.get("all_columns_x")]
            elif label_col[0] == self.form_data.get("all_columns_y"):  # type: ignore
                metric_col = df[self.form_data.get("all_columns_y")]
            else:
                metric_col = df[label_col[0]]  # type: ignore
        point_radius_col = (
            [None] * len(df.index)
            if self.form_data.get("point_radius") == "Auto"
            else df[self.form_data.get("point_radius")]
        )

        # limiting geo precision as long decimal values trigger issues
        # around json-bignumber in Mapbox
        geo_precision = 10
        # using geoJSON formatting
        geo_json = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"metric": metric, "radius": point_radius},
                    "geometry": {
                        "type": "Point",
                        "coordinates": [
                            round(lon, geo_precision),
                            round(lat, geo_precision),
                        ],
                    },
                }
                for lon, lat, metric, point_radius in zip(
                    df[self.form_data.get("all_columns_x")],
                    df[self.form_data.get("all_columns_y")],
                    metric_col,
                    point_radius_col,
                    strict=False,
                )
            ],
        }

        x_series, y_series = (
            df[self.form_data.get("all_columns_x")],
            df[self.form_data.get("all_columns_y")],
        )
        south_west = [x_series.min(), y_series.min()]
        north_east = [x_series.max(), y_series.max()]

        return {
            "geoJSON": geo_json,
            "hasCustomMetric": has_custom_metric,
            "mapboxApiKey": current_app.config["MAPBOX_API_KEY"],
            "mapStyle": self.form_data.get("mapbox_style"),
            "aggregatorName": self.form_data.get("pandas_aggfunc"),
            "clusteringRadius": self.form_data.get("clustering_radius"),
            "pointRadiusUnit": self.form_data.get("point_radius_unit"),
            "globalOpacity": self.form_data.get("global_opacity"),
            "bounds": [south_west, north_east],
            "renderWhileDragging": self.form_data.get("render_while_dragging"),
            "tooltip": self.form_data.get("rich_tooltip"),
            "color": self.form_data.get("mapbox_color"),
        }


class DeckGLMultiLayer(BaseViz):
    """Pile on multiple DeckGL layers"""

    viz_type = "deck_multi"
    verbose_name = _("Deck.gl - Multiple Layers")

    is_timeseries = False
    credits = '<a href="https://uber.github.io/deck.gl/">deck.gl</a>'

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        return {}

    def _filter_items_by_scope(
        self,
        items: list[Any],
        layer_index: int,
        layer_filter_scope: dict[str, list[int]],
    ) -> list[Any]:
        """Filter items based on layer filter scope."""
        filtered_items = []
        for filter_item in items:
            filter_id = getattr(filter_item, "filterId", None)
            if filter_id:
                filter_scope = layer_filter_scope.get(filter_id, [])
                if filter_scope is None:
                    filter_scope = []
                if not filter_scope or layer_index in filter_scope:
                    filtered_items.append(filter_item)
            else:
                filtered_items.append(filter_item)
        return filtered_items

    def _process_extra_form_data_filters(
        self,
        layer_index: int,
        layer_filter_scope: dict[str, list[int]],
        filter_data_mapping: dict[str, list[Any]],
        extra_form_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Process extra_form_data filters with layer-specific filtering."""
        if not extra_form_data or not filter_data_mapping:
            return extra_form_data

        filtered_extra_form_data_filters = []
        for filter_id, filter_scope in layer_filter_scope.items():
            if filter_scope is None:
                filter_scope = []

            if not filter_scope or layer_index in filter_scope:
                filters_from_this_filter = filter_data_mapping.get(filter_id, [])
                filtered_extra_form_data_filters.extend(filters_from_this_filter)

        return {
            **extra_form_data,
            "filters": filtered_extra_form_data_filters,
        }

    def _apply_layer_filtering(
        self, form_data: dict[str, Any], layer_index: int
    ) -> dict[str, Any]:
        """Apply layer-specific filtering to form data."""
        layer_filter_scope = self.form_data.get("layer_filter_scope", {})
        filter_data_mapping = self.form_data.get("filter_data_mapping", {})

        if not layer_filter_scope:
            form_data["extra_filters"] = self.form_data.get("extra_filters", [])
            form_data["adhoc_filters"] = self.form_data.get("adhoc_filters")
            form_data["extra_form_data"] = self.form_data.get("extra_form_data")
            return form_data

        filtered_extra_filters = self._filter_items_by_scope(
            self.form_data.get("extra_filters", []), layer_index, layer_filter_scope
        )
        filtered_adhoc_filters = self._filter_items_by_scope(
            self.form_data.get("adhoc_filters", []), layer_index, layer_filter_scope
        )

        extra_form_data = self.form_data.get("extra_form_data", {})
        filtered_extra_form_data = self._process_extra_form_data_filters(
            layer_index, layer_filter_scope, filter_data_mapping, extra_form_data
        )

        form_data["extra_filters"] = filtered_extra_filters
        form_data["adhoc_filters"] = filtered_adhoc_filters
        form_data["extra_form_data"] = filtered_extra_form_data
        return form_data

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        # Late imports to avoid circular import issues
        # pylint: disable=import-outside-toplevel
        from superset import db
        from superset.models.slice import Slice

        slice_ids = self.form_data.get("deck_slices")
        slices = db.session.query(Slice).filter(Slice.id.in_(slice_ids)).all()

        features: dict[str, list[Any]] = {}

        for layer_index, slc in enumerate(slices):
            form_data = slc.form_data
            form_data = self._apply_layer_filtering(form_data, layer_index)

            viz_type_name = form_data.get("viz_type")
            viz_class = viz_types.get(viz_type_name)
            if not viz_class:
                continue  # skip unknown viz types

            viz_instance = viz_class(datasource=slc.datasource, form_data=form_data)
            payload = viz_instance.get_payload()

            if (
                payload
                and "data" in payload
                and payload["data"] is not None
                and "features" in payload["data"]
            ):
                if viz_type_name not in features:
                    features[viz_type_name] = []
                features[viz_type_name].extend(payload["data"]["features"])

        return {
            "features": features,
            "mapboxApiKey": current_app.config["MAPBOX_API_KEY"],
            "slices": [slc.data for slc in slices if slc.data is not None],
        }


class BaseDeckGLViz(BaseViz):
    """Base class for deck.gl visualizations"""

    is_timeseries = False
    credits = '<a href="https://uber.github.io/deck.gl/">deck.gl</a>'
    spatial_control_keys: list[str] = []

    def __init__(
        self, datasource: BaseDatasource, form_data: dict[str, Any], **kwargs: Any
    ) -> None:
        # Apply layer-specific filtering for deck multi layer charts in edit mode
        if self._should_apply_layer_filtering(form_data):
            form_data = self._apply_multilayer_filtering(form_data)
        super().__init__(datasource, form_data, **kwargs)

    def _should_apply_layer_filtering(self, form_data: dict[str, Any]) -> bool:
        """Check if this is a deck layer that's part of a multilayer setup."""
        return (
            "slice_id" in form_data
            and "adhoc_filters" in form_data
            and self._has_layer_scoped_filters(form_data)
        )

    def _has_layer_scoped_filters(self, form_data: dict[str, Any]) -> bool:
        """Check if any filter has layerFilterScope (indicates multilayer context)."""
        for filter_item in form_data.get("adhoc_filters", []):
            if (
                isinstance(filter_item, dict)
                and filter_item.get("layerFilterScope") is not None
            ):
                return True
        return False

    def _apply_multilayer_filtering(self, form_data: dict[str, Any]) -> dict[str, Any]:
        """
        Filter adhoc_filters based on layer scope for this specific layer.

        In deck multi-layer charts, each individual layer should only receive:
        1. Global filters (filters without layerFilterScope)
        2. Filters specifically scoped to this layer

        This prevents over-filtering when multiple layer-scoped filters are present.
        """
        slice_id = form_data.get("slice_id")
        deck_slices = self._get_deck_slices_from_filters(form_data)

        if not deck_slices or slice_id not in deck_slices:
            return form_data

        layer_index = deck_slices.index(slice_id)
        filtered_adhoc_filters = []

        for filter_item in form_data.get("adhoc_filters", []):
            layer_scope = self._get_filter_layer_scope(filter_item)

            # Include global filters (no layer scope) or filters scoped to this layer
            if layer_scope is None or layer_index in layer_scope:
                filtered_adhoc_filters.append(filter_item)

        modified_form_data = form_data.copy()
        modified_form_data["adhoc_filters"] = filtered_adhoc_filters
        return modified_form_data

    def _get_deck_slices_from_filters(
        self, form_data: dict[str, Any]
    ) -> list[int] | None:
        """Extract deck_slices from any filter that contains it."""
        for filter_item in form_data.get("adhoc_filters", []):
            if isinstance(filter_item, dict) and "deck_slices" in filter_item:
                return filter_item["deck_slices"]
        return None

    def _get_filter_layer_scope(self, filter_item: Any) -> list[int] | None:
        """Extract layerFilterScope from a filter item."""
        if isinstance(filter_item, dict):
            return filter_item.get("layerFilterScope")
        return getattr(filter_item, "layerFilterScope", None)

    @deprecated(deprecated_in="3.0")
    def get_metrics(self) -> list[str]:
        # pylint: disable=attribute-defined-outside-init
        self.metric = self.form_data.get("size")
        return [self.metric] if self.metric else []

    @deprecated(deprecated_in="3.0")
    def process_spatial_query_obj(self, key: str, group_by: list[str]) -> None:
        group_by.extend(self.get_spatial_columns(key))

    @deprecated(deprecated_in="3.0")
    def get_spatial_columns(self, key: str) -> list[str]:
        spatial = self.form_data.get(key)
        if spatial is None:
            raise ValueError(_("Bad spatial key"))

        if spatial.get("type") == "latlong":
            return [spatial.get("lonCol"), spatial.get("latCol")]

        if spatial.get("type") == "delimited":
            return [spatial.get("lonlatCol")]

        if spatial.get("type") == "geohash":
            return [spatial.get("geohashCol")]
        return []

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def parse_coordinates(latlong: Any) -> tuple[float, float] | None:
        if not latlong:
            return None
        try:
            point = Point(latlong)
            return (point.latitude, point.longitude)
        except Exception as ex:
            raise SpatialException(
                _("Invalid spatial point encountered: %(latlong)s", latlong=latlong)
            ) from ex

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def reverse_geohash_decode(geohash_code: str) -> tuple[str, str]:
        lat, lng = geohash.decode(geohash_code)
        return (lng, lat)

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def reverse_latlong(df: pd.DataFrame, key: str) -> None:
        df[key] = [tuple(reversed(o)) for o in df[key] if isinstance(o, (list, tuple))]

    @deprecated(deprecated_in="3.0")
    def process_spatial_data_obj(self, key: str, df: pd.DataFrame) -> pd.DataFrame:
        spatial = self.form_data.get(key)
        if spatial is None:
            raise ValueError(_("Bad spatial key"))

        if spatial.get("type") == "latlong":
            df[key] = list(
                zip(
                    pd.to_numeric(df[spatial.get("lonCol")], errors="coerce"),
                    pd.to_numeric(df[spatial.get("latCol")], errors="coerce"),
                    strict=False,
                )
            )
        elif spatial.get("type") == "delimited":
            lon_lat_col = spatial.get("lonlatCol")
            df[key] = df[lon_lat_col].apply(self.parse_coordinates)
            del df[lon_lat_col]
        elif spatial.get("type") == "geohash":
            df[key] = df[spatial.get("geohashCol")].map(self.reverse_geohash_decode)
            del df[spatial.get("geohashCol")]

        if spatial.get("reverseCheckbox"):
            self.reverse_latlong(df, key)

        if df.get(key) is None:
            raise NullValueException(
                _(
                    "Encountered invalid NULL spatial entry, \
                                       please consider filtering those out"
                )
            )
        return df

    @deprecated(deprecated_in="3.0")
    def add_null_filters(self) -> None:
        spatial_columns = set()
        for key in self.spatial_control_keys:
            for column in self.get_spatial_columns(key):
                spatial_columns.add(column)

        if self.form_data.get("adhoc_filters") is None:
            self.form_data["adhoc_filters"] = []

        if line_column := self.form_data.get("line_column"):
            spatial_columns.add(line_column)

        for column in sorted(spatial_columns):
            filter_ = simple_filter_to_adhoc(
                {"col": column, "op": "IS NOT NULL", "val": ""}
            )
            self.form_data["adhoc_filters"].append(filter_)

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        # add NULL filters
        if self.form_data.get("filter_nulls", True):
            self.add_null_filters()

        query_obj = super().query_obj()
        group_by: list[str] = []

        for key in self.spatial_control_keys:
            self.process_spatial_query_obj(key, group_by)

        if self.form_data.get("dimension"):
            group_by += [self.form_data["dimension"]]

        if self.form_data.get("js_columns"):
            group_by += self.form_data.get("js_columns") or []
        # Ensure this value is sorted so that it does not
        # cause the cache key generation (which hashes the
        # query object) to generate different keys for values
        # that should be considered the same.
        group_by = sorted(set(group_by))
        if metrics := self.get_metrics():
            query_obj["groupby"] = group_by
            query_obj["metrics"] = metrics
            query_obj["columns"] = []
            first_metric = query_obj["metrics"][0]
            query_obj["orderby"] = [
                (first_metric, not self.form_data.get("order_desc", True))
            ]
        else:
            query_obj["columns"] = group_by
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_js_columns(self, data: dict[str, Any]) -> dict[str, Any]:
        cols = self.form_data.get("js_columns") or []
        return {col: data.get(col) for col in cols}

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        # Processing spatial info
        for key in self.spatial_control_keys:
            df = self.process_spatial_data_obj(key, df)

        features = []
        for data in df.to_dict(orient="records"):
            feature = self.get_properties(data)
            extra_props = self.get_js_columns(data)
            if extra_props:
                feature["extraProps"] = extra_props
            features.append(feature)

        return {
            "features": features,
            "mapboxApiKey": current_app.config["MAPBOX_API_KEY"],
            "metricLabels": self.metric_labels,
        }

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError()


class DeckScatterViz(BaseDeckGLViz):
    """deck.gl's ScatterLayer"""

    viz_type = "deck_scatter"
    verbose_name = _("Deck.gl - Scatter plot")
    spatial_control_keys = ["spatial"]
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        # pylint: disable=attribute-defined-outside-init
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        self.point_radius_fixed = self.form_data.get("point_radius_fixed") or {
            "type": "fix",
            "value": 500,
        }
        return super().query_obj()

    @deprecated(deprecated_in="3.0")
    def get_metrics(self) -> list[str]:
        # pylint: disable=attribute-defined-outside-init
        self.metric = None
        if self.point_radius_fixed.get("type") == "metric":
            self.metric = self.point_radius_fixed["value"]
            return [self.metric]
        return []

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "metric": data.get(self.metric_label) if self.metric_label else None,
            "radius": (
                self.fixed_value
                if self.fixed_value
                else data.get(self.metric_label)
                if self.metric_label
                else None
            ),
            "cat_color": data.get(self.dim) if self.dim else None,
            "position": data.get("spatial"),
            DTTM_ALIAS: data.get(DTTM_ALIAS),
        }

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        # pylint: disable=attribute-defined-outside-init
        self.metric_label = utils.get_metric_name(self.metric) if self.metric else None
        self.point_radius_fixed = self.form_data.get("point_radius_fixed")
        self.fixed_value = None
        self.dim = self.form_data.get("dimension")
        if self.point_radius_fixed and self.point_radius_fixed.get("type") != "metric":
            self.fixed_value = self.point_radius_fixed.get("value")
        return super().get_data(df)


class DeckScreengrid(BaseDeckGLViz):
    """deck.gl's ScreenGridLayer"""

    viz_type = "deck_screengrid"
    verbose_name = _("Deck.gl - Screen Grid")
    spatial_control_keys = ["spatial"]
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        return super().query_obj()

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
            "__timestamp": data.get(DTTM_ALIAS) or data.get("__time"),
        }

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = (  # pylint: disable=attribute-defined-outside-init
            utils.get_metric_name(self.metric) if self.metric else None
        )
        return super().get_data(df)


class DeckGrid(BaseDeckGLViz):
    """deck.gl's DeckLayer"""

    viz_type = "deck_grid"
    verbose_name = _("Deck.gl - 3D Grid")
    spatial_control_keys = ["spatial"]

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = (  # pylint: disable=attribute-defined-outside-init
            utils.get_metric_name(self.metric) if self.metric else None
        )
        return super().get_data(df)


@deprecated(deprecated_in="3.0")
def geohash_to_json(geohash_code: str) -> list[list[float]]:
    bbox = geohash.bbox(geohash_code)
    return [
        [bbox.get("w"), bbox.get("n")],
        [bbox.get("e"), bbox.get("n")],
        [bbox.get("e"), bbox.get("s")],
        [bbox.get("w"), bbox.get("s")],
        [bbox.get("w"), bbox.get("n")],
    ]


class DeckPathViz(BaseDeckGLViz):
    """deck.gl's PathLayer"""

    viz_type = "deck_path"
    verbose_name = _("Deck.gl - Paths")
    deck_viz_key = "path"
    is_timeseries = True
    deser_map = {
        "json": json.loads,
        "polyline": polyline.decode,
        "geohash": geohash_to_json,
    }

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        # pylint: disable=attribute-defined-outside-init
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        query_obj = super().query_obj()
        self.metric = self.form_data.get("metric")
        line_col = self.form_data.get("line_column")
        if query_obj["metrics"]:
            self.has_metrics = True
            query_obj["groupby"].append(line_col)
        else:
            self.has_metrics = False
            query_obj["columns"].append(line_col)
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        line_type = self.form_data["line_type"]
        deser = self.deser_map[line_type]
        line_column = self.form_data["line_column"]
        path = deser(data[line_column])
        if self.form_data.get("reverse_long_lat"):
            path = [(o[1], o[0]) for o in path]
        data[self.deck_viz_key] = path
        if line_type != "geohash":
            del data[line_column]
        data["__timestamp"] = data.get(DTTM_ALIAS) or data.get("__time")
        return data

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = (  # pylint: disable=attribute-defined-outside-init
            utils.get_metric_name(self.metric) if self.metric else None
        )
        return super().get_data(df)


class DeckPolygon(DeckPathViz):
    """deck.gl's Polygon Layer"""

    viz_type = "deck_polygon"
    deck_viz_key = "polygon"
    verbose_name = _("Deck.gl - Polygon")

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        # pylint: disable=attribute-defined-outside-init
        self.elevation = self.form_data.get("point_radius_fixed") or {
            "type": "fix",
            "value": 500,
        }
        return super().query_obj()

    @deprecated(deprecated_in="3.0")
    def get_metrics(self) -> list[str]:
        metrics = [self.form_data.get("metric")]
        if self.elevation.get("type") == "metric":
            metrics.append(self.elevation.get("value"))
        return [metric for metric in metrics if metric]

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        super().get_properties(data)
        elevation = self.form_data["point_radius_fixed"]["value"]
        type_ = self.form_data["point_radius_fixed"]["type"]
        data["elevation"] = (
            data.get(utils.get_metric_name(elevation))
            if type_ == "metric"
            else elevation
        )
        return data


class DeckHex(BaseDeckGLViz):
    """deck.gl's DeckLayer"""

    viz_type = "deck_hex"
    verbose_name = _("Deck.gl - 3D HEX")
    spatial_control_keys = ["spatial"]

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = (  # pylint: disable=attribute-defined-outside-init
            utils.get_metric_name(self.metric) if self.metric else None
        )
        return super().get_data(df)


class DeckHeatmap(BaseDeckGLViz):
    """deck.gl's HeatmapLayer"""

    viz_type = "deck_heatmap"
    verbose_name = _("Deck.gl - Heatmap")
    spatial_control_keys = ["spatial"]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = (  # pylint: disable=attribute-defined-outside-init
            utils.get_metric_name(self.metric) if self.metric else None
        )
        return super().get_data(df)


class DeckContour(BaseDeckGLViz):
    """deck.gl's ContourLayer"""

    viz_type = "deck_contour"
    verbose_name = _("Deck.gl - Contour")
    spatial_control_keys = ["spatial"]

    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "position": data.get("spatial"),
            "weight": (data.get(self.metric_label) if self.metric_label else None) or 1,
        }

    def get_data(self, df: pd.DataFrame) -> VizData:
        self.metric_label = (  # pylint: disable=attribute-defined-outside-init
            utils.get_metric_name(self.metric) if self.metric else None
        )
        return super().get_data(df)


class DeckGeoJson(BaseDeckGLViz):
    """deck.gl's GeoJSONLayer"""

    viz_type = "deck_geojson"
    verbose_name = _("Deck.gl - GeoJSON")

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        query_obj["columns"] += [self.form_data.get("geojson")]
        query_obj["metrics"] = []
        query_obj["groupby"] = []
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        geojson = data[get_column_name(self.form_data["geojson"])]
        return json.loads(geojson)


class DeckArc(BaseDeckGLViz):
    """deck.gl's Arc Layer"""

    viz_type = "deck_arc"
    verbose_name = _("Deck.gl - Arc")
    spatial_control_keys = ["start_spatial", "end_spatial"]
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        self.is_timeseries = bool(self.form_data.get("time_grain_sqla"))
        return super().query_obj()

    @deprecated(deprecated_in="3.0")
    def get_properties(self, data: dict[str, Any]) -> dict[str, Any]:
        dim = self.form_data.get("dimension")
        return {
            "sourcePosition": data.get("start_spatial"),
            "targetPosition": data.get("end_spatial"),
            "cat_color": data.get(dim) if dim else None,
            DTTM_ALIAS: data.get(DTTM_ALIAS),
        }

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        return {
            "features": super().get_data(df)["features"],
            "mapboxApiKey": current_app.config["MAPBOX_API_KEY"],
        }


class EventFlowViz(BaseViz):
    """A visualization to explore patterns in event sequences"""

    viz_type = "event_flow"
    verbose_name = _("Event flow")
    credits = 'from <a href="https://github.com/williaster/data-ui">@data-ui</a>'
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query = super().query_obj()
        form_data = self.form_data

        event_key = form_data["all_columns_x"]
        entity_key = form_data["entity"]
        meta_keys = [
            col
            for col in form_data["all_columns"] or []
            if col not in (event_key, entity_key)
        ]

        query["columns"] = [event_key, entity_key] + meta_keys

        if form_data["order_by_entity"]:
            query["orderby"] = [(entity_key, True)]

        return query

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        return df.to_dict(orient="records")


class PairedTTestViz(BaseViz):
    """A table displaying paired t-test values"""

    viz_type = "paired_ttest"
    verbose_name = _("Time Series - Paired t-test")
    sort_series = False
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        if sort_by := self.form_data.get("timeseries_limit_metric"):
            sort_by_label = utils.get_metric_name(sort_by)
            if sort_by_label not in utils.get_metric_names(query_obj["metrics"]):
                query_obj["metrics"].append(sort_by)
            if self.form_data.get("order_desc"):
                query_obj["orderby"] = [
                    (sort_by, not self.form_data.get("order_desc", True))
                ]
        return query_obj

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        """
        Transform received data frame into an object of the form:
        {
            'metric1': [
                {
                    groups: ('groupA', ... ),
                    values: [ {x, y}, ... ],
                }, ...
            ], ...
        }
        """

        if df.empty:
            return None

        groups = get_column_names(self.form_data.get("groupby"))
        metrics = self.metric_labels
        df = df.pivot_table(index=DTTM_ALIAS, columns=groups, values=metrics)
        cols = []
        # Be rid of falsy keys
        for col in df.columns:
            if col == "":
                cols.append("N/A")
            elif col is None:
                cols.append("NULL")
            else:
                cols.append(col)
        df.columns = cols
        data: dict[str, list[dict[str, Any]]] = {}
        series = df.to_dict("series")
        for name_set in df.columns:
            # If no groups are defined, nameSet will be the metric name
            has_group = not isinstance(name_set, str)
            data_ = {
                "group": name_set[1:] if has_group else "All",
                "values": [
                    {
                        "x": t,
                        "y": series[name_set][t] if t in series[name_set] else None,
                    }
                    for t in df.index
                ],
            }
            key = name_set[0] if has_group else name_set
            if key in data:
                data[key].append(data_)
            else:
                data[key] = [data_]
        return data


class RoseViz(NVD3TimeSeriesViz):
    viz_type = "rose"
    verbose_name = _("Time Series - Nightingale Rose Chart")
    sort_series = False
    is_timeseries = True

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None

        data = super().get_data(df)
        result: dict[str, list[dict[str, str]]] = {}
        for datum in data:
            key = datum["key"]
            for val in datum["values"]:
                timestamp = val["x"].value
                if not result.get(timestamp):
                    result[timestamp] = []
                value = 0 if math.isnan(val["y"]) else val["y"]
                result[timestamp].append(
                    {
                        "key": key,
                        "value": value,  # type: ignore
                        "name": ", ".join(key) if isinstance(key, list) else key,
                        "time": val["x"],
                    }
                )
        return result


class PartitionViz(NVD3TimeSeriesViz):
    """
    A hierarchical data visualization with support for time series.
    """

    viz_type = "partition"
    verbose_name = _("Partition Diagram")

    @deprecated(deprecated_in="3.0")
    def query_obj(self) -> QueryObjectDict:
        query_obj = super().query_obj()
        time_op = self.form_data.get("time_series_option", "not_time")
        # Return time series data if the user specifies so
        query_obj["is_timeseries"] = time_op != "not_time"
        return query_obj

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def levels_for(
        time_op: str, groups: list[str], df: pd.DataFrame
    ) -> dict[int, pd.Series]:
        """
        Compute the partition at each `level` from the dataframe.
        """
        levels = {}
        for i in range(0, len(groups) + 1):
            agg_df = df.groupby(groups[:i]) if i else df
            levels[i] = (
                agg_df.mean(numeric_only=True)
                if time_op == "agg_mean"
                else agg_df.sum(numeric_only=True)
            )
        return levels

    @staticmethod
    @deprecated(deprecated_in="3.0")
    def levels_for_diff(
        time_op: str, groups: list[str], df: pd.DataFrame
    ) -> dict[int, pd.DataFrame]:
        # Obtain a unique list of the time grains
        times = list(set(df[DTTM_ALIAS]))
        times.sort()
        until = times[len(times) - 1]
        since = times[0]
        # Function describing how to calculate the difference
        func = {
            "point_diff": [pd.Series.sub, lambda a, b, fill_value: a - b],
            "point_factor": [pd.Series.div, lambda a, b, fill_value: a / float(b)],
            "point_percent": [
                lambda a, b, fill_value=0: a.div(b, fill_value=fill_value) - 1,
                lambda a, b, fill_value: a / float(b) - 1,
            ],
        }[time_op]
        agg_df = df.groupby(DTTM_ALIAS).sum(numeric_only=True)
        levels = {
            0: pd.Series(
                {
                    m: func[1](agg_df[m][until], agg_df[m][since], 0)
                    for m in agg_df.columns
                }
            )
        }
        for i in range(1, len(groups) + 1):
            agg_df = df.groupby([DTTM_ALIAS] + groups[:i]).sum(numeric_only=True)
            levels[i] = pd.DataFrame(
                {
                    m: func[0](agg_df[m][until], agg_df[m][since], fill_value=0)
                    for m in agg_df.columns
                }
            )
        return levels

    @deprecated(deprecated_in="3.0")
    def levels_for_time(
        self, groups: list[str], df: pd.DataFrame
    ) -> dict[int, VizData]:
        procs = {}
        for i in range(0, len(groups) + 1):
            self.form_data["groupby"] = groups[:i]
            df_drop = df.drop(groups[i:], axis=1)
            procs[i] = self.process_data(df_drop, aggregate=True)
        self.form_data["groupby"] = groups
        return procs

    @deprecated(deprecated_in="3.0")
    def nest_values(
        self,
        levels: dict[int, pd.DataFrame],
        level: int = 0,
        metric: str | None = None,
        dims: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Nest values at each level on the back-end with
        access and setting, instead of summing from the bottom.
        """
        if dims is None:
            dims = []
        if not level:
            return [
                {
                    "name": m,
                    "val": levels[0][m],
                    "children": self.nest_values(levels, 1, m),
                }
                for m in levels[0].index
            ]
        if level == 1:
            metric_level = levels[1][metric]
            return [
                {
                    "name": i,
                    "val": metric_level[i],
                    "children": self.nest_values(levels, 2, metric, [i]),
                }
                for i in metric_level.index
            ]
        if level >= len(levels):
            return []

        dim_level = levels[level][metric]
        for d in dims:
            if d not in dim_level:
                return []
            dim_level = dim_level[d]

        return [
            {
                "name": [*dims, i],
                "val": dim_level[i],
                "children": self.nest_values(levels, level + 1, metric, dims + [i]),
            }
            for i in dim_level.index
        ]

    @deprecated(deprecated_in="3.0")
    def nest_procs(
        self,
        procs: dict[int, pd.DataFrame],
        level: int = -1,
        dims: tuple[str, ...] | None = None,
        time: Any = None,
    ) -> list[dict[str, Any]]:
        if dims is None:
            dims = ()
        if level == -1:
            return [
                {"name": m, "children": self.nest_procs(procs, 0, (m,))}
                for m in procs[0].columns
            ]
        if not level:
            return [
                {
                    "name": t,
                    "val": procs[0][dims[0]][t],
                    "children": self.nest_procs(procs, 1, dims, t),
                }
                for t in procs[0].index
            ]
        if level >= len(procs):
            return []
        return [
            {
                "name": i,
                "val": procs[level][dims][i][time],
                "children": self.nest_procs(procs, level + 1, dims + (i,), time),
            }
            for i in procs[level][dims].columns
        ]

    @deprecated(deprecated_in="3.0")
    def get_data(self, df: pd.DataFrame) -> VizData:
        if df.empty:
            return None
        groups = get_column_names(self.form_data.get("groupby"))
        time_op = self.form_data.get("time_series_option", "not_time")
        if not groups:
            raise ValueError(_("Please choose at least one groupby"))
        if time_op == "not_time":
            levels = self.levels_for("agg_sum", groups, df)
        elif time_op in ["agg_sum", "agg_mean"]:
            levels = self.levels_for(time_op, groups, df)
        elif time_op in ["point_diff", "point_factor", "point_percent"]:
            levels = self.levels_for_diff(time_op, groups, df)
        elif time_op == "adv_anal":
            procs = self.levels_for_time(groups, df)
            return self.nest_procs(procs)
        else:
            levels = self.levels_for("agg_sum", [DTTM_ALIAS] + groups, df)
        return self.nest_values(levels)


@deprecated(deprecated_in="3.0")
def get_subclasses(cls: type[BaseViz]) -> set[type[BaseViz]]:
    return set(cls.__subclasses__()).union(
        [sc for c in cls.__subclasses__() for sc in get_subclasses(c)]
    )


viz_types = {
    o.viz_type: o
    for o in get_subclasses(BaseViz)
    if o.viz_type not in current_app.config["VIZ_TYPE_DENYLIST"]
}
