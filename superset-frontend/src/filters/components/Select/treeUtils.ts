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

export type TreeOption = {
  label: string;
  value: string | number | null;
};

export type TreeSelectDataNode = {
  title: string;
  value: string;
  key: string;
  selectable: boolean;
  children?: TreeSelectDataNode[];
};

type InternalNode = {
  title: string;
  value: string;
  key: string;
  children: Record<string, InternalNode>;
};

const createNode = (title: string, value: string): InternalNode => ({
  title,
  value,
  key: value,
  children: {},
});

const toSortedTree = (
  nodeMap: Record<string, InternalNode>,
): TreeSelectDataNode[] =>
  Object.values(nodeMap)
    .sort((a, b) => a.value.localeCompare(b.value))
    .map(node => ({
      title: node.title,
      value: node.value,
      key: node.key,
      selectable: true,
      children: toSortedTree(node.children),
    }));

export function buildTreeSelectData(
  options: TreeOption[],
  delimiter = '/',
): TreeSelectDataNode[] {
  const root: Record<string, InternalNode> = {};

  options.forEach(option => {
    if (typeof option.value !== 'string') {
      return;
    }

    const trimmed = option.value.trim();
    if (!trimmed) {
      return;
    }

    const parts = trimmed.split(delimiter).filter(Boolean);
    if (!parts.length) {
      return;
    }

    let cursor = root;
    parts.forEach((part, index) => {
      const pathValue = parts.slice(0, index + 1).join(delimiter);
      if (!cursor[part]) {
        cursor[part] = createNode(part, pathValue);
      }
      if (index === parts.length - 1) {
        cursor[part].title = option.label;
      }
      cursor = cursor[part].children;
    });
  });

  return toSortedTree(root);
}

export function normalizeTreeSelectValue(
  value: string | string[] | number | number[] | null | undefined,
): (string | number)[] | null | undefined {
  if (value === undefined) {
    return undefined;
  }
  if (value === null) {
    return null;
  }
  if (Array.isArray(value)) {
    return value.filter(v => typeof v === 'string' || typeof v === 'number');
  }
  if (typeof value === 'string' || typeof value === 'number') {
    return [value];
  }
  return null;
}
