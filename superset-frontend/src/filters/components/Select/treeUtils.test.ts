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

import { buildTreeSelectData, normalizeTreeSelectValue } from './treeUtils';

describe('treeUtils', () => {
  it('builds hierarchy from path values', () => {
    const tree = buildTreeSelectData(
      [
        { label: 'Helsinki', value: 'company/europe/finland/helsinki' },
        { label: 'Espoo', value: 'company/europe/finland/espoo' },
      ],
      '/',
    );

    expect(tree).toEqual([
      {
        title: 'company',
        value: 'company',
        key: 'company',
        selectable: true,
        children: [
          {
            title: 'europe',
            value: 'company/europe',
            key: 'company/europe',
            selectable: true,
            children: [
              {
                title: 'finland',
                value: 'company/europe/finland',
                key: 'company/europe/finland',
                selectable: true,
                children: [
                  {
                    title: 'Espoo',
                    value: 'company/europe/finland/espoo',
                    key: 'company/europe/finland/espoo',
                    selectable: true,
                    children: [],
                  },
                  {
                    title: 'Helsinki',
                    value: 'company/europe/finland/helsinki',
                    key: 'company/europe/finland/helsinki',
                    selectable: true,
                    children: [],
                  },
                ],
              },
            ],
          },
        ],
      },
    ]);
  });

  it('normalizes TreeSelect value payload', () => {
    expect(normalizeTreeSelectValue('a/b')).toEqual(['a/b']);
    expect(normalizeTreeSelectValue(['a/b', 'a/c'])).toEqual(['a/b', 'a/c']);
    expect(normalizeTreeSelectValue(null)).toBeNull();
    expect(normalizeTreeSelectValue(undefined)).toBeUndefined();
  });
});
