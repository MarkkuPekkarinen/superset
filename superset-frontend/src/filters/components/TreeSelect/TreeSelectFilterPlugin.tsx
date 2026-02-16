/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ensureIsArray, getColumnLabel } from '@superset-ui/core';
import { getSelectExtraFormData } from '../../utils';
import { FilterPluginStyle } from '../common';
import { TreeSelect, type TreeSelectProps } from '@superset-ui/core/components';
import {
  DEFAULT_FORM_DATA,
  PluginFilterTreeSelectProps,
  SelectValue,
} from './types';

type TreeNode = {
  key: string;
  value: string;
  title: string;
  children?: TreeNode[];
};

function normalizeValue(value: unknown): string | undefined {
  if (value === null || value === undefined) {
    return undefined;
  }
  return String(value);
}

function buildTreeData(values: string[]): TreeNode[] {
  type MutableNode = TreeNode & { childrenMap: Map<string, MutableNode> };

  const roots = new Map<string, MutableNode>();

  const ensureChild = (
    container: Map<string, MutableNode>,
    nodeValue: string,
    title: string,
  ) => {
    let node = container.get(nodeValue);
    if (!node) {
      node = {
        key: nodeValue,
        value: nodeValue,
        title,
        children: [],
        childrenMap: new Map<string, MutableNode>(),
      };
      container.set(nodeValue, node);
    }
    return node;
  };

  values.forEach(path => {
    const delimiter = path.includes('/') ? '/' : '.';
    const segments =
      delimiter === '/'
        ? path.split('/').filter(Boolean)
        : path.split('.').filter(Boolean);

    if (!segments.length) {
      ensureChild(roots, path, path);
      return;
    }

    let currentMap = roots;
    let assembled = delimiter === '/' ? '' : undefined;

    segments.forEach(segment => {
      const nextValue =
        delimiter === '/'
          ? `${assembled}/${segment}`
          : assembled
            ? `${assembled}.${segment}`
            : segment;
      assembled = nextValue;
      const node = ensureChild(currentMap, nextValue, segment);
      currentMap = node.childrenMap;
    });
  });

  const convertNode = (node: MutableNode): TreeNode => ({
    key: node.key,
    value: node.value,
    title: node.title,
    children: [...node.childrenMap.values()].map(convertNode),
  });

  return [...roots.values()].map(convertNode);
}

export default function TreeSelectFilterPlugin(
  props: PluginFilterTreeSelectProps,
) {
  const {
    data,
    filterState,
    formData,
    height,
    isRefreshing,
    width,
    setDataMask,
    setHoveredFilter,
    unsetHoveredFilter,
    setFocusedFilter,
    unsetFocusedFilter,
    setFilterActive,
    inputRef,
    clearAllTrigger,
    onClearAllComplete,
  } = props;

  const { enableEmptyFilter, inverseSelection, multiSelect } = {
    ...DEFAULT_FORM_DATA,
    ...formData,
  };

  const groupby = useMemo(
    () => ensureIsArray(formData.groupby).map(getColumnLabel),
    [formData.groupby],
  );
  const [col] = groupby;
  const [value, setValue] = useState<SelectValue>(filterState?.value);

  useEffect(() => {
    setValue(filterState?.value);
  }, [filterState?.value]);

  useEffect(() => {
    if (clearAllTrigger) {
      setValue(undefined);
      onClearAllComplete?.(formData.nativeFilterId);
    }
  }, [clearAllTrigger, formData.nativeFilterId, onClearAllComplete]);

  const options = useMemo(() => {
    if (!col) {
      return [];
    }

    const values = data
      .map(row => normalizeValue(row[col]))
      .filter((item): item is string => !!item);
    return buildTreeData(values);
  }, [col, data]);

  const handleChange = useCallback<NonNullable<TreeSelectProps['onChange']>>(
    nextValue => {
      const normalized = (
        Array.isArray(nextValue)
          ? nextValue.map(item => String(item))
          : nextValue
            ? [String(nextValue)]
            : undefined
      ) as SelectValue;

      setValue(normalized);

      const emptyFilter =
        enableEmptyFilter && !inverseSelection && !normalized?.length;
      setDataMask({
        filterState: {
          value: normalized,
          label: normalized?.join(', '),
          excludeFilterValues: inverseSelection,
        },
        extraFormData: col
          ? getSelectExtraFormData(
              col,
              normalized,
              emptyFilter,
              inverseSelection,
            )
          : {},
      });
      setFilterActive(!!normalized?.length);
    },
    [col, enableEmptyFilter, inverseSelection, setDataMask, setFilterActive],
  );

  return (
    <FilterPluginStyle width={width} height={height}>
      <TreeSelect
        ref={inputRef}
        allowClear
        treeCheckable={multiSelect}
        showSearch
        disabled={isRefreshing}
        style={{ width: '100%' }}
        value={value as string[] | undefined}
        treeData={options}
        onChange={handleChange}
        onFocus={() => {
          setFocusedFilter();
          setHoveredFilter();
        }}
        onBlur={() => {
          unsetFocusedFilter();
          unsetHoveredFilter();
        }}
        onMouseEnter={setHoveredFilter}
        onMouseLeave={unsetHoveredFilter}
      />
    </FilterPluginStyle>
  );
}
